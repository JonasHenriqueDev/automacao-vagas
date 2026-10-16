"""Rodízio entre provedores gratuitos de LLM, com cooldown, backoff e log de chamadas (seção 6)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from sqlalchemy.orm import Session, sessionmaker

from .. import registro
from ..config import Config
from ..db.models import LlmChamada, LlmProvedorEstado, agora_utc
from .cliente import (
    ClienteOpenAI,
    ErroAutenticacao,
    ErroCota,
    ErroLLM,
    ErroTemporario,
    RespostaLLM,
)

log = logging.getLogger(__name__)

COOLDOWN_MINUTO = 65.0  # segundos, quando o provedor não manda Retry-After


class ProvedoresEsgotados(Exception):
    """Nenhum provedor disponível agora; a vaga fica na fila para a próxima rodada."""


def _proxima_meia_noite_utc(agora: datetime) -> datetime:
    return (agora + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)


class Rodizio:
    def __init__(
        self,
        config: Config,
        fabrica: sessionmaker[Session],
        *,
        transporte: httpx.BaseTransport | None = None,
        dormir: Callable[[float], None] = time.sleep,
        agora: Callable[[], datetime] = agora_utc,
        ao_desativar: Callable[[str, str], None] | None = None,
    ):
        self.config = config
        self.fabrica = fabrica
        self._transporte = transporte
        self._dormir = dormir
        self._agora = agora
        self._ao_desativar = ao_desativar  # notifica (Discord) quando uma chave é inválida

    # ----- estado -----
    def _estado(self, s: Session, provedor: str) -> LlmProvedorEstado:
        est = s.get(LlmProvedorEstado, provedor)
        if est is None:
            est = LlmProvedorEstado(provedor=provedor)
            s.add(est)
        return est

    def disponivel(self, provedor: str) -> tuple[bool, str]:
        with self.fabrica() as s:
            est = s.get(LlmProvedorEstado, provedor)
            if est and est.desativado:
                return False, f"desativado: {est.motivo}"
            if est and est.cooldown_ate and est.cooldown_ate > self._agora():
                return False, f"em cooldown até {est.cooldown_ate:%d/%m %H:%M} UTC"
        return True, ""

    def reativar(self, provedor: str) -> None:
        with self.fabrica() as s:
            est = self._estado(s, provedor)
            est.desativado, est.motivo, est.cooldown_ate = False, "", None
            s.commit()

    def _registrar(self, provedor: str, modelo: str, tarefa: str, sucesso: bool, erro: str = "",
                   resp: RespostaLLM | None = None, *, duracao_ms: int = 0, http_status: int | None = None,
                   tipo_erro: str = "", tentativa: int = 1) -> None:
        """Grava a chamada no banco (histórico completo) e na linha do tempo da execução."""
        with self.fabrica() as s:
            s.add(LlmChamada(provedor=provedor, modelo=modelo, tarefa=tarefa, sucesso=sucesso, erro=erro[:4000],
                             tokens_in=resp.tokens_in if resp else 0, tokens_out=resp.tokens_out if resp else 0,
                             criado_em=self._agora(), execucao_id=registro.execucao_atual(),
                             vaga_id=registro.vaga_atual(), duracao_ms=duracao_ms, http_status=http_status,
                             tipo_erro=tipo_erro, tentativa=tentativa))
            s.commit()
        seg = duracao_ms / 1000
        if sucesso:
            tok = f", {resp.tokens_in}/{resp.tokens_out} tokens" if resp else ""
            registro.log(f"LLM {provedor}/{modelo} [{tarefa}] OK em {seg:.1f}s{tok}", "info", "llm")
        else:
            http = f" HTTP {http_status}" if http_status else ""
            registro.log(f"LLM {provedor}/{modelo} [{tarefa}] FALHOU ({tipo_erro}{http}, tentativa {tentativa}, "
                         f"{seg:.1f}s): {erro[:600]}", "aviso", "llm")

    def _cooldown(self, provedor: str, e: ErroCota) -> None:
        agora = self._agora()
        if e.retry_after is not None:
            ate = agora + timedelta(seconds=e.retry_after + 1)
        elif e.diaria:
            ate = _proxima_meia_noite_utc(agora)
        else:
            ate = agora + timedelta(seconds=COOLDOWN_MINUTO)
        with self.fabrica() as s:
            self._estado(s, provedor).cooldown_ate = ate
            s.commit()
        registro.log(f"LLM {provedor} em cooldown até {ate:%d/%m %H:%M} UTC", "aviso", "llm")

    def _desativar(self, provedor: str, motivo: str) -> None:
        with self.fabrica() as s:
            est = self._estado(s, provedor)
            est.desativado, est.motivo = True, motivo[:300]
            s.commit()
        registro.log(f"LLM {provedor} DESATIVADO: {motivo[:300]}", "erro", "llm")
        if self._ao_desativar:
            self._ao_desativar(provedor, motivo)

    # ----- configuração -----
    def _url_base(self, provedor: str, cfg: dict) -> str:
        url = cfg["base_url"]
        for nome in ("CLOUDFLARE_ACCOUNT_ID",):
            if "{" + nome + "}" in url:
                valor = self.config.segredos.obter(nome)
                if not valor:
                    return ""  # sem o id da conta não há URL válida: o provedor é pulado
                url = url.replace("{" + nome + "}", valor)
        return url

    def cliente(self, provedor: str) -> ClienteOpenAI | None:
        """Cliente do provedor, ou None se estiver inativo, sem chave ou sem configuração."""
        cfg = (self.config.obter("llm.provedores") or {}).get(provedor)
        if not cfg or not cfg.get("ativo", True):
            return None
        chave = self.config.segredos.obter(cfg.get("chave_env", ""))
        if not chave:
            return None
        url = self._url_base(provedor, cfg)
        if "{" in url or not url:
            return None
        return ClienteOpenAI(url, chave, transporte=self._transporte)

    # ----- execução -----
    def executar(
        self,
        tarefa: str,
        mensagens: list[dict],
        *,
        json_mode: bool = False,
        validador: Callable[[str], object] | None = None,
        temperatura: float = 0.2,
        max_tokens: int | None = None,
    ) -> tuple[RespostaLLM, object | None]:
        """Chama o 1º provedor disponível do ranking da `tarefa`; devolve (resposta, valor validado).

        `validador(texto)` pode lançar ValueError: a resposta é rejeitada e o próximo provedor é tentado.
        """
        ranking = self.config.obter(f"llm.ranking.{tarefa}") or []
        tentativas = int(self.config.obter("llm.tentativas_temporarias", 2))
        backoff = float(self.config.obter("llm.backoff_segundos", 2))
        motivos: list[str] = []

        for item in ranking:
            provedor, modelo = item["provedor"], item["modelo"]
            if not item.get("ativo", True):
                continue
            ok, porque = self.disponivel(provedor)
            if not ok:
                motivos.append(f"{provedor}: {porque}")
                registro.log(f"LLM {provedor} pulado [{tarefa}]: {porque}", "info", "llm")
                continue
            cliente = self.cliente(provedor)
            if cliente is None:
                motivos.append(f"{provedor}: sem chave/inativo")
                registro.log(f"LLM {provedor} pulado [{tarefa}]: sem chave cadastrada ou inativo", "info", "llm")
                continue
            try:
                for n in range(tentativas + 1):
                    inicio = time.monotonic()
                    resp = None

                    def dur() -> int:
                        return int((time.monotonic() - inicio) * 1000)

                    try:
                        resp = cliente.chat(modelo, mensagens, json_mode=json_mode, temperatura=temperatura,
                                            max_tokens=max_tokens)
                        resp.provedor = provedor
                        valor = validador(resp.texto) if validador else None
                    except ErroTemporario as e:
                        self._registrar(provedor, modelo, tarefa, False, str(e), duracao_ms=dur(),
                                        http_status=e.status, tipo_erro="temporario", tentativa=n + 1)
                        if n < tentativas:
                            self._dormir(backoff * (2**n))
                            continue
                        motivos.append(f"{provedor}: {e}")
                        break
                    except ErroCota as e:
                        self._registrar(provedor, modelo, tarefa, False, str(e), duracao_ms=dur(),
                                        http_status=e.status, tipo_erro="cota", tentativa=n + 1)
                        self._cooldown(provedor, e)
                        motivos.append(f"{provedor}: cota esgotada")
                        break
                    except ErroAutenticacao as e:
                        self._registrar(provedor, modelo, tarefa, False, str(e), duracao_ms=dur(),
                                        http_status=e.status, tipo_erro="auth", tentativa=n + 1)
                        self._desativar(provedor, f"Chave inválida ({e})")
                        motivos.append(f"{provedor}: chave inválida")
                        break
                    except ErroLLM as e:
                        self._registrar(provedor, modelo, tarefa, False, str(e), duracao_ms=dur(),
                                        http_status=e.status, tipo_erro="outro", tentativa=n + 1)
                        motivos.append(f"{provedor}: {e}")
                        break
                    except ValueError as e:  # validador rejeitou a saída
                        self._registrar(provedor, modelo, tarefa, False, f"saída inválida: {e}", resp,
                                        duracao_ms=dur(), tipo_erro="saida_invalida", tentativa=n + 1)
                        motivos.append(f"{provedor}: saída inválida ({e})")
                        break
                    else:
                        self._registrar(provedor, modelo, tarefa, True, resp=resp, duracao_ms=dur(),
                                        tentativa=n + 1)
                        return resp, valor
            finally:
                cliente.close()

        resumo = "; ".join(motivos or ["ranking vazio"])
        registro.log(f"Nenhum provedor disponível para '{tarefa}': {resumo[:800]}", "aviso", "llm")
        raise ProvedoresEsgotados(f"Nenhum provedor disponível para '{tarefa}': " + resumo)
