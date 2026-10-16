"""Autenticação do painel: usuário único, senha com argon2, sessão em cookie assinado, limite de tentativas, CSRF."""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request

from ..db.models import Configuracao
from ..pipeline import Servicos

_ph = PasswordHasher()
# hash de uma senha aleatória, para gastar o mesmo tempo quando o usuário não existe
_HASH_FALSO = _ph.hash(secrets.token_urlsafe(16))

MIN_SENHA = 12


class RedirecionarLogin(Exception):
    pass


def definir_credenciais(sv: Servicos, usuario: str, senha: str) -> None:
    usuario = usuario.strip()
    if not usuario or len(usuario) > 60:
        raise ValueError("Informe um usuário (até 60 caracteres).")
    if len(senha) < MIN_SENHA:
        raise ValueError(f"A senha precisa ter pelo menos {MIN_SENHA} caracteres.")
    with sv.fabrica() as s:
        for chave, valor in (("painel.usuario", usuario), ("painel.senha_hash", _ph.hash(senha))):
            reg = s.get(Configuracao, chave)
            if reg is None:
                s.add(Configuracao(chave=chave, valor=valor))
            else:
                reg.valor = valor
        s.commit()


def _credenciais(sv: Servicos) -> tuple[str, str]:
    with sv.fabrica() as s:
        u, h = s.get(Configuracao, "painel.usuario"), s.get(Configuracao, "painel.senha_hash")
        return (u.valor if u else ""), (h.valor if h else "")


def tem_credenciais(sv: Servicos) -> bool:
    u, h = _credenciais(sv)
    return bool(u and h)


def verificar(sv: Servicos, usuario: str, senha: str) -> bool:
    u, h = _credenciais(sv)
    ok_usuario = bool(u) and hmac.compare_digest(u.encode(), usuario.strip().encode())
    try:
        _ph.verify(h if ok_usuario and h else _HASH_FALSO, senha)
        return ok_usuario
    except VerificationError:
        return False


class LimiteTentativas:
    """Falhas de login por IP numa janela deslizante (em memória: o painel é de processo único)."""

    def __init__(self, maximo: int, janela_s: float):
        self.maximo, self.janela = maximo, janela_s
        self._falhas: dict[str, deque[float]] = defaultdict(deque)
        self._trava = threading.Lock()

    def _limpar(self, ip: str, agora: float) -> deque[float]:
        fila = self._falhas[ip]
        while fila and agora - fila[0] > self.janela:
            fila.popleft()
        return fila

    def bloqueado(self, ip: str) -> bool:
        with self._trava:
            return len(self._limpar(ip, time.monotonic())) >= self.maximo

    def falhou(self, ip: str) -> None:
        with self._trava:
            agora = time.monotonic()
            self._limpar(ip, agora).append(agora)

    def limpar(self, ip: str) -> None:
        with self._trava:
            self._falhas.pop(ip, None)


def token_csrf(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def exigir_login(request: Request) -> None:
    if not request.session.get("usuario"):
        raise RedirecionarLogin()


async def checar_csrf(request: Request) -> None:
    """Exige o token da sessão em todo POST (campo `csrf` do formulário ou cabeçalho X-CSRF-Token do HTMX)."""
    esperado = request.session.get("csrf", "")
    enviado = request.headers.get("x-csrf-token", "")
    if not enviado:
        form = await request.form()
        enviado = str(form.get("csrf", ""))
    if not esperado or not hmac.compare_digest(esperado.encode(), enviado.encode()):
        raise HTTPException(status_code=403, detail="Token CSRF inválido. Recarregue a página.")
