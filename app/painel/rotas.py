"""Rotas do painel: revisão de vagas, candidaturas, configurações, integrações e saúde."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..agendador import validar_cron
from ..config import SEGREDOS_CONHECIDOS
from ..coletores.base import USER_AGENT, idade_horas
from ..coletores.ats import parse_quadro
from ..coletores.email_imap import ColetorEmail
from ..db.models import (
    Aplicacao,
    CurriculoGerado,
    Execucao,
    LlmChamada,
    LlmProvedorEstado,
    Match,
    Vaga,
    agora_utc,
)
from ..pipeline import Servicos, iniciar_em_segundo_plano, reprocessar_vaga
from .auth import (
    checar_csrf,
    exigir_login,
    tem_credenciais,
    token_csrf,
    verificar,
)

AQUI = Path(__file__).parent
templates = Jinja2Templates(directory=str(AQUI / "templates"))
router = APIRouter()

#: fuso usado para mostrar horários no painel (o banco guarda UTC)
FUSO_PADRAO = "America/Sao_Paulo"


def idade_texto(dt: datetime | None) -> str:
    """'há 45 min', 'há 3 h 20 min', 'há 2 dias': quanto tempo faz que a vaga foi aberta."""
    h = idade_horas(dt)
    if h is None:
        return "data desconhecida"
    minutos = int(h * 60)
    if minutos < 60:
        return f"há {max(minutos, 1)} min"
    if h < 24:
        return f"há {int(h)} h" + (f" {minutos % 60} min" if minutos % 60 else "")
    dias = int(h // 24)
    return f"há {dias} dia{'s' if dias > 1 else ''}"


def hora_local(dt: datetime | None, formato: str = "%d/%m %H:%M") -> str:
    if dt is None:
        return "—"
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(FUSO_PADRAO)).strftime(formato)


def duracao(inicio: datetime | None, fim: datetime | None) -> str:
    if inicio is None:
        return "—"
    seg = int(((fim or agora_utc()) - inicio).total_seconds())
    return f"{seg // 60} min {seg % 60:02d} s" if seg >= 60 else f"{seg} s"


templates.env.filters["idade"] = idade_texto
templates.env.filters["hl"] = hora_local
templates.env.filters["duracao"] = duracao

STATUS_APLICACAO = ["aplicado", "entrevista", "recusado", "oferta"]


def sv_de(request: Request) -> Servicos:
    return request.app.state.sv


def contexto(request: Request, **extra) -> dict:
    flash = request.session.pop("flash", None)
    return {"request": request, "csrf": token_csrf(request), "flash": flash,
            "usuario": request.session.get("usuario"), **extra}


def pagina(request: Request, nome: str, **extra) -> HTMLResponse:
    return templates.TemplateResponse(request, nome, contexto(request, **extra))


def avisar(request: Request, msg: str) -> None:
    request.session["flash"] = msg


def voltar(destino: str = "/") -> RedirectResponse:
    return RedirectResponse(destino, status_code=303)


# ------------------------------------------------------------------ login
@router.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return pagina(request, "login.html", erro=None, sem_credenciais=not tem_credenciais(sv_de(request)))


@router.post("/login", dependencies=[Depends(checar_csrf)])
def login(request: Request, usuario: str = Form(""), senha: str = Form("")):
    ip = request.client.host if request.client else "?"
    limite = request.app.state.limite_login
    if limite.bloqueado(ip):
        return templates.TemplateResponse(
            request, "login.html",
            contexto(request, erro="Muitas tentativas. Aguarde alguns minutos.", sem_credenciais=False),
            status_code=429)
    if not verificar(sv_de(request), usuario, senha):
        limite.falhou(ip)
        return templates.TemplateResponse(
            request, "login.html",
            contexto(request, erro="Usuário ou senha incorretos.", sem_credenciais=False), status_code=401)
    limite.limpar(ip)
    csrf_antigo = request.session.get("csrf")
    request.session.clear()  # nova sessão após o login
    request.session["usuario"] = usuario.strip()
    request.session["csrf"] = csrf_antigo or ""
    return voltar("/")


@router.post("/logout", dependencies=[Depends(checar_csrf)])
def logout(request: Request):
    request.session.clear()
    return voltar("/login")


# ------------------------------------------------------------------ revisão
def metricas(s: Session, limite: float) -> dict:
    agora = agora_utc()
    sete = agora - timedelta(days=7)
    por_dia = dict(s.execute(
        select(func.date(Vaga.data_coleta), func.count()).where(Vaga.data_coleta >= sete)
        .group_by(func.date(Vaga.data_coleta))).all())
    aplicadas = s.scalar(select(func.count()).select_from(Aplicacao).where(Aplicacao.data >= sete)) or 0
    total_ap = s.scalar(select(func.count()).select_from(Aplicacao)) or 0
    resp = s.scalar(select(func.count()).select_from(Aplicacao)
                    .where(Aplicacao.status.in_(["entrevista", "recusado", "oferta"]))) or 0
    return {
        "coletadas_hoje": por_dia.get(agora.strftime("%Y-%m-%d"), 0),
        "coletadas_7d": sum(por_dia.values()),
        "acima_limite": s.scalar(select(func.count()).select_from(Match).where(Match.score >= limite)) or 0,
        "aplicadas_semana": aplicadas,
        "taxa_resposta": round(100 * resp / total_ap) if total_ap else None,
        "por_dia": sorted(por_dia.items()),
    }


def _avisos(v: Vaga) -> list[str]:
    try:
        return json.loads(v.avisos_json or "[]")
    except json.JSONDecodeError:
        return []


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(exigir_login)])
def revisar(request: Request, todas: int = 0, status: str = "novo", antigas: int = 0):
    """Lista de revisão. Por padrão só vagas abertas há no máximo `filtros.idade_maxima_horas` (24 h)."""
    sv = sv_de(request)
    limite = float(sv.config.obter("match.limite", 70))
    maxima = sv.idade_maxima_horas()
    if status not in ("novo", "descartado"):
        status = "novo"
    with sv.fabrica() as s:
        q = select(Match).join(Vaga).where(Match.status == status)
        if status == "novo":
            if not todas:
                q = q.where(Match.score >= limite)
            if not antigas and maxima:
                q = q.where(or_(Vaga.data_publicacao.is_(None), Vaga.data_publicacao >= sv.corte_idade()))
        # mais novas primeiro; dentro da mesma idade, maior score
        q = q.order_by(Vaga.data_publicacao.desc(), Match.score.desc()).limit(150)
        itens = []
        for m in s.scalars(q).all():
            det = json.loads(m.detalhamento_json or "{}")
            itens.append({"m": m, "v": m.vaga, "det": det, "avisos": _avisos(m.vaga),
                          "cv": m.vaga.curriculos[-1] if m.vaga.curriculos else None})
        met = metricas(s, limite)
        return pagina(request, "revisar.html", itens=itens, met=met, limite=limite, todas=todas, status=status,
                      antigas=antigas, maxima=maxima)


@router.get("/vaga/{vaga_id}", response_class=HTMLResponse, dependencies=[Depends(exigir_login)])
def detalhe(request: Request, vaga_id: int):
    sv = sv_de(request)
    with sv.fabrica() as s:
        v = s.get(Vaga, vaga_id)
        if v is None:
            raise HTTPException(404, "Vaga não encontrada.")
        det = json.loads(v.match.detalhamento_json) if v.match else {}
        req = json.loads(v.requisitos_json) if v.requisitos_json else None
        cv_conteudo = json.loads(v.curriculos[-1].conteudo_json) if v.curriculos else None
        return pagina(request, "vaga.html", v=v, m=v.match, det=det, req=req, avisos=_avisos(v),
                      curriculos=v.curriculos, cv_conteudo=cv_conteudo, aplicacoes=v.aplicacoes,
                      labels=json.loads(v.labels_json or "[]"))


@router.post("/vaga/{vaga_id}/aplicar", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def aplicar(request: Request, vaga_id: int):
    """Só marca a vaga como aplicada (status 'aplicado'); não abre nem redireciona para o site da vaga."""
    sv = sv_de(request)
    with sv.fabrica() as s:
        v = s.get(Vaga, vaga_id)
        if v is None:
            raise HTTPException(404, "Vaga não encontrada.")
        if v.match:
            v.match.status = "aprovado"
        if not v.aplicacoes:
            s.add(Aplicacao(vaga_id=v.id, curriculo_id=v.curriculos[-1].id if v.curriculos else None,
                            canal="site", status="aplicado"))
        s.commit()
    if request.headers.get("hx-request"):
        return HTMLResponse("")  # a vaga sai da fila de revisão
    avisar(request, "Vaga marcada como aplicada.")
    return voltar(f"/vaga/{vaga_id}")


@router.post("/vaga/{vaga_id}/descartar", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def descartar(request: Request, vaga_id: int):
    sv = sv_de(request)
    with sv.fabrica() as s:
        v = s.get(Vaga, vaga_id)
        if v is None or v.match is None:
            raise HTTPException(404, "Vaga não encontrada.")
        v.match.status = "descartado"
        s.commit()
    if request.headers.get("hx-request"):
        return HTMLResponse("")
    return voltar("/")


@router.post("/vaga/{vaga_id}/restaurar", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def restaurar(request: Request, vaga_id: int):
    sv = sv_de(request)
    with sv.fabrica() as s:
        v = s.get(Vaga, vaga_id)
        if v is None or v.match is None:
            raise HTTPException(404, "Vaga não encontrada.")
        v.match.status = "novo"
        s.commit()
    if request.headers.get("hx-request"):
        return HTMLResponse("")
    return voltar("/?status=descartado")


@router.post("/vaga/{vaga_id}/descricao", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def colar_descricao(request: Request, vaga_id: int, descricao: str = Form("")):
    if len(descricao.strip()) < 80:
        avisar(request, "Cole a descrição completa da vaga (mínimo de 80 caracteres).")
        return voltar(f"/vaga/{vaga_id}")
    avisar(request, reprocessar_vaga(sv_de(request), vaga_id, descricao[:30000]))
    return voltar(f"/vaga/{vaga_id}")


@router.get("/cv/{cv_id}.pdf", dependencies=[Depends(exigir_login)])
def baixar_cv(request: Request, cv_id: int):
    sv = sv_de(request)
    with sv.fabrica() as s:
        cv = s.get(CurriculoGerado, cv_id)
        if cv is None:
            raise HTTPException(404, "Currículo não encontrado.")
        caminho = Path(cv.caminho_pdf).resolve()
    saida = sv.config.caminho("saida").resolve()
    if saida not in caminho.parents or not caminho.is_file():  # nunca serve arquivo fora da pasta de saída
        raise HTTPException(404, "Arquivo do currículo indisponível.")
    return FileResponse(caminho, media_type="application/pdf", filename=caminho.name,
                        content_disposition_type="inline")


# ------------------------------------------------------------------ candidaturas
@router.get("/candidaturas", response_class=HTMLResponse, dependencies=[Depends(exigir_login)])
def candidaturas(request: Request):
    sv = sv_de(request)
    with sv.fabrica() as s:
        itens = s.scalars(select(Aplicacao).order_by(Aplicacao.data.desc())).all()
        linhas = [{"a": a, "v": a.vaga} for a in itens]
        return pagina(request, "candidaturas.html", linhas=linhas, status_opcoes=STATUS_APLICACAO)


@router.post("/candidaturas/{app_id}", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def atualizar_candidatura(request: Request, app_id: int, status: str = Form(...), notas: str = Form("")):
    if status not in STATUS_APLICACAO:
        raise HTTPException(400, "Status inválido.")
    sv = sv_de(request)
    with sv.fabrica() as s:
        a = s.get(Aplicacao, app_id)
        if a is None:
            raise HTTPException(404, "Candidatura não encontrada.")
        a.status, a.notas = status, notas[:4000]
        s.commit()
    avisar(request, "Candidatura atualizada.")
    return voltar("/candidaturas")


# ------------------------------------------------------------------ configurações
def _ranking_do_form(form, tarefa: str, atual: list[dict], validos: set[str]) -> list[dict]:
    itens = []
    for i, item in enumerate(atual):
        if item["provedor"] not in validos:
            continue
        modelo = str(form.get(f"{tarefa}_{i}_modelo", item["modelo"])).strip()[:120] or item["modelo"]
        try:
            ordem = int(form.get(f"{tarefa}_{i}_ordem", i + 1))
        except ValueError:
            ordem = i + 1
        itens.append((ordem, i, {"provedor": item["provedor"], "modelo": modelo,
                                 "ativo": form.get(f"{tarefa}_{i}_ativo") == "on"}))
    return [x[2] for x in sorted(itens, key=lambda t: (t[0], t[1]))]


#: (chave em `fontes.*`, rótulo, só roda com "incluir internacionais" ligado)
FONTES_UI = [
    ("gupy", "Gupy (vagas brasileiras)", False),
    ("quadros", "Quadros de empresas (Greenhouse, Lever, Ashby)", False),
    ("programathor", "ProgramaThor (sem data nem descrição completa)", False),
    ("vagas_com", "Vagas.com", False),
    ("himalayas", "Himalayas (as do Brasil sempre; o resto só com internacionais)", False),
    ("remotive", "Remotive", True),
    ("remoteok", "RemoteOK", True),
    ("jobicy", "Jobicy", True),
    ("arbeitnow", "Arbeitnow", True),
    ("weworkremotely", "We Work Remotely", True),
]


@router.get("/configuracoes", response_class=HTMLResponse, dependencies=[Depends(exigir_login)])
def configuracoes(request: Request):
    sv, ag = sv_de(request), request.app.state.agendador
    cfg = sv.config
    repos = cfg.obter("fontes.github_issues.repos") or []
    return pagina(
        request, "configuracoes.html",
        cron=cfg.obter("agendamento.cron"), fuso=cfg.obter("agendamento.fuso"),
        proxima=ag.proxima_execucao(), limite=cfg.obter("match.limite"), pesos=cfg.obter("match.pesos"),
        bloqueadas="\n".join(cfg.obter("filtros.palavras_bloqueadas") or []),
        idade_max=cfg.obter("filtros.idade_maxima_horas", 24),
        repos="\n".join(r["repo"] + (" | " + ", ".join(r.get("labels", [])) if r.get("labels") else "")
                        for r in repos),
        incluir_internacional=bool(cfg.obter("fontes.incluir_internacional", False)),
        fontes_ui=[{"chave": chave, "rotulo": rotulo, "internacional": inter,
                    "ativo": bool(cfg.obter(f"fontes.{chave}.ativo", True))}
                   for chave, rotulo, inter in FONTES_UI],
        gupy_termos="\n".join(cfg.obter("fontes.gupy.termos") or []),
        vagas_termos="\n".join(cfg.obter("fontes.vagas_com.termos") or []),
        quadros="\n".join(cfg.obter("fontes.quadros.lista") or []),
        rankings={t: cfg.obter(f"llm.ranking.{t}") or [] for t in ("reescrita_cv", "extracao")},
        titulos={"reescrita_cv": "Reescrita do CV", "extracao": "Extração de requisitos"},
        rodando=_rodando())


def _rodando() -> bool:
    from ..pipeline import TRAVA
    return TRAVA.locked()


@router.post("/configuracoes/agendamento", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def salvar_agendamento(request: Request, cron: str = Form(...)):
    ag = request.app.state.agendador
    try:
        validar_cron(cron.strip(), ag.fuso)
        prox = ag.reagendar(cron.strip()) if ag.sched.running else None
        if not ag.sched.running:
            sv_de(request).config.definir("agendamento.cron", cron.strip())
        avisar(request, f"Agendamento salvo. Próxima execução: {prox:%d/%m %H:%M}" if prox else "Agendamento salvo.")
    except ValueError as e:
        avisar(request, f"Cron não salvo: {e}")
    return voltar("/configuracoes")


@router.post("/configuracoes/match", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
async def salvar_match(request: Request):
    form = await request.form()
    cfg = sv_de(request).config
    try:
        limite = float(str(form.get("limite", "70")).replace(",", "."))
        pesos = {k: float(str(form.get(f"peso_{k}", "0")).replace(",", "."))
                 for k in ("obrigatorios", "desejaveis", "senioridade", "local_salario")}
        if not 0 <= limite <= 100 or any(p < 0 for p in pesos.values()) or sum(pesos.values()) <= 0:
            raise ValueError
    except ValueError:
        avisar(request, "Valores inválidos: limite entre 0 e 100 e pesos não negativos com soma maior que zero.")
        return voltar("/configuracoes")
    cfg.definir("match.limite", limite)
    cfg.definir("match.pesos", pesos)
    avisar(request, "Limite e pesos salvos.")
    return voltar("/configuracoes")


@router.post("/configuracoes/fontes", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
async def salvar_fontes(request: Request):
    form = await request.form()
    cfg = sv_de(request).config
    repos = []
    for linha in str(form.get("repos", "")).splitlines():
        if not linha.strip():
            continue
        nome, _, labels = linha.partition("|")
        nome = nome.strip()
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", nome):
            avisar(request, f"Repositório inválido: {nome!r} (use dono/repositório).")
            return voltar("/configuracoes")
        repos.append({"repo": nome, "labels": [l.strip() for l in labels.split(",") if l.strip()]})
    def linhas(campo: str) -> list[str]:
        return [x.strip() for x in str(form.get(campo, "")).splitlines() if x.strip()]

    termos_gupy, termos_vagas, quadros = linhas("gupy_termos"), linhas("vagas_termos"), linhas("quadros")
    if any(len(t) > 60 for t in termos_gupy + termos_vagas) or len(termos_gupy) > 20 or len(termos_vagas) > 20:
        avisar(request, "Termos de busca inválidos: até 20 termos de no máximo 60 caracteres.")
        return voltar("/configuracoes")
    invalidos = [q for q in quadros if parse_quadro(q) is None]
    if invalidos:
        avisar(request, f"Quadro inválido: {invalidos[0]!r} (use plataforma:nome, com plataforma greenhouse, lever ou ashby).")
        return voltar("/configuracoes")
    try:
        idade = int(str(form.get("idade_max", "24")).strip() or "24")
        if not 0 <= idade <= 24 * 365:
            raise ValueError
    except ValueError:
        avisar(request, "Idade máxima inválida: use um número de horas entre 0 e 8760.")
        return voltar("/configuracoes")
    cfg.definir("filtros.idade_maxima_horas", idade)
    cfg.definir("fontes.github_issues.repos", repos)
    cfg.definir("fontes.incluir_internacional", form.get("incluir_internacional") == "on")
    for chave, _rotulo, _inter in FONTES_UI:
        cfg.definir(f"fontes.{chave}.ativo", form.get(f"fonte_{chave}") == "on")
    cfg.definir("fontes.gupy.termos", termos_gupy)
    cfg.definir("fontes.vagas_com.termos", termos_vagas)
    cfg.definir("fontes.quadros.lista", [f"{p}:{n}" for p, n in map(parse_quadro, quadros)])
    cfg.definir("filtros.palavras_bloqueadas",
                [p.strip() for p in str(form.get("bloqueadas", "")).splitlines() if p.strip()])
    avisar(request, "Fontes e filtros salvos.")
    return voltar("/configuracoes")


@router.post("/configuracoes/ranking", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
async def salvar_ranking(request: Request):
    form = await request.form()
    cfg = sv_de(request).config
    validos = set((cfg.obter("llm.provedores") or {}).keys())
    for tarefa in ("reescrita_cv", "extracao"):
        atual = cfg.obter(f"llm.ranking.{tarefa}") or []
        cfg.definir(f"llm.ranking.{tarefa}", _ranking_do_form(form, tarefa, atual, validos))
    avisar(request, "Ranking de provedores salvo.")
    return voltar("/configuracoes")


@router.post("/rodar-agora", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def rodar_agora(request: Request):
    """Inicia uma rodada em segundo plano e leva para a tela com a barra de progresso e o log ao vivo."""
    exec_id = iniciar_em_segundo_plano(sv_de(request), "manual")
    if exec_id is None:
        avisar(request, "Já existe uma execução em andamento.")
        with sv_de(request).fabrica() as s:
            atual = s.scalar(select(Execucao.id).where(Execucao.status == "rodando").order_by(Execucao.id.desc()))
        return voltar(f"/execucoes/{atual}" if atual else "/execucoes")
    return voltar(f"/execucoes/{exec_id}")


# ------------------------------------------------------------------ integrações
@router.get("/integracoes", response_class=HTMLResponse, dependencies=[Depends(exigir_login)])
def integracoes(request: Request):
    sv = sv_de(request)
    cfg = sv.config
    provedores = cfg.obter("llm.provedores") or {}
    segredos = [{"nome": n, "final": cfg.segredos.final(n), "env": cfg.segredos.no_ambiente(n)}
                for n in SEGREDOS_CONHECIDOS]
    from .rotas_execucao import resumo_provedores  # import tardio: evita ciclo entre os módulos de rotas
    with sv.fabrica() as s:
        estados = {e.provedor: e for e in s.scalars(select(LlmProvedorEstado)).all()}
        resumo = resumo_provedores(s)
    return pagina(
        request, "integracoes.html", segredos=segredos, provedores=provedores, estados=estados, resumo=resumo,
        pode_gravar=cfg.segredos.pode_gravar,
        email={k: cfg.obter(f"fontes.email.{k}") for k in ("ativo", "servidor", "porta", "usuario", "pasta")},
        discord_ativo=cfg.obter("discord.ativo", True))


@router.post("/integracoes/segredo", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def salvar_segredo(request: Request, nome: str = Form(...), valor: str = Form(""), remover: str = Form("")):
    sv = sv_de(request)
    if nome not in SEGREDOS_CONHECIDOS:
        raise HTTPException(400, "Segredo desconhecido.")
    try:
        if remover:
            sv.config.segredos.remover(nome)
            avisar(request, f"{nome} removido do painel.")
        elif valor.strip():
            sv.config.segredos.gravar(nome, valor.strip())
            avisar(request, f"{nome} salvo (criptografado).")
    except RuntimeError as e:
        avisar(request, str(e))
    return voltar("/integracoes")


@router.post("/integracoes/email", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
async def salvar_email(request: Request):
    form = await request.form()
    cfg = sv_de(request).config
    try:
        porta = int(str(form.get("porta", "993")))
        if not 1 <= porta <= 65535:
            raise ValueError
    except ValueError:
        avisar(request, "Porta inválida.")
        return voltar("/integracoes")
    cfg.definir("fontes.email.servidor", str(form.get("servidor", "")).strip())
    cfg.definir("fontes.email.porta", porta)
    cfg.definir("fontes.email.usuario", str(form.get("usuario", "")).strip())
    cfg.definir("fontes.email.pasta", str(form.get("pasta", "INBOX")).strip() or "INBOX")
    cfg.definir("fontes.email.ativo", form.get("ativo") == "on")
    avisar(request, "Conta de e-mail salva. A senha de app é salva em EMAIL_SENHA (acima).")
    return voltar("/integracoes")


@router.post("/integracoes/discord", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
async def salvar_discord(request: Request):
    form = await request.form()
    sv_de(request).config.definir("discord.ativo", form.get("ativo") == "on")
    avisar(request, "Preferência de notificação salva.")
    return voltar("/integracoes")


def _resultado(ok: bool, msg: str) -> HTMLResponse:
    from html import escape
    return HTMLResponse(f'<span class="{"ok" if ok else "erro"}">{escape(msg)}</span>')


@router.post("/integracoes/testar/{alvo}", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def testar(request: Request, alvo: str, provedor: str = Form("")):
    sv = sv_de(request)
    cfg = sv.config
    try:
        if alvo == "llm":
            cliente = sv.rodizio.cliente(provedor)
            if cliente is None:
                return _resultado(False, "Provedor inativo, sem chave ou sem URL configurada.")
            try:
                modelos = cliente.listar_modelos()
            finally:
                cliente.close()
            return _resultado(True, f"Chave válida. {len(modelos)} modelos: " + ", ".join(modelos[:12])
                              + ("…" if len(modelos) > 12 else ""))
        if alvo == "discord":
            return _resultado(True, sv.discord.testar())
        if alvo == "email":
            senha = cfg.segredos.obter("EMAIL_SENHA")
            e = {k: cfg.obter(f"fontes.email.{k}") for k in ("servidor", "porta", "usuario", "pasta")}
            if not (e["usuario"] and senha):
                return _resultado(False, "Informe o usuário e a senha de app (EMAIL_SENHA).")
            return _resultado(True, ColetorEmail(e["servidor"], int(e["porta"]), e["usuario"], senha,
                                                 e["pasta"] or "INBOX").testar())
        if alvo == "github":
            token = cfg.segredos.obter("GITHUB_TOKEN")
            if not token:
                return _resultado(False, "GITHUB_TOKEN não configurado.")
            r = httpx.get("https://api.github.com/rate_limit", timeout=15,
                          headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
            r.raise_for_status()
            lim = r.json()["resources"]["core"]
            return _resultado(True, f"Token válido. Limite: {lim['remaining']}/{lim['limit']} requisições/hora.")
    except Exception as e:  # noqa: BLE001
        return _resultado(False, f"Falhou: {e}"[:300])
    raise HTTPException(404, "Teste desconhecido.")


@router.post("/integracoes/reativar", dependencies=[Depends(exigir_login), Depends(checar_csrf)])
def reativar(request: Request, provedor: str = Form(...)):
    sv_de(request).rodizio.reativar(provedor)
    avisar(request, f"{provedor} reativado.")
    return voltar("/integracoes")


# ------------------------------------------------------------------ saúde
@router.get("/saude", response_class=HTMLResponse, dependencies=[Depends(exigir_login)])
def saude(request: Request):
    sv, ag = sv_de(request), request.app.state.agendador
    sete = agora_utc() - timedelta(days=7)
    with sv.fabrica() as s:
        execs = s.scalars(select(Execucao).order_by(Execucao.inicio.desc()).limit(15)).all()
        uso = s.execute(
            select(LlmChamada.provedor, func.count(), func.sum(LlmChamada.sucesso),
                   func.coalesce(func.sum(LlmChamada.tokens_in), 0), func.coalesce(func.sum(LlmChamada.tokens_out), 0))
            .where(LlmChamada.criado_em >= sete).group_by(LlmChamada.provedor)).all()
        erros = s.scalars(select(LlmChamada).where(LlmChamada.sucesso.is_(False))
                          .order_by(LlmChamada.criado_em.desc()).limit(10)).all()
        estados = s.scalars(select(LlmProvedorEstado)).all()
        return pagina(request, "saude.html", execs=execs, uso=uso, erros=erros, estados=estados,
                      proxima=ag.proxima_execucao(), rodando=_rodando(), agora=agora_utc())
