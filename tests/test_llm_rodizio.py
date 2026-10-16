from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.models import LlmChamada, LlmProvedorEstado, agora_utc
from app.llm.rodizio import ProvedoresEsgotados, Rodizio
from tests.conftest import resposta_chat

RANKING = [
    {"provedor": "groq", "modelo": "m-groq", "ativo": True},
    {"provedor": "gemini", "modelo": "m-gemini", "ativo": True},
    {"provedor": "mistral", "modelo": "m-mistral", "ativo": True},
]


def montar(fabrica_servicos, monkeypatch, handler, chaves=("GROQ_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY"),
           relogio=None, desativados=None):
    for c in chaves:
        monkeypatch.setenv(c, "chave-teste-1234")
    sv = fabrica_servicos(handler, {"llm": {"ranking": {"extracao": RANKING}}})
    dormiu: list[float] = []
    rod = Rodizio(sv.config, sv.fabrica, transporte=sv.transporte, dormir=dormiu.append,
                  agora=relogio or agora_utc, ao_desativar=(lambda p, m: desativados.append(p)) if desativados is not None else None)
    return sv, rod, dormiu


def hosts(pedidos):
    return [r.url.host for r in pedidos]


def test_usa_o_primeiro_provedor_e_registra_a_chamada(fabrica_servicos, monkeypatch):
    pedidos = []

    def h(req):
        pedidos.append(req)
        return resposta_chat("ok")

    sv, rod, _ = montar(fabrica_servicos, monkeypatch, h)
    resp, _ = rod.executar("extracao", [{"role": "user", "content": "oi"}])
    assert resp.texto == "ok" and resp.provedor == "groq" and hosts(pedidos) == ["api.groq.com"]
    with sv.fabrica() as s:
        c = s.scalars(select(LlmChamada)).one()
        assert (c.provedor, c.sucesso, c.tokens_in, c.tokens_out, c.tarefa) == ("groq", True, 100, 50, "extracao")


def test_429_passa_para_o_proximo_e_aplica_cooldown(fabrica_servicos, monkeypatch):
    pedidos = []

    def h(req):
        pedidos.append(req)
        if req.url.host == "api.groq.com":
            return httpx.Response(429, headers={"retry-after": "30"}, json={"error": "rate limit"})
        return resposta_chat("do gemini")

    sv, rod, _ = montar(fabrica_servicos, monkeypatch, h)
    resp, _ = rod.executar("extracao", [{"role": "user", "content": "oi"}])
    assert resp.provedor == "gemini"
    ok, motivo = rod.disponivel("groq")
    assert not ok and "cooldown" in motivo

    pedidos.clear()
    rod.executar("extracao", [{"role": "user", "content": "de novo"}])
    assert "api.groq.com" not in hosts(pedidos)  # pulado enquanto em cooldown


def test_cooldown_expira_com_o_relogio(fabrica_servicos, monkeypatch):
    agora = [agora_utc()]

    def h(req):
        if req.url.host == "api.groq.com":
            return httpx.Response(429, headers={"retry-after": "30"}, json={})
        return resposta_chat("x")

    sv, rod, _ = montar(fabrica_servicos, monkeypatch, h, relogio=lambda: agora[0])
    rod.executar("extracao", [{"role": "user", "content": "a"}])
    assert not rod.disponivel("groq")[0]
    agora[0] += timedelta(seconds=40)
    assert rod.disponivel("groq")[0]


def test_429_de_cota_diaria_espera_ate_o_dia_seguinte(fabrica_servicos, monkeypatch):
    def h(req):
        if req.url.host == "api.groq.com":
            return httpx.Response(429, json={"error": {"message": "You exceeded your quota per day"}})
        return resposta_chat("x")

    sv, rod, _ = montar(fabrica_servicos, monkeypatch, h)
    rod.executar("extracao", [{"role": "user", "content": "a"}])
    with sv.fabrica() as s:
        ate = s.get(LlmProvedorEstado, "groq").cooldown_ate
    assert ate - agora_utc() > timedelta(minutes=1) and ate.hour == 0


