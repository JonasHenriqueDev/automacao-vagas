"""Regra das 24 h, deduplicação entre fontes, progresso e log completo da execução."""
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from app.coletores.base import VagaBruta, idade_horas
from app.coletores.email_imap import ColetorEmail
from app.coletores.github_issues import ColetorGithubIssues
from app.db.models import (
    CurriculoGerado,
    Execucao,
    ExecucaoLog,
    FonteEstado,
    LlmChamada,
    Match,
    Vaga,
    agora_utc,
)
from app.pipeline import (
    TRAVA,
    iniciar_em_segundo_plano,
    notificar,
    processar_pendentes,
    rodar,
    salvar_se_nova,
)
from tests.test_coletores import FakeIMAP, HTML_LINKEDIN, _email_bruto
from tests.test_pipeline import ISSUES, _issue, preparar, servidor_falso


def test_github_ignora_vagas_antigas_mesmo_que_atualizadas_hoje():
    """Regressão: vaga de 2025 comentada hoje não pode aparecer (idade = data de criação)."""
    paginas = []

    def handler(req):
        paginas.append(int(req.url.params["page"]))
        return httpx.Response(200, json=[
            _issue(11, "[Remoto] Dev Backend Pleno na Nova", ["Remoto"], "", horas_atras=3),
            _issue(10, "[Remoto] Dev PHP/Laravel (Pleno) na Empreender", ["Remoto"], "", horas_atras=24 * 300),
        ])

    col = ColetorGithubIssues("backend-br/vagas", ["Remoto"], [], idade_maxima_horas=24, max_paginas=3,
                              cliente=httpx.Client(transport=httpx.MockTransport(handler)))
    vagas = col.coletar()
    assert [v.url.rsplit("/", 1)[1] for v in vagas] == ["11"]
    assert col.stats["antigas"] == 1 and paginas == [1]  # parou de paginar ao chegar nas antigas


def test_idade_horas():
    agora = datetime(2026, 10, 16, 12, 0)
    assert idade_horas(agora - timedelta(hours=5), agora) == 5
    assert idade_horas(None, agora) is None
    assert idade_horas(agora + timedelta(hours=1), agora) == 0  # data futura não vira idade negativa


def test_alerta_de_email_recebe_a_data_do_email_como_publicacao():
    imap = FakeIMAP({1: _email_bruto("LinkedIn <jobalerts-noreply@linkedin.com>", HTML_LINKEDIN)})
    vagas = ColetorEmail("imap.x", 993, "u", "s", conexao_factory=lambda: imap).coletar()
    assert vagas and all(isinstance(v.data_publicacao, datetime) for v in vagas)


def test_dedupe_por_empresa_e_titulo_entre_fontes(fabrica_servicos):
    sv = fabrica_servicos()
    with sv.fabrica() as s:
        assert salvar_se_nova(s, VagaBruta("github:x", "https://github.com/x/1", "Dev Backend Pleno", "Acme")) is not None
        # mesma vaga vinda por outra fonte/URL
        assert salvar_se_nova(s, VagaBruta("email:linkedin", "https://linkedin.com/jobs/view/9/",
                                           "dev backend pleno", "ACME")) is None
        # empresa desconhecida não pode colidir só pelo título
        assert salvar_se_nova(s, VagaBruta("github:x", "https://github.com/x/2", "Dev Backend Pleno", "")) is not None
        s.commit()


def test_vagas_com_mais_de_24h_nao_sao_gravadas(fabrica_servicos, monkeypatch):
    """O caso real: '[Remoto] Desenvolvedor PHP/Laravel (Pleno) na Empreender' de 2025 não pode aparecer."""
    velha = _issue(50, "[Remoto] Desenvolvedor PHP/Laravel (Pleno) na Empreender", ["Remoto", "Pleno"],
                   "Requisitos: PHP, Laravel", horas_atras=24 * 330)

    def h(req):
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=[ISSUES[0], velha])
        return servidor_falso()(req)

    sv = preparar(fabrica_servicos, monkeypatch, h)
    ex = rodar(sv)
    assert ex.vagas_novas == 1
    with sv.fabrica() as s:
        assert not any("Empreender" in v.titulo or "Empreender" in v.url for v in s.scalars(select(Vaga)).all())


