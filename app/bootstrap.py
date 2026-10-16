"""Montagem dos serviços: configuração, banco (com migrações) e dependências."""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .db.sessao import criar_engine, criar_fabrica, migrar
from .pipeline import Servicos


def criar_servicos(config_path: str | Path | None = None, migrar_banco: bool = True) -> Servicos:
    cfg = Config.carregar(config_path)
    caminho_banco = cfg.caminho("banco")
    if migrar_banco:
        caminho_banco.parent.mkdir(parents=True, exist_ok=True)
        migrar(caminho_banco)
    fabrica = criar_fabrica(criar_engine(caminho_banco))
    cfg.ligar_banco(fabrica)
    return Servicos(cfg, fabrica)
