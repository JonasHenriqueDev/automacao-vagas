"""Listagem de modelos nos provedores cujo /models não segue o padrão da OpenAI."""
import httpx
import pytest

from app.llm.cliente import ClienteOpenAI, ErroLLM


def _cliente(base: str, handler) -> ClienteOpenAI:
    return ClienteOpenAI(base, "chave", transporte=httpx.MockTransport(handler))


def test_github_usa_catalogo():
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/inference/models":
            return httpx.Response(200, text="OK")
        assert req.url.path == "/catalog/models"
        return httpx.Response(200, json=[{"id": "openai/gpt-4.1"}, {"id": "openai/gpt-4.1-mini"}])

    assert _cliente("https://models.github.ai/inference", h).listar_modelos() == [
        "openai/gpt-4.1", "openai/gpt-4.1-mini"]


def test_cloudflare_usa_search_e_nome():
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/ai/v1/models"):
            return httpx.Response(405, json={"success": False})
        assert req.url.path.endswith("/ai/models/search")
        return httpx.Response(200, json={"result": [{"id": "uuid-1", "name": "@cf/meta/llama"}]})

    base = "https://api.cloudflare.com/client/v4/accounts/abc/ai/v1"
    assert _cliente(base, h).listar_modelos() == ["@cf/meta/llama"]


def test_resposta_nao_json_vira_erro_claro():
    c = _cliente("https://exemplo.com/v1", lambda req: httpx.Response(200, text="OK"))
    with pytest.raises(ErroLLM, match="não é JSON"):
        c.listar_modelos()
