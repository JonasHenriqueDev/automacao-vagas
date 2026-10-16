"""Registro de execução: log completo e progresso ao vivo, gravados no banco.

Usa `contextvars`: a rodada define a execução atual e qualquer módulo (coletores, rodízio de LLM,
geração de CV) pode chamar `log()` sem receber parâmetros extras. Fora de uma rodada, só escreve no logger.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from .db.models import Execucao, ExecucaoLog, agora_utc

_log = logging.getLogger("vagas.execucao")
_NIVEIS = {"info": logging.INFO, "aviso": logging.WARNING, "erro": logging.ERROR}


@dataclass
class Contexto:
    fabrica: sessionmaker[Session]
    execucao_id: int | None  # None: só vincula chamadas de LLM à vaga (ações manuais do painel)
    vaga_id: int | None = None


_ctx: ContextVar[Contexto | None] = ContextVar("execucao_ctx", default=None)


def iniciar(fabrica: sessionmaker[Session], execucao_id: int | None):
    """Liga a execução ao contexto atual. Devolve o token para `encerrar`."""
    return _ctx.set(Contexto(fabrica, execucao_id))


def encerrar(token) -> None:
    _ctx.reset(token)


def execucao_atual() -> int | None:
    c = _ctx.get()
    return c.execucao_id if c else None


def vaga_atual() -> int | None:
    c = _ctx.get()
    return c.vaga_id if c else None


def definir_vaga(vaga_id: int | None) -> None:
    c = _ctx.get()
    if c:
        c.vaga_id = vaga_id


def log(mensagem: str, nivel: str = "info", etapa: str = "") -> None:
    """Grava uma linha de log da execução atual (e no logger padrão)."""
    _log.log(_NIVEIS.get(nivel, logging.INFO), "[%s] %s", etapa or "-", mensagem)
    c = _ctx.get()
    if not c or c.execucao_id is None:
        return
    try:
        with c.fabrica() as s:
            s.add(ExecucaoLog(execucao_id=c.execucao_id, nivel=nivel, etapa=etapa, mensagem=mensagem[:6000]))
            s.commit()
    except Exception:  # noqa: BLE001 - log nunca derruba a rodada
        _log.exception("Falha ao gravar o log da execução")


def progresso(percentual: float, etapa: str) -> None:
    """Atualiza a barra de progresso da execução atual (0 a 100)."""
    c = _ctx.get()
    if not c or c.execucao_id is None:
        return
    try:
        with c.fabrica() as s:
            ex = s.get(Execucao, c.execucao_id)
            if ex is not None:
                ex.progresso = max(0.0, min(100.0, float(percentual)))
                ex.etapa = etapa[:200]
                s.commit()
    except Exception:  # noqa: BLE001
        _log.exception("Falha ao gravar o progresso")


def agora():
    return agora_utc()
