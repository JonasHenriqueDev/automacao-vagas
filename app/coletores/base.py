"""Interface comum dos coletores e normalização/deduplicação de vagas."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    """Cada fonte é um módulo isolado com esta interface (seção 4 do plano)."""

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


def idade_horas(publicada: datetime | None, agora: datetime | None = None) -> float | None:
    """Horas desde a publicação (datas ingênuas em UTC). None se a data for desconhecida."""
    if publicada is None:
        return None
    agora = agora or datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0.0, (agora - publicada).total_seconds() / 3600)
