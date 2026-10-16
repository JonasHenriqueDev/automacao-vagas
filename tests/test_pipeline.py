import json
from pathlib import Path

import httpx
from sqlalchemy import select

from app.db.models import CurriculoGerado, Execucao, Match, Vaga
from app.pipeline import rodar
from tests.conftest import resposta_chat
from tests.test_curriculo import saida_valida

DESCRICAO_BOA = """Empresa: Acme Pagamentos
Buscamos pessoa desenvolvedora backend (pleno) 100% remota.
Requisitos:
- Node.js, NestJS e PostgreSQL
- Integração com Stripe e webhooks
- Docker e Redis
Diferenciais:
- AWS
"""


def _issue(n, titulo, labels, corpo, horas_atras=2):
    from datetime import datetime, timedelta, timezone
    criada = (datetime.now(timezone.utc) - timedelta(hours=horas_atras)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"number": n, "title": titulo, "body": corpo, "html_url": f"https://github.com/backend-br/vagas/issues/{n}",
            "created_at": criada, "labels": [{"name": l} for l in labels]}


ISSUES = [
    _issue(1, "[Remoto] Desenvolvedor Backend Pleno", ["Remoto", "Pleno"], DESCRICAO_BOA),
    _issue(2, "[Remoto] Desenvolvedor Backend Sênior", ["Remoto", "Sênior"], "Node.js"),  # excluída pela label
    _issue(3, "Desenvolvedor Backend Pleno (Presencial)", ["Pleno"], "Vaga presencial em SP. Node.js"),
    _issue(4, "[Remoto] Dev Go Pleno", ["Remoto", "Pleno"], "Requisitos: Go, Kubernetes, Terraform, gRPC"),
]

EXTRACAO = {"obrigatorios": ["Node.js", "NestJS", "PostgreSQL", "Stripe", "Docker", "Redis"], "desejaveis": ["AWS"],
            "senioridade": "pleno", "modelo_trabalho": "remoto", "local": "", "salario": "", "idioma": "pt"}


def servidor_falso(llm_ok=True, extracao=EXTRACAO, chamadas=None):
    def h(req: httpx.Request) -> httpx.Response:
        if chamadas is not None:
            chamadas.append(req.url.host)
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=ISSUES)
        if req.url.host == "discord.com":
            return httpx.Response(204)
        if not llm_ok:
            return httpx.Response(429, json={"error": "quota"})
        corpo = json.loads(req.content)
        sistema = corpo["messages"][0]["content"]
        if "extrai requisitos" in sistema:
            # a vaga 4 (Go/Kubernetes) tem outro perfil
            usuario = corpo["messages"][1]["content"]
            if "Kubernetes" in usuario:
                return resposta_chat({**extracao, "obrigatorios": ["Go", "Kubernetes", "Terraform", "gRPC"],
                                      "desejaveis": []})
            return resposta_chat(extracao)
        return resposta_chat(saida_valida())

    return h


def preparar(fabrica_servicos, monkeypatch, handler, ajustes=None):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    base = {"fontes": {"github_issues": {"ativo": True, "repos": [{"repo": "backend-br/vagas", "labels": ["Remoto"]}],
                                         "labels_excluir": ["Sênior"], "max_paginas": 1, "min_intervalo_horas": 3,
                                         "ativo ": True}}}
    base["fontes"]["github_issues"].pop("ativo ")
    return fabrica_servicos(handler, ajustes or base)


def test_rodada_completa_gera_vagas_matches_e_cv(fabrica_servicos, monkeypatch):
    hosts = []
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso(chamadas=hosts))
    ex = rodar(sv)
    assert ex is not None and not ex.erros, ex.erros
    assert ex.vagas_coletadas == 3 and ex.vagas_novas == 3  # a sênior foi barrada pela label no coletor

    with sv.fabrica() as s:
        vagas = {v.url.rsplit("/", 1)[1]: v for v in s.scalars(select(Vaga)).all()}
        assert vagas["3"].eliminada_motivo and "presencial" in vagas["3"].eliminada_motivo
        boa = vagas["1"]
        assert boa.match.score >= 85 and boa.match.modo == "llm" and boa.match.status == "novo"
        assert len(boa.curriculos) == 1
        cv = boa.curriculos[0]
        assert Path(cv.caminho_pdf).read_bytes().startswith(b"%PDF")
        assert (cv.provedor_llm, cv.versao) == ("gemini", 1)
        assert vagas["4"].match.score < 40 and not vagas["4"].curriculos  # abaixo do limite: sem CV
        assert boa.match.notificado is True
    assert "discord.com" in hosts


