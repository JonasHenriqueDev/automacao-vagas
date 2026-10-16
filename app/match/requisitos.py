"""Requisitos estruturados de uma vaga + extrator por palavras (fase 1, sem LLM)."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from . import taxonomia


class RequisitosVaga(BaseModel):
    """Mesmo formato para o extrator por palavras e para a LLM (seção 5 do plano)."""

    obrigatorios: list[str] = Field(default_factory=list)
    desejaveis: list[str] = Field(default_factory=list)
    senioridade: str = "desconhecida"  # junior | pleno | senior | desconhecida
    modelo_trabalho: str = "desconhecido"  # remoto | hibrido | presencial | desconhecido
    local: str = ""
    salario: str = ""
    idioma: str = "pt"

    @field_validator("obrigatorios", "desejaveis", mode="before")
    @classmethod
    def _lista_de_textos(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x).strip() for x in v if str(x).strip()]

    @field_validator("senioridade", mode="before")
    @classmethod
    def _senioridade(cls, v):
        t = taxonomia.norm(str(v or ""))
        if any(k in t for k in ("junior", "jr", "entry")):
            return "junior"
        if any(k in t for k in ("pleno", "mid", "intermedi")):
            return "pleno"
        if any(k in t for k in ("senior", "sr", "staff", "lead", "principal", "especialista")):
            return "senior"
        return "desconhecida"

    @field_validator("modelo_trabalho", mode="before")
    @classmethod
    def _modelo(cls, v):
        t = taxonomia.norm(str(v or ""))
        if "hibrid" in t or "hybrid" in t:
            return "hibrido"
        if "presencial" in t or "on-site" in t or "onsite" in t:
            return "presencial"
        if "remot" in t:
            return "remoto"
        return "desconhecido"

    @field_validator("idioma", mode="before")
    @classmethod
    def _idioma(cls, v):
        return "en" if str(v or "").lower().startswith("en") else "pt"


_MARCADORES_DESEJAVEL = re.compile(
    r"(desej[aá]ve(l|is)|diferenciais?|nice[- ]to[- ]have|n[aã]o (s[aã]o )?obrigat[oó]rio|bonus|b[oô]nus|"
    r"\bplus\b|preferred|preferably|ser[aá] um diferencial|would be a plus)",
    re.I,
)
_MARCADORES_OBRIGATORIO = re.compile(
    r"(requisitos?|obrigat[oó]rio|must[- ]have|requirements?|qualifica[cç][oõ]es|qualifications|"
    r"what you('ll)? need|o que (buscamos|esperamos)|pr[eé]-requisitos)",
    re.I,
)


def extrair_por_palavras(titulo: str, descricao: str, labels: list[str] | None = None) -> RequisitosVaga:
    """Tecnologias da taxonomia citadas. Trechos após marcadores de 'desejável/diferencial' viram desejáveis;
    o restante conta como obrigatório. Título e labels sempre contam como obrigatórios."""
    obrig: list[str] = []
    desej: list[str] = []
    vistos: set[str] = set()

    for t in taxonomia.encontrar(" \n ".join([titulo, *(labels or [])])):
        if t not in vistos:
            vistos.add(t)
            obrig.append(t)

    modo = "obrig"
    for linha in (descricao or "").splitlines():
        if _MARCADORES_DESEJAVEL.search(linha):
            modo = "desej"
        elif _MARCADORES_OBRIGATORIO.search(linha):
            modo = "obrig"
        for t in taxonomia.encontrar(linha):
            if t in vistos:
                continue
            vistos.add(t)
            (desej if modo == "desej" else obrig).append(t)

    return RequisitosVaga(obrigatorios=obrig, desejaveis=desej)
