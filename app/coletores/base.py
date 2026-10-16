"""Interface comum dos coletores e normalização/deduplicação de vagas."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

USER_AGENT = "automacao-vagas/0.1 (+uso pessoal; https://github.com/JonasHenriqueDev/automacao-vagas)"


@dataclass
class VagaBruta:
    fonte: str
    url: str
    titulo: str
    empresa: str = ""
    descricao: str = ""
    local: str = ""
    modelo_trabalho: str = "desconhecido"  # remoto | hibrido | presencial | desconhecido
    salario: str = ""
    data_publicacao: datetime | None = None
    labels: list[str] = field(default_factory=list)
    descricao_completa: bool = True
    idioma: str = ""


class Coletor(Protocol):
    """Cada fonte é um módulo isolado com esta interface (seção 4 do plano).

    Atributos opcionais lidos pelo pipeline: `prefiltrar_titulo` (descarta já na coleta títulos que
    não são de desenvolvimento ou de senioridade bloqueada, para fontes amplas) e `stats` (contadores
    mostrados no log da coleta)."""

    nome: str

    def coletar(self) -> list[VagaBruta]: ...


def _limpar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto or "").strip().lower()


def normalizar_url(url: str) -> str:
    partes = urlsplit(url.strip())
    caminho = partes.path.rstrip("/") or "/"
    return urlunsplit((partes.scheme.lower(), partes.netloc.lower(), caminho, "", ""))


def calcular_hash(vaga: VagaBruta) -> str:
    """Hash de empresa + título + url (sem query/fragmento), conforme o plano."""
    base = "|".join([_limpar(vaga.empresa), _limpar(vaga.titulo), normalizar_url(vaga.url)])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


_PT = re.compile(
    r"\b(vaga|requisitos|experi[eê]ncia|conhecimento|desej[aá]vel|diferencial|responsabilidades|"
    r"atividades|benef[ií]cios|somos|buscamos|empresa|trabalho|remoto|n[oó]s)\b",
    re.I,
)
_EN = re.compile(
    r"\b(requirements|experience|knowledge|responsibilities|benefits|we are|we're|looking for|"
    r"you will|you'll|skills|our team|nice to have|remote)\b",
    re.I,
)


def detectar_idioma(texto: str) -> str:
    pt = len(_PT.findall(texto))
    en = len(_EN.findall(texto))
    return "en" if en > pt else "pt"


def data_iso(texto: str | None) -> datetime | None:
    """ISO 8601 (com 'Z', offset ou sem fuso) -> datetime ingênuo em UTC; None se vazio/inválido."""
    if not texto:
        return None
    try:
        d = datetime.fromisoformat(str(texto).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def data_epoch(valor: int | float | str | None) -> datetime | None:
    """Epoch em segundos ou milissegundos -> datetime ingênuo em UTC."""
    try:
        n = float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n > 1e11:  # milissegundos
        n /= 1000
    return datetime.fromtimestamp(n, timezone.utc).replace(tzinfo=None)


def corrigir_mojibake(texto: str) -> str:
    """Desfaz UTF-8 lido como Latin-1 ('QuÃ©' -> 'Qué'), defeito comum em APIs (ex.: RemoteOK)."""
    if "Ã" not in texto and "Â" not in texto:
        return texto
    try:
        return texto.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return texto


def html_para_texto(html: str) -> str:
    """HTML (inclusive escapado duas vezes, como na Arbeitnow) -> texto simples com quebras de linha."""
    texto = corrigir_mojibake(html or "")
    if "&lt;" in texto and "<" not in texto:
        texto = unescape(texto)
    texto = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", texto)
    texto = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|ul|ol|tr)>", "\n", texto)
    texto = re.sub(r"(?i)<li\b[^>]*>", "\n- ", texto)
    texto = unescape(re.sub(r"<[^>]+>", " ", texto)).replace("\xa0", " ")
    texto = re.sub(r"[ \t]+", " ", texto)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", re.sub(r" ?\n ?", "\n", texto)).strip()


_BRASIL = re.compile(
    r"brazil|brasil|latam|latin america|south america|am[eé]rica latina|"
    r"s[aã]o paulo|rio de janeiro|belo horizonte|curitiba|porto alegre|florian[oó]polis|recife|campinas|"
    r"bras[ií]lia|salvador|fortaleza|goi[aâ]nia|, br\b", re.I)
_SEM_RESTRICAO = re.compile(
    r"worldwide|anywhere|global|\bany ?where\b|whole world|everywhere|"
    r"^\s*(fully |100% )?(remote|remoto|home ?office|telecommute)?\s*$", re.I)


def local_brasil(local: str) -> bool:
    """O local cita o Brasil/LATAM (ou uma cidade brasileira)."""
    return bool(_BRASIL.search(local or ""))


def elegivel_do_brasil(local: str) -> bool:
    """Quem mora no Brasil pode se candidatar: local vazio, 'remote' genérico, 'worldwide' ou cita Brasil/LATAM.
    'USA only', 'Germany', 'Europe' etc. dão False."""
    return local_brasil(local) or bool(_SEM_RESTRICAO.search(local or ""))


def idade_horas(publicada: datetime | None, agora: datetime | None = None) -> float | None:
    """Horas desde a publicação (datas ingênuas em UTC). None se a data for desconhecida."""
    if publicada is None:
        return None
    agora = agora or datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0.0, (agora - publicada).total_seconds() / 3600)