def test_segunda_rodada_nao_duplica_nem_regenera(fabrica_servicos, monkeypatch):
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso())
    rodar(sv)
    # a fonte respeita o intervalo mínimo (3 h): a 2ª rodada pula a coleta
    ex2 = rodar(sv)
    assert ex2.vagas_coletadas == 0
    # força a coleta de novo: as mesmas issues não geram vagas novas
    from app.db.models import FonteEstado
    with sv.fabrica() as s:
        s.get(FonteEstado, "github:backend-br/vagas").ultima_execucao = None
        s.commit()
    ex3 = rodar(sv)
    assert ex3.vagas_coletadas == 3 and ex3.vagas_novas == 0
    with sv.fabrica() as s:
        assert s.scalar(select(Vaga.id).where(Vaga.id > 0).limit(1)) is not None
        assert len(s.scalars(select(CurriculoGerado)).all()) == 1
        assert len(s.scalars(select(Match)).all()) == 2


def test_sem_llm_a_vaga_fica_na_fila_com_score_por_palavras(fabrica_servicos, monkeypatch):
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso(llm_ok=False))
    ex = rodar(sv)
    assert not ex.erros
    with sv.fabrica() as s:
        boa = s.scalars(select(Vaga).where(Vaga.url.like("%/1"))).one()
        assert boa.match.modo == "palavras" and boa.match.score >= 70
        assert boa.curriculos == []  # sem LLM: CV adiado, nada se perde

    # LLM volta: a próxima rodada gera o CV pendente sem recoletar
    sv2 = preparar(fabrica_servicos, monkeypatch, servidor_falso(llm_ok=True))
    from app.db.models import LlmProvedorEstado
    with sv2.fabrica() as s:
        for e in s.scalars(select(LlmProvedorEstado)).all():
            e.cooldown_ate = None
        s.commit()
    from app.pipeline import processar_pendentes
    erros, _ = processar_pendentes(sv2)
    assert not erros
    with sv2.fabrica() as s:
        assert len(s.scalars(select(CurriculoGerado)).all()) == 1


def test_coletor_quebrado_nao_derruba_a_rodada_e_avisa_no_discord(fabrica_servicos, monkeypatch):
    avisos = []

    def h(req):
        if req.url.host == "api.github.com":
            return httpx.Response(500, text="boom")
        if req.url.host == "discord.com":
            avisos.append(json.loads(req.content)["content"])
            return httpx.Response(204)
        return resposta_chat("{}")

    sv = preparar(fabrica_servicos, monkeypatch, h)
    ex = rodar(sv)
    assert "github:backend-br/vagas" in ex.erros
    assert any("Coletor com falha" in a for a in avisos)


def test_execucao_simultanea_e_bloqueada(fabrica_servicos, monkeypatch):
    from app.pipeline import TRAVA
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso())
    assert TRAVA.acquire(blocking=False)
    try:
        assert rodar(sv) is None
    finally:
        TRAVA.release()


def test_vaga_de_email_sem_descricao_tem_score_preliminar_e_nao_gera_cv(fabrica_servicos, monkeypatch):
    from app.coletores.base import VagaBruta
    from app.pipeline import processar_pendentes, salvar_se_nova
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso())
    with sv.fabrica() as s:
        salvar_se_nova(s, VagaBruta("email:linkedin", "https://www.linkedin.com/jobs/view/4012345678/",
                                    "Desenvolvedor Backend Node.js NestJS Pleno", "Acme", "", "Brasil (Remoto)",
                                    descricao_completa=False))
        s.commit()
    erros, novos = processar_pendentes(sv)
    assert not erros and len(novos) == 1 and novos[0].preliminar
    with sv.fabrica() as s:
        assert s.scalars(select(CurriculoGerado)).all() == []


def test_pipeline_registra_execucao(fabrica_servicos, monkeypatch):
    sv = preparar(fabrica_servicos, monkeypatch, servidor_falso())
    rodar(sv, origem="manual")
    with sv.fabrica() as s:
        e = s.scalars(select(Execucao)).one()
        assert e.origem == "manual" and e.fim is not None and e.vagas_novas == 3
