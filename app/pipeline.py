"""Uma rodada completa: coletar → normalizar/deduplicar → filtrar → pontuar → gerar CV → notificar.

Tudo em sequência (uma fonte por vez, um CV por vez), conforme as restrições de recurso da VM.
Cada passo escreve no log da execução (`registro.log`) e atualiza a barra de progresso.
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import datetime, timedelta
from typing import Callable

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from . import registro
from .coletores.base import Coletor, VagaBruta, calcular_hash, detectar_idioma, idade_horas
from .coletores.email_imap import ColetorEmail
from .coletores.github_issues import ColetorGithubIssues
from .config import Config
from .curriculo import render
from .curriculo.mestre import Mestre, Pessoais, carregar_mestre, carregar_pessoais
from .curriculo.personalizar import personalizar
from .db.models import (
    CurriculoGerado,
    Execucao,
    FonteEstado,
    Match,
    Vaga,
    agora_utc,
)
from .llm.rodizio import ProvedoresEsgotados, Rodizio
from .match import extracao_llm, filtros, score
from .match.requisitos import RequisitosVaga, extrair_por_palavras
from .match.seguranca import detectar_instrucoes
from .notificacao.discord import Discord

log = logging.getLogger(__name__)

#: nunca duas execuções ao mesmo tempo (agendada + "Rodar agora")
TRAVA = threading.Lock()

# Faixas da barra de progresso (%): coleta, pontuação, CVs, notificação
FAIXA_COLETA = (2, 40)
FAIXA_PONTUACAO = (40, 65)
FAIXA_CV = (65, 95)


def _faixa(faixa: tuple[float, float], i: int, n: int) -> float:
    ini, fim = faixa
    return ini + (fim - ini) * (i / n if n else 1)


class Servicos:
    """Dependências compartilhadas por pipeline, CLI e painel."""

    def __init__(self, config: Config, fabrica: sessionmaker[Session],
                 transporte: httpx.BaseTransport | None = None):
        self.config = config
        self.fabrica = fabrica
        self.transporte = transporte
        self.discord = Discord(config, transporte)
        self.rodizio = Rodizio(config, fabrica, transporte=transporte,
                               ao_desativar=lambda p, m: self.discord.erro(
                                   f"Chave de LLM inválida: {p}", f"O provedor foi desativado. {m}"))

    def mestre(self) -> Mestre:
        return carregar_mestre(self.config.caminho("mestre"))

    def pessoais(self) -> Pessoais:
        return carregar_pessoais(self.config.caminho("pessoais"))

    def idade_maxima_horas(self) -> float | None:
        v = self.config.obter("filtros.idade_maxima_horas", 24)
        return float(v) if v else None

    def corte_idade(self) -> datetime | None:
        """Vagas publicadas antes disto são velhas demais. None = sem limite."""
        h = self.idade_maxima_horas()
        return agora_utc() - timedelta(hours=h) if h else None


def _recente(corte: datetime | None):
    """Condição SQL: publicada depois do corte (ou sem data conhecida)."""
    if corte is None:
        return Vaga.id > 0
    return or_(Vaga.data_publicacao.is_(None), Vaga.data_publicacao >= corte)


# ------------------------------------------------------------------ coleta
def _intervalo_ok(s: Session, fonte: str, horas: float) -> bool:
    est = s.get(FonteEstado, fonte)
    if est is None or est.ultima_execucao is None:
        return True
    return agora_utc() - est.ultima_execucao >= timedelta(hours=horas)


def montar_coletores(sv: Servicos) -> list[tuple[str, float, Coletor]]:
    """(nome do estado, intervalo mínimo em horas, coletor) para cada fonte ativa."""
    cfg = sv.config
    resultado: list[tuple[str, float, Coletor]] = []
    cliente = httpx.Client(transport=sv.transporte, timeout=30)

    gh = cfg.obter("fontes.github_issues") or {}
    if gh.get("ativo"):
        token = cfg.segredos.obter("GITHUB_TOKEN")
        repos = cfg.obter("fontes.github_issues.repos") or gh.get("repos", [])
        for item in repos:
            nome = f"github:{item['repo']}"
            resultado.append((nome, float(gh.get("min_intervalo_horas", 3)), ColetorGithubIssues(
                item["repo"], item.get("labels", []), gh.get("labels_excluir", []), token=token,
                max_paginas=int(gh.get("max_paginas", 3)), cliente=cliente,
                idade_maxima_horas=sv.idade_maxima_horas())))

    em = dict(cfg.obter("fontes.email") or {})
    for chave in ("ativo", "servidor", "porta", "usuario", "pasta"):
        em[chave] = cfg.obter(f"fontes.email.{chave}", em.get(chave))
    senha = cfg.segredos.obter("EMAIL_SENHA")
    if em.get("ativo") and em.get("usuario") and senha:
        resultado.append(("email", float(em.get("min_intervalo_horas", 3)), ColetorEmail(
            em["servidor"], int(em["porta"]), em["usuario"], senha, em.get("pasta", "INBOX"),
            em.get("remetentes", []), em.get("marcar_como_lido", True), int(em.get("max_emails", 50)))))
    return resultado


def _assume_remoto(config: Config, fonte: str) -> bool:
    return fonte.startswith("email") and bool(config.obter("fontes.email.assume_remoto", True))


def _chave_fraca(s: Session, v: VagaBruta) -> bool:
    """Mesma empresa + mesmo título já registrados (ex.: a vaga chegou por outra fonte/URL)."""
    if not v.empresa.strip():
        return False
    return s.scalar(select(Vaga.id).where(
        func.lower(Vaga.empresa) == v.empresa.strip().lower(),
        func.lower(Vaga.titulo) == v.titulo.strip().lower()).limit(1)) is not None


def salvar_se_nova(s: Session, v: VagaBruta) -> Vaga | None:
    """Insere a vaga se for inédita (hash ou empresa+título). Devolve a vaga nova ou None."""
    h = calcular_hash(v)
    if s.scalar(select(Vaga.id).where(Vaga.hash == h)) is not None or _chave_fraca(s, v):
        return None
    vaga = Vaga(
        fonte=v.fonte, url=v.url, empresa=v.empresa, titulo=v.titulo, descricao=v.descricao, local=v.local,
        modelo_trabalho=v.modelo_trabalho, salario=v.salario, data_publicacao=v.data_publicacao, hash=h,
        labels_json=json.dumps(v.labels, ensure_ascii=False), descricao_completa=v.descricao_completa,
        idioma=v.idioma or detectar_idioma(f"{v.titulo}\n{v.descricao}"),
        avisos_json=json.dumps(detectar_instrucoes(v.descricao), ensure_ascii=False),
    )
    s.add(vaga)
    s.flush()
    return vaga


def coletar_tudo(sv: Servicos) -> tuple[int, int, list[str]]:
    """Roda cada fonte (uma por vez, respeitando o intervalo mínimo). Devolve (coletadas, novas, erros).

    Vagas abertas há mais que `filtros.idade_maxima_horas` (padrão 24 h) nem chegam a ser gravadas.
    """
    coletadas = novas = 0
    erros: list[str] = []
    fontes = montar_coletores(sv)
    maxima = sv.idade_maxima_horas()
    registro.log(f"Coleta: {len(fontes)} fonte(s) ativa(s); idade máxima da vaga: "
                 f"{f'{maxima:g} h' if maxima else 'sem limite'}.", "info", "coleta")
    for i, (nome, intervalo, coletor) in enumerate(fontes):
        registro.progresso(_faixa(FAIXA_COLETA, i, len(fontes)), f"Coletando {nome} ({i + 1}/{len(fontes)})")
        with sv.fabrica() as s:
            if not _intervalo_ok(s, nome, intervalo):
                registro.log(f"{nome}: pulada (intervalo mínimo de {intervalo:g} h ainda não passou).", "info", "coleta")
                continue
        inicio = agora_utc()
        try:
            vagas = coletor.coletar()
        except Exception as e:  # noqa: BLE001 - um coletor quebrado não derruba os outros
            msg = f"{nome}: {e}"
            registro.log(f"{nome}: FALHOU — {e}", "erro", "coleta")
            erros.append(msg)
            sv.discord.erro("Coletor com falha", msg)
            continue
        antigas = duplicadas = novas_fonte = 0
        with sv.fabrica() as s:
            for v in vagas:
                coletadas += 1
                idade = idade_horas(v.data_publicacao)
                if maxima and idade is not None and idade > maxima:
                    antigas += 1
                    continue
                if salvar_se_nova(s, v) is not None:
                    novas_fonte += 1
                else:
                    duplicadas += 1
            est = s.get(FonteEstado, nome) or FonteEstado(fonte=nome)
            est.ultima_execucao = inicio
            s.add(est)
            s.commit()
        novas += novas_fonte
        stats = getattr(coletor, "stats", {})
        detalhe = (f"issues lidas {stats.get('lidas', '?')} em {stats.get('paginas', '?')} página(s); "
                   f"{stats.get('antigas', 0)} velhas demais no coletor, {stats.get('label_excluida', 0)} com label "
                   f"excluída, {stats.get('prs', 0)} PRs; " if stats else "")
        registro.log(f"{nome}: {detalhe}{len(vagas)} candidata(s) → {novas_fonte} NOVA(S), {duplicadas} já conhecida(s), "
                     f"{antigas} velha(s) demais.", "info", "coleta")
    registro.log(f"Coleta concluída: {coletadas} candidata(s), {novas} nova(s).", "info", "coleta")
    return coletadas, novas, erros


# ------------------------------------------------------------------ processamento
def requisitos_da_vaga(sv: Servicos, vaga: Vaga) -> tuple[RequisitosVaga, str]:
    """Extração com LLM quando há descrição completa e provedor disponível; senão, por palavras."""
    if vaga.requisitos_json:
        try:
            return RequisitosVaga.model_validate_json(vaga.requisitos_json), "llm"
        except Exception:  # noqa: BLE001
            pass
    labels = json.loads(vaga.labels_json or "[]")
    if vaga.descricao_completa and sv.config.obter("match.usar_llm_na_extracao", True):
        try:
            req, prov, modelo = extracao_llm.extrair(sv.rodizio, vaga.titulo, vaga.empresa, vaga.descricao)
            vaga.requisitos_json = req.model_dump_json()
            return req, "llm"
        except ProvedoresEsgotados:
            registro.log("Extração por LLM indisponível; usando palavras-chave.", "info", "match")
    return extrair_por_palavras(vaga.titulo, vaga.descricao, labels), "palavras"


def pontuar(sv: Servicos, s: Session, vaga: Vaga, mestre: Mestre) -> Match | None:
    """Aplica os filtros eliminatórios e, se passar, calcula e salva o match."""
    cfg_f = sv.config.obter("filtros")
    cfg_f["palavras_bloqueadas"] = sv.config.obter("filtros.palavras_bloqueadas", [])
    bruta = VagaBruta(
        fonte=vaga.fonte, url=vaga.url, titulo=vaga.titulo, empresa=vaga.empresa, descricao=vaga.descricao,
        local=vaga.local, modelo_trabalho=vaga.modelo_trabalho, labels=json.loads(vaga.labels_json or "[]"))
    res = filtros.avaliar(bruta, cfg_f, assume_remoto=_assume_remoto(sv.config, vaga.fonte))
    vaga.modelo_trabalho = res.modelo_trabalho
    if not res.aprovada:
        vaga.eliminada_motivo = res.motivo
        return None

    s.commit()  # a chamada à LLM grava o log em outra sessão: libera o lock de escrita antes
    req, modo = requisitos_da_vaga(sv, vaga)
    if req.senioridade == "desconhecida" and res.senioridade in ("junior", "pleno"):
        req.senioridade = res.senioridade
    if req.modelo_trabalho == "desconhecido":
        req.modelo_trabalho = res.modelo_trabalho
    elif req.modelo_trabalho in ("hibrido", "presencial"):
        vaga.eliminada_motivo = f"LLM identificou modelo {req.modelo_trabalho}"
        return None
    if req.senioridade == "senior":
        vaga.eliminada_motivo = "LLM identificou senioridade sênior"
        return None

    perfil = score.Perfil(mestre, float(sv.config.obter("match.peso_academico", 0.5)))
    det = score.calcular(req, perfil, sv.config.obter("match.pesos"), preliminar=not vaga.descricao_completa,
                         modo=modo, local=vaga.local)
    vaga.idioma = req.idioma if modo == "llm" else vaga.idioma
    m = vaga.match or Match(vaga_id=vaga.id, score=det.score)
    m.score, m.preliminar, m.modo = det.score, det.preliminar, modo
    m.detalhamento_json = json.dumps(det.como_dict(), ensure_ascii=False)
    s.add(m)
    return m


def gerar_cv(sv: Servicos, s: Session, vaga: Vaga, mestre: Mestre, pessoais: Pessoais) -> CurriculoGerado | None:
    """Gera o PDF da vaga. Levanta ProvedoresEsgotados se não houver LLM disponível."""
    try:
        req = RequisitosVaga.model_validate_json(vaga.requisitos_json) if vaga.requisitos_json else \
            extrair_por_palavras(vaga.titulo, vaga.descricao, json.loads(vaga.labels_json or "[]"))
    except Exception:  # noqa: BLE001
        req = extrair_por_palavras(vaga.titulo, vaga.descricao)
    idioma = vaga.idioma if vaga.idioma in ("pt", "en") else "pt"
    resultado = personalizar(sv.rodizio, mestre, pessoais, req, vaga.titulo, vaga.descricao, idioma)
    for correcao in resultado.conteudo.correcoes:
        registro.log(f"Validação anti-invenção: {correcao}", "aviso", "cv")
    versao = len(vaga.curriculos) + 1
    nome = f"cv_{vaga.id}_v{versao}_{render.slug(vaga.empresa or vaga.titulo)}.pdf"
    destino = sv.config.caminho("saida") / nome
    render.renderizar(resultado.conteudo, pessoais, sv.config.caminho("templates_cv"), destino)
    cv = CurriculoGerado(
        vaga_id=vaga.id, caminho_pdf=str(destino), conteudo_json=resultado.conteudo.model_dump_json(),
        provedor_llm=resultado.provedor, modelo_llm=resultado.modelo, versao=versao)
    s.add(cv)
    s.flush()
    return cv


def processar_pendentes(sv: Servicos) -> tuple[list[str], list[Match]]:
    """Pontua vagas sem match nem eliminação e gera CVs pendentes acima do limite. Devolve (erros, novos matches)."""
    erros: list[str] = []
    novos: list[Match] = []
    mestre, pessoais = sv.mestre(), sv.pessoais()
    limite = float(sv.config.obter("match.limite", 70))
    corte = sv.corte_idade()

    with sv.fabrica() as s:
        ids = s.scalars(select(Vaga.id).where(Vaga.eliminada_motivo.is_(None), ~Vaga.match.has(), _recente(corte))
                        .order_by(Vaga.data_coleta)).all()
    registro.log(f"Pontuação: {len(ids)} vaga(s) pendente(s) (filtros eliminatórios + score).", "info", "match")
    eliminadas = 0
    for i, vid in enumerate(ids):
        with sv.fabrica() as s:
            vaga = s.get(Vaga, vid)
            registro.progresso(_faixa(FAIXA_PONTUACAO, i, len(ids)), f"Pontuando vaga {i + 1}/{len(ids)}: {vaga.titulo[:60]}")
            registro.definir_vaga(vid)
            rotulo = f"Vaga #{vid} «{vaga.titulo[:70]}» ({vaga.fonte})"
            try:
                m = pontuar(sv, s, vaga, mestre)
                s.commit()
                if m is None:
                    eliminadas += 1
                    registro.log(f"{rotulo}: ELIMINADA — {vaga.eliminada_motivo}", "info", "match")
                else:
                    novos.append(m)
                    det = json.loads(m.detalhamento_json or "{}")
                    registro.log(
                        f"{rotulo}: score {m.score:.0f}% ({m.modo}{', preliminar' if m.preliminar else ''}); "
                        f"bateu: {', '.join(det.get('bateram', [])) or '—'}; faltou: {', '.join(det.get('faltaram', [])) or '—'}",
                        "info", "match")
            except Exception as e:  # noqa: BLE001
                s.rollback()
                erros.append(f"pontuar vaga {vid}: {e}")
                registro.log(f"{rotulo}: ERRO ao pontuar — {e}", "erro", "match")
                log.exception("Falha ao pontuar a vaga %s", vid)
            finally:
                registro.definir_vaga(None)
    registro.log(f"Pontuação concluída: {len(novos)} com score, {eliminadas} eliminada(s) pelos filtros.", "info", "match")

    # CVs: vagas recentes, com descrição completa, acima do limite, sem CV ainda e não descartadas
    with sv.fabrica() as s:
        pend = s.scalars(
            select(Vaga.id).join(Match).where(
                Match.score >= limite, Match.status == "novo", Vaga.descricao_completa.is_(True),
                ~Vaga.curriculos.any(), _recente(corte)).order_by(Match.score.desc())).all()
    registro.log(f"Currículos: {len(pend)} vaga(s) acima de {limite:g}% sem CV.", "info", "cv")
    for i, vid in enumerate(pend):
        with sv.fabrica() as s:
            vaga = s.get(Vaga, vid)
            registro.progresso(_faixa(FAIXA_CV, i, len(pend)), f"Gerando CV {i + 1}/{len(pend)}: {vaga.titulo[:60]}")
            registro.definir_vaga(vid)
            try:
                cv = gerar_cv(sv, s, vaga, mestre, pessoais)
                s.commit()
                registro.log(f"Vaga #{vid}: CV v{cv.versao} gerado por {cv.provedor_llm}/{cv.modelo_llm} → "
                             f"{cv.caminho_pdf}", "info", "cv")
            except ProvedoresEsgotados as e:
                registro.log(f"Vaga #{vid}: CV adiado, sem provedor de LLM disponível ({str(e)[:300]}). "
                             f"Restantes ficam para a próxima rodada.", "aviso", "cv")
                break  # sem LLM agora: o resto também fica para a próxima rodada
            except Exception as e:  # noqa: BLE001
                s.rollback()
                erros.append(f"gerar CV da vaga {vid}: {e}")
                registro.log(f"Vaga #{vid}: ERRO ao gerar CV — {e}", "erro", "cv")
                log.exception("Falha ao gerar CV da vaga %s", vid)
            finally:
                registro.definir_vaga(None)
    return erros, novos


def notificar(sv: Servicos) -> int:
    """Avisa no Discord das vagas recentes acima do limite ainda não notificadas."""
    limite = float(sv.config.obter("match.limite", 70))
    corte = sv.corte_idade()
    with sv.fabrica() as s:
        ms = s.scalars(select(Match).join(Vaga).where(
            Match.score >= limite, Match.status == "novo", Match.notificado.is_(False), _recente(corte))
            .order_by(Match.score.desc())).all()
        if not ms:
            registro.log("Notificação: nenhuma vaga nova acima do limite.", "info", "discord")
            return 0
        top = [(m.score, m.vaga.titulo, m.vaga.empresa, m.vaga.fonte)
               for m in ms[: int(sv.config.obter("discord.maximo_top", 3))]]
        enviado = sv.discord.vagas_novas(len(ms), top, sv.config.obter("painel.url_publica", ""))
        if enviado or not sv.discord.ativo:
            for m in ms:
                m.notificado = True
            s.commit()
        registro.log(f"Notificação Discord: {len(ms)} vaga(s) — "
                     f"{'enviada' if enviado else 'NÃO enviada (webhook ausente/inativo ou erro)'}.",
                     "info" if enviado else "aviso", "discord")
        return len(ms)


# ------------------------------------------------------------------ rodada completa
def criar_execucao(sv: Servicos, origem: str) -> int:
    """Cria o registro da execução (para o painel já poder mostrar a barra de progresso)."""
    with sv.fabrica() as s:
        ex = Execucao(origem=origem, status="rodando", etapa="Na fila", progresso=0.0)
        s.add(ex)
        s.commit()
        return ex.id


def iniciar_em_segundo_plano(sv: Servicos, origem: str = "manual") -> int | None:
    """Cria a execução e a roda numa thread. Devolve o id, ou None se já houver outra em andamento."""
    if TRAVA.locked():
        return None
    exec_id = criar_execucao(sv, origem)
    threading.Thread(target=rodar, args=(sv, origem, exec_id), daemon=True, name=f"rodada-{exec_id}").start()
    return exec_id


def rodar(sv: Servicos, origem: str = "agendada", exec_id: int | None = None) -> Execucao | None:
    """Uma rodada completa. Devolve None se já houver outra em andamento."""
    if not TRAVA.acquire(blocking=False):
        log.warning("Já existe uma execução em andamento; ignorando.")
        if exec_id is not None:
            with sv.fabrica() as s:
                ex = s.get(Execucao, exec_id)
                if ex:
                    ex.status, ex.fim, ex.etapa = "erro", agora_utc(), "Outra execução já estava em andamento"
                    s.commit()
        return None
    token = None
    try:
        if exec_id is None:
            exec_id = criar_execucao(sv, origem)
        token = registro.iniciar(sv.fabrica, exec_id)
        registro.log(f"Execução #{exec_id} iniciada ({origem}).", "info", "rodada")
        registro.progresso(1, "Iniciando")
        erros: list[str] = []
        coletadas = novas = 0
        falhou = False
        try:
            coletadas, novas, erros_c = coletar_tudo(sv)
            erros += erros_c
            erros_p, _ = processar_pendentes(sv)
            erros += erros_p
            registro.progresso(96, "Enviando notificação")
            notificar(sv)
        except Exception:  # noqa: BLE001
            falhou = True
            erros.append(traceback.format_exc(limit=3))
            registro.log("Execução interrompida por erro inesperado:\n" + erros[-1], "erro", "rodada")
            sv.discord.erro("Execução com falha", erros[-1])
        with sv.fabrica() as s:
            ex = s.get(Execucao, exec_id)
            ex.fim, ex.vagas_coletadas, ex.vagas_novas = agora_utc(), coletadas, novas
            ex.erros = "\n".join(erros)[:4000]
            ex.status = "erro" if falhou else "concluida"
            ex.progresso = 100.0
            ex.etapa = "Interrompida por erro" if falhou else ("Concluída com avisos" if erros else "Concluída")
            s.commit()
            s.refresh(ex)
        registro.log(f"Execução #{exec_id} terminada: {coletadas} candidata(s), {novas} nova(s), "
                     f"{len(erros)} erro(s).", "erro" if falhou else "info", "rodada")
        return ex
    finally:
        if token is not None:
            registro.encerrar(token)
        TRAVA.release()


# ------------------------------------------------------------------ ações manuais (painel)
def reprocessar_vaga(sv: Servicos, vaga_id: int, descricao: str | None = None) -> str:
    """Re-pontua uma vaga (opcionalmente com a descrição colada pelo usuário) e tenta gerar o CV.

    Devolve uma mensagem curta de resultado para o painel.
    """
    mestre, pessoais = sv.mestre(), sv.pessoais()
    token = registro.iniciar(sv.fabrica, None)  # vincula as chamadas de LLM a esta vaga
    registro.definir_vaga(vaga_id)
    try:
        with sv.fabrica() as s:
            vaga = s.get(Vaga, vaga_id)
            if vaga is None:
                return "Vaga não encontrada."
            if descricao is not None:
                vaga.descricao = descricao.strip()
                vaga.descricao_completa = bool(vaga.descricao)
                vaga.avisos_json = json.dumps(detectar_instrucoes(vaga.descricao), ensure_ascii=False)
                vaga.idioma = detectar_idioma(f"{vaga.titulo}\n{vaga.descricao}")
                vaga.requisitos_json = None
            vaga.eliminada_motivo = None
            if vaga.match is not None and vaga.match.status == "descartado":
                vaga.match.status = "novo"
            s.commit()  # SQLite: não segurar transação de escrita enquanto o rodízio grava em outra sessão
            m = pontuar(sv, s, vaga, mestre)
            s.commit()
            if m is None:
                return f"Vaga eliminada pelos filtros: {vaga.eliminada_motivo}"
            if not vaga.descricao_completa:
                return f"Score preliminar {m.score:.0f}%. Cole a descrição completa para gerar o CV."
            try:
                gerar_cv(sv, s, vaga, mestre, pessoais)
                s.commit()
            except ProvedoresEsgotados as e:
                return f"Score {m.score:.0f}%. CV adiado: {e}"
            except Exception as e:  # noqa: BLE001
                s.rollback()
                return f"Score {m.score:.0f}%, mas o CV falhou: {e}"
            return f"Score {m.score:.0f}%. CV gerado."
    finally:
        registro.encerrar(token)
