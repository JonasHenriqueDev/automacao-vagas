"""Coletor de alertas de vagas por e-mail (IMAP), com um parser por remetente. LinkedIn primeiro.

É a única forma permitida de trazer vagas do LinkedIn (seção 4.2). O e-mail não traz a descrição
completa: as vagas saem com `descricao_completa=False` e o sistema NÃO busca a descrição no site.
"""
from __future__ import annotations

import email
import imaplib
import re
from email.header import decode_header, make_header
from email.message import Message
from datetime import timezone
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from typing import Callable

from .base import VagaBruta

_JOB_URL = re.compile(r"https?://[\w.]*linkedin\.com/(?:comm/)?jobs/view/(?:[\w%-]*?-)?(\d{6,})", re.I)
_TAG = re.compile(r"<[^>]+>")
_ANCORA = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_RUIDO = re.compile(
    r"(candidatura simplificada|easy apply|seja um dos primeiros|be an early applicant|ver vaga|view job|"
    r"visualizar vaga|há \d+|\d+ (dias?|horas?|semanas?) atr[aá]s|\d+ (days?|hours?|weeks?) ago|"
    r"recrutando agora|actively (recruiting|hiring)|promovid[ao]|promoted|candidate-se|apply now|"
    r"^\W*$)",
    re.I,
)


def _texto(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html))).strip()


def parser_linkedin(assunto: str, html: str) -> list[VagaBruta]:
    """Extrai (título, empresa, local, link) de cada cartão de vaga do alerta do LinkedIn.

    Estrutura esperada: o link da vaga traz o título; os textos seguintes trazem empresa e local
    (às vezes juntos como 'Empresa · Local'). Calibrar com e-mails reais se o LinkedIn mudar o layout.
    """
    cartoes: list[tuple[str, str, int, int]] = []  # id, título, início e fim da âncora
    vistos: set[str] = set()
    for m in _ANCORA.finditer(html):
        achou = _JOB_URL.search(unescape(m.group(1)))
        if not achou:
            continue
        job_id = achou.group(1)
        titulo = _texto(m.group(2))
        if not titulo or _RUIDO.search(titulo) or job_id in vistos:
            continue
        vistos.add(job_id)
        cartoes.append((job_id, titulo, m.start(), m.end()))

    vagas: list[VagaBruta] = []
    for idx, (job_id, titulo, _ini, fim) in enumerate(cartoes):
        limite = cartoes[idx + 1][2] if idx + 1 < len(cartoes) else len(html)
        trecho = html[fim:limite]
        partes = [p for p in (_texto(x) for x in re.split(r"</(?:td|tr|div|p|a|span)>|<br\s*/?>", trecho))
                  if p and not _RUIDO.search(p)]
        empresa, local = "", ""
        if partes:
            pedacos = re.split(r"\s[·•|]\s", partes[0], maxsplit=1)
            if len(pedacos) > 1:
                empresa, local = pedacos
            else:
                empresa = partes[0]
                local = partes[1] if len(partes) > 1 else ""
        vagas.append(VagaBruta(
            fonte="email:linkedin",
            url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            titulo=titulo,
            empresa=empresa[:200],
            local=local[:300],
            descricao="",
            descricao_completa=False,
            labels=[f"alerta: {assunto}"] if assunto else [],
        ))
    return vagas


#: um parser por domínio do remetente; novos remetentes (Indeed, Gupy...) entram aqui na fase 4
PARSERS: dict[str, Callable[[str, str], list[VagaBruta]]] = {
    "linkedin.com": parser_linkedin,
}


def _decodificar(valor: str | None) -> str:
    return str(make_header(decode_header(valor or ""))) if valor else ""


def _html(msg: Message) -> str:
    for parte in msg.walk():
        if parte.get_content_type() == "text/html":
            carga = parte.get_payload(decode=True) or b""
            return carga.decode(parte.get_content_charset() or "utf-8", errors="replace")
    return ""


class ColetorEmail:
    nome = "email"

    def __init__(self, servidor: str, porta: int, usuario: str, senha: str, pasta: str = "INBOX",
                 remetentes: list[str] | None = None, marcar_como_lido: bool = True, max_emails: int = 50,
                 conexao_factory: Callable[[], imaplib.IMAP4] | None = None):
        self.servidor, self.porta, self.usuario, self.senha = servidor, porta, usuario, senha
        self.pasta = pasta
        self.remetentes = remetentes or []
        self.marcar_como_lido = marcar_como_lido
        self.max_emails = max_emails
        self._factory = conexao_factory

    def _conectar(self) -> imaplib.IMAP4:
        if self._factory:
            return self._factory()
        return imaplib.IMAP4_SSL(self.servidor, self.porta)

    def testar(self) -> str:
        """Valida servidor/usuário/senha/pasta; devolve quantos e-mails não lidos há."""
        con = self._conectar()
        try:
            con.login(self.usuario, self.senha)
            tipo, _ = con.select(self.pasta, readonly=True)
            if tipo != "OK":
                raise RuntimeError(f"Pasta '{self.pasta}' não encontrada.")
            _, dados = con.search(None, "UNSEEN")
            return f"Login OK. {len(dados[0].split()) if dados and dados[0] else 0} e-mails não lidos na pasta."
        finally:
            try:
                con.logout()
            except Exception:
                pass

    def coletar(self) -> list[VagaBruta]:
        vagas: list[VagaBruta] = []
        con = self._conectar()
        try:
            con.login(self.usuario, self.senha)
            con.select(self.pasta)
            ids: list[bytes] = []
            for rem in (self.remetentes or [""]):
                criterio = ("UNSEEN", "FROM", f'"{rem}"') if rem else ("UNSEEN",)
                _, dados = con.search(None, *criterio)
                ids += dados[0].split() if dados and dados[0] else []
            for num in sorted(set(ids))[: self.max_emails]:
                _, dados = con.fetch(num, "(BODY.PEEK[])")
                bruto = next((p[1] for p in dados if isinstance(p, tuple)), None)
                if not bruto:
                    continue
                msg = email.message_from_bytes(bruto)
                dominio = parseaddr(msg.get("From", ""))[1].rsplit("@", 1)[-1].lower()
                parser = next((p for d, p in PARSERS.items() if dominio.endswith(d)), None)
                if parser is None:
                    continue
                novas = parser(_decodificar(msg.get("Subject")), _html(msg))
                try:  # a data do alerta é a melhor estimativa de "quando a vaga abriu"
                    enviado = parsedate_to_datetime(msg.get("Date", ""))
                    enviado = enviado.astimezone(timezone.utc).replace(tzinfo=None) if enviado.tzinfo else enviado
                except (TypeError, ValueError):
                    enviado = None
                for v in novas:
                    v.data_publicacao = enviado
                vagas += novas
                if self.marcar_como_lido:
                    con.store(num, "+FLAGS", "\\Seen")
        finally:
            try:
                con.logout()
            except Exception:
                pass
        return vagas