def test_idade_maxima_e_configuravel(fabrica_servicos, monkeypatch):
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso())
    sv.config.definir("filtros.idade_maxima_horas", 1)  # as issues de teste têm 2 h
    assert rodar(sv).vagas_novas == 0
    sv.config.definir("filtros.idade_maxima_horas", 0)  # sem limite
    with sv.fabrica() as s:
        s.get(FonteEstado, "github:backend-br/vagas").ultima_execucao = None
        s.commit()
    assert rodar(sv).vagas_novas == 3


def test_vaga_gravada_que_envelheceu_sai_de_cv_e_notificacao(fabrica_servicos, monkeypatch):
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso())
    rodar(sv)
    with sv.fabrica() as s:
        for v in s.scalars(select(Vaga)).all():
            v.data_publicacao = agora_utc() - timedelta(hours=40)
        for m in s.scalars(select(Match)).all():
            m.notificado = False
        for cv in s.scalars(select(CurriculoGerado)).all():
            s.delete(cv)
        s.commit()
    erros, novos = processar_pendentes(sv)
    assert not erros and novos == [] and notificar(sv) == 0
    with sv.fabrica() as s:
        assert s.scalars(select(CurriculoGerado)).all() == []  # não gera CV para vaga velha


def test_execucao_tem_progresso_logs_completos_e_chamadas_de_llm_vinculadas(fabrica_servicos, monkeypatch):
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso())
    ex = rodar(sv)
    assert ex.status == "concluida" and ex.progresso == 100 and ex.etapa == "Concluída"
    with sv.fabrica() as s:
        logs = s.scalars(select(ExecucaoLog).where(ExecucaoLog.execucao_id == ex.id).order_by(ExecucaoLog.id)).all()
        assert {"rodada", "coleta", "match", "cv", "llm", "discord"} <= {l.etapa for l in logs}
        texto = "\n".join(l.mensagem for l in logs)
        assert "NOVA(S)" in texto and "ELIMINADA" in texto and "score" in texto and "CV v1 gerado" in texto
        assert any(l.mensagem.startswith("LLM groq/") and "OK em" in l.mensagem for l in logs)
        chamadas = s.scalars(select(LlmChamada)).all()
        assert chamadas and all(c.execucao_id == ex.id for c in chamadas)
        assert any(c.vaga_id is not None for c in chamadas) and all(c.duracao_ms >= 0 for c in chamadas)


def test_falha_de_llm_fica_registrada_com_tipo_e_http_status(fabrica_servicos, monkeypatch):
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso(llm_ok=False))
    rodar(sv)
    with sv.fabrica() as s:
        falhas = s.scalars(select(LlmChamada).where(LlmChamada.sucesso.is_(False))).all()
        assert falhas and all(c.tipo_erro == "cota" and c.http_status == 429 and "quota" in c.erro for c in falhas)


def test_iniciar_em_segundo_plano_devolve_id_e_respeita_a_trava(fabrica_servicos, monkeypatch):
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso())
    assert TRAVA.acquire(blocking=False)
    try:
        assert iniciar_em_segundo_plano(sv) is None
    finally:
        TRAVA.release()
    eid = iniciar_em_segundo_plano(sv)
    assert isinstance(eid, int)
    for _ in range(150):
        with sv.fabrica() as s:
            if s.get(Execucao, eid).status != "rodando":
                break
        time.sleep(0.1)
    with sv.fabrica() as s:
        ex = s.get(Execucao, eid)
        assert ex.status == "concluida" and ex.progresso == 100
