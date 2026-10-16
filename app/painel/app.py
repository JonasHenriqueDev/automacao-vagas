"""Aplicação FastAPI do painel (atrás do Caddy, escutando só em 127.0.0.1)."""
from __future__ import annotations

import logging
import mimetypes
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..agendador import Agendador
from ..pipeline import Servicos
from .auth import LimiteTentativas, RedirecionarLogin
from .rotas import router
from .rotas_curriculo import router as router_curriculo
from .rotas_execucao import router as router_execucao

# O registro do Windows pode mapear .js para text/plain, e o navegador (nosniff) recusa o script.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

log = logging.getLogger(__name__)
AQUI = Path(__file__).parent

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")


def criar_app(sv: Servicos, com_agendador: bool = True) -> FastAPI:
    agendador = Agendador(sv)

    @asynccontextmanager
    async def ciclo(_app: FastAPI):
        if com_agendador:
            agendador.iniciar()
        try:
            yield
        finally:
            agendador.parar()

    app = FastAPI(title="Automação de vagas", lifespan=ciclo, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sv = sv
    app.state.agendador = agendador
    app.state.limite_login = LimiteTentativas(
        int(sv.config.obter("painel.max_tentativas_login", 5)),
        float(sv.config.obter("painel.janela_login_minutos", 15)) * 60)

    segredo = os.environ.get("SEGREDO_SESSAO", "")
    if not segredo:
        segredo = secrets.token_urlsafe(48)
        log.warning("SEGREDO_SESSAO ausente no .env: sessões serão invalidadas a cada reinício.")
    app.add_middleware(
        SessionMiddleware, secret_key=segredo, session_cookie="vagas_sessao", max_age=14 * 24 * 3600,
        same_site="strict", https_only=bool(sv.config.obter("painel.cookie_seguro", True)))

    @app.middleware("http")
    async def cabecalhos(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(RedirecionarLogin)
    async def _login(request: Request, _exc: RedirecionarLogin):
        if request.headers.get("hx-request"):
            return PlainTextResponse("Sessão expirada.", status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")
    app.include_router(router)
    app.include_router(router_execucao)
    app.include_router(router_curriculo)
    return app
