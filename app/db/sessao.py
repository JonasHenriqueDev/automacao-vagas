"""Engine SQLite, sessões e migrações."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

RAIZ = Path(__file__).resolve().parents[2]


def criar_engine(caminho: str | Path) -> Engine:
    """`caminho` pode ser um arquivo ou ':memory:'."""
    caminho = str(caminho)
    url = "sqlite://" if caminho == ":memory:" else f"sqlite:///{caminho}"
    if caminho != ":memory:":
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        if caminho != ":memory:":
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    return engine


def criar_fabrica(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def sessao(fabrica: sessionmaker[Session]) -> Iterator[Session]:
    s = fabrica()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def migrar(caminho_banco: str | Path) -> None:
    """Aplica as migrações do Alembic (`alembic upgrade head`)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{caminho_banco}")
    command.upgrade(cfg, "head")


def criar_tabelas(engine: Engine) -> None:
    """Atalho para testes: cria o esquema direto dos modelos."""
    Base.metadata.create_all(engine)
