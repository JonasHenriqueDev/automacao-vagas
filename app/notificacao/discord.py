"""Notificações no Discord via webhook (vagas novas e erros graves)."""
from __future__ import annotations

import logging

import httpx

from ..coletores.base import USER_AGENT
from ..config import Config

log = logging.getLogger(__name__)


class Discord:
    def __init__(self, config: Config, transporte: httpx.BaseTransport | None = None):
        self.config = config
        self._transporte = transporte

    @property
    def url(self) -> str:
        return self.config.segredos.obter("DISCORD_WEBHOOK_URL")

    @property
    def ativo(self) -> bool:
        return bool(self.config.obter("discord.ativo", True)) and bool(self.url)

    def _postar(self, payload: dict) -> None:
        r = httpx.Client(transport=self._transporte, timeout=15).post(
            self.url, json=payload, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()

    def enviar(self, conteudo: str, embeds: list[dict] | None = None) -> bool:
        """Nunca levanta exceção: falha de notificação não pode derrubar a execução."""
        if not self.ativo:
            return False
        try:
            self._postar({"content": conteudo[:1900], "embeds": embeds or [],
                          "allowed_mentions": {"parse": []}})
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Falha ao notificar o Discord: %s", e)
            return False

    def testar(self) -> str:
        if not self.url:
            raise RuntimeError("Webhook do Discord não configurado.")
        self._postar({"content": "✅ automacao-vagas: teste de notificação.", "allowed_mentions": {"parse": []}})
        return "Mensagem de teste enviada."

    def vagas_novas(self, qtd: int, top: list[tuple[float, str, str, str]], url_painel: str) -> bool:
        """`top`: (score, título, empresa, fonte). Resumo: quantidade, top 3 por score e link do painel."""
        if qtd <= 0:
            return False
        linhas = [f"**{s:.0f}%** · {t} — {e or 'empresa n/d'} _( {f} )_" for s, t, e, f in top]
        embed = {"title": f"{qtd} vaga(s) nova(s) para revisar", "url": url_painel,
                 "description": "\n".join(linhas) or "Sem detalhes.", "color": 0x2B7A78}
        return self.enviar("", [embed])

    def erro(self, titulo: str, detalhe: str) -> bool:
        return self.enviar(f"⚠️ **{titulo}**\n{detalhe[:1500]}")