def test_chave_invalida_desativa_e_notifica(fabrica_servicos, monkeypatch):
    avisados = []

    def h(req):
        if req.url.host == "api.groq.com":
            return httpx.Response(401, json={"error": "invalid api key"})
        return resposta_chat("x")

    sv, rod, _ = montar(fabrica_servicos, monkeypatch, h, desativados=avisados)
    resp, _ = rod.executar("extracao", [{"role": "user", "content": "a"}])
    assert resp.provedor == "gemini" and avisados == ["groq"]
    assert "desativado" in rod.disponivel("groq")[1]
    rod.reativar("groq")
    assert rod.disponivel("groq")[0]


def test_erro_5xx_tenta_de_novo_com_backoff_e_depois_pula(fabrica_servicos, monkeypatch):
    chamadas = {"groq": 0}

    def h(req):
        if req.url.host == "api.groq.com":
            chamadas["groq"] += 1
            return httpx.Response(503, text="indisponível")
        return resposta_chat("x")

    sv, rod, dormiu = montar(fabrica_servicos, monkeypatch, h)
    resp, _ = rod.executar("extracao", [{"role": "user", "content": "a"}])
    assert chamadas["groq"] == 3  # 1 + 2 novas tentativas
    assert resp.provedor == "gemini" and len(dormiu) == 2


def test_5xx_que_se_recupera_nao_troca_de_provedor(fabrica_servicos, monkeypatch):
    estado = {"n": 0}

    def h(req):
        estado["n"] += 1
        return httpx.Response(500) if estado["n"] == 1 else resposta_chat("recuperou")

    sv, rod, _ = montar(fabrica_servicos, monkeypatch, h)
    resp, _ = rod.executar("extracao", [{"role": "user", "content": "a"}])
    assert resp.provedor == "groq" and resp.texto == "recuperou"


def test_provedor_sem_chave_e_pulado(fabrica_servicos, monkeypatch):
    pedidos = []
    sv, rod, _ = montar(fabrica_servicos, monkeypatch, lambda r: (pedidos.append(r), resposta_chat("x"))[1],
                        chaves=("GEMINI_API_KEY",))
    resp, _ = rod.executar("extracao", [{"role": "user", "content": "a"}])
    assert resp.provedor == "gemini" and hosts(pedidos) == ["generativelanguage.googleapis.com"]


def test_validador_rejeita_saida_e_vai_ao_proximo(fabrica_servicos, monkeypatch):
    def h(req):
        return resposta_chat("lixo" if req.url.host == "api.groq.com" else '{"ok": 1}')

    def validar(texto):
        if not texto.startswith("{"):
            raise ValueError("não é JSON")
        return {"ok": True}

    sv, rod, _ = montar(fabrica_servicos, monkeypatch, h)
    resp, valor = rod.executar("extracao", [{"role": "user", "content": "a"}], validador=validar)
    assert resp.provedor == "gemini" and valor == {"ok": True}


def test_todos_esgotados_levanta_e_nada_se_perde(fabrica_servicos, monkeypatch):
    sv, rod, _ = montar(fabrica_servicos, monkeypatch, lambda r: httpx.Response(429, json={}))
    with pytest.raises(ProvedoresEsgotados) as e:
        rod.executar("extracao", [{"role": "user", "content": "a"}])
    assert "groq" in str(e.value)
    with pytest.raises(ProvedoresEsgotados):  # segunda chamada: todos em cooldown, nem tenta a rede
        rod.executar("extracao", [{"role": "user", "content": "b"}])


def test_json_mode_sem_suporte_repete_sem_response_format(fabrica_servicos, monkeypatch):
    corpos = []

    def h(req):
        import json
        corpo = json.loads(req.content)
        corpos.append(corpo)
        return httpx.Response(400, text="response_format unsupported") if "response_format" in corpo else resposta_chat("{}")

    sv, rod, _ = montar(fabrica_servicos, monkeypatch, h)
    rod.executar("extracao", [{"role": "user", "content": "a"}], json_mode=True)
    assert "response_format" in corpos[0] and "response_format" not in corpos[1]


def test_url_do_cloudflare_usa_account_id(fabrica_servicos, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "abc123")
    sv = fabrica_servicos(lambda r: resposta_chat("x"))
    cli = Rodizio(sv.config, sv.fabrica, transporte=sv.transporte).cliente("cloudflare")
    assert cli.base_url == "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID")
    assert Rodizio(sv.config, sv.fabrica).cliente("cloudflare") is None
