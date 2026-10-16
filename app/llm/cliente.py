"""Cliente genérico para APIs compatíveis com a da OpenAI (base_url + chave + modelo)."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..coletores.base import USER_AGENT


class ErroLLM(Exception):
    """Falha genérica (resposta inválida, 4xx de requisição...). Passa para o próximo provedor."""

    def __init__(self, mensagem: str = "", status: int | None = None):
        super().__init__(mensagem)
        self.status = status  # HTTP status quando houve resposta


class ErroCota(ErroLLM):
    def __init__(self, mensagem: str, retry_after: float | None = None, diaria: bool = False,
                 status: int | None = 429):
        super().__init__(mensagem, status)
        self.retry_after = retry_after
        self.diaria = diaria


class ErroAutenticacao(ErroLLM):
    """401/403: chave inválida. O provedor é desativado e o usuário notificado."""


class ErroTemporario(ErroLLM):
    """Timeout ou 5xx: vale tentar de novo com backoff."""


@dataclass
class RespostaLLM:
    texto: str
    tokens_in: int = 0
    tokens_out: int = 0
    provedor: str = ""
    modelo: str = ""


_PALAVRAS_DIARIA = ("per day", "daily", "por dia", "diári", "diari", "rpd", "tpd", "quota exceeded",
                    "exceeded your current quota")


def _retry_after(r: httpx.Response) -> float | None:
    valor = r.headers.get("retry-after")
    if valor:
        try:
            return float(valor)
        except ValueError:
            return None
    return None


class ClienteOpenAI:
    def __init__(self, base_url: str, chave: str, transporte: httpx.BaseTransport | None = None,
                 timeout: float = 90.0):
        self.base_url = base_url.rstrip("/")
        self._chave = chave
        self._http = httpx.Client(timeout=timeout, transport=transporte)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._chave}", "Content-Type": "application/json",
                "User-Agent": USER_AGENT}

    def close(self) -> None:
        self._http.close()

    def listar_modelos(self) -> list[str]:
        try:
            r = self._http.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.HTTPError as e:
            raise ErroTemporario(f"Falha de rede: {e}") from e
        self._checar(r)
        dados = r.json()
        itens = dados.get("data", dados) if isinstance(dados, dict) else dados
        return sorted({str(m.get("id") or m.get("name")) for m in itens if isinstance(m, dict)})

    @staticmethod
    def _checar(r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        corpo = r.text[:1500]
        if r.status_code in (401, 403):
            raise ErroAutenticacao(f"HTTP {r.status_code}: {corpo}", r.status_code)
        if r.status_code == 429:
            diaria = any(p in corpo.lower() for p in _PALAVRAS_DIARIA)
            raise ErroCota(f"HTTP 429: {corpo}", _retry_after(r), diaria, 429)
        if r.status_code >= 500 or r.status_code in (408, 409):
            raise ErroTemporario(f"HTTP {r.status_code}: {corpo}", r.status_code)
        raise ErroLLM(f"HTTP {r.status_code}: {corpo}", r.status_code)

    def chat(self, modelo: str, mensagens: list[dict], json_mode: bool = False,
             temperatura: float = 0.2, max_tokens: int | None = None) -> RespostaLLM:
        corpo: dict = {"model": modelo, "messages": mensagens, "temperature": temperatura}
        if max_tokens:
            corpo["max_tokens"] = max_tokens
        if json_mode:
            corpo["response_format"] = {"type": "json_object"}
        r = self._enviar(corpo)
        if r.status_code == 400 and json_mode:  # modelo sem suporte a response_format: tenta sem
            corpo.pop("response_format")
            r = self._enviar(corpo)
        self._checar(r)
        try:
            dados = r.json()
            texto = dados["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ErroLLM(f"Resposta fora do formato esperado: {r.text[:200]}") from e
        uso = dados.get("usage") or {}
        return RespostaLLM(texto=texto, tokens_in=int(uso.get("prompt_tokens") or 0),
                           tokens_out=int(uso.get("completion_tokens") or 0), modelo=modelo)

    def _enviar(self, corpo: dict) -> httpx.Response:
        try:
            return self._http.post(f"{self.base_url}/chat/completions", json=corpo, headers=self._headers())
        except httpx.TimeoutException as e:
            raise ErroTemporario(f"Timeout: {e}") from e
        except httpx.HTTPError as e:
            raise ErroTemporario(f"Falha de rede: {e}") from e
