"""Currículo mestre (data/curriculo_mestre.yaml) e dados pessoais (fora do git)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from ..match import taxonomia

Idioma = Literal["pt", "en"]


class Bilingue(BaseModel):
    pt: str
    en: str = ""

    def em(self, idioma: str) -> str:
        return (self.en if idioma == "en" and self.en else self.pt).strip()


class Skill(BaseModel):
    nome: str
    categoria: str = ""
    nivel: int = 3
    anos: float | None = None
    academico: bool = False
    sinonimos: list[str] = Field(default_factory=list)

    @property
    def canonicos(self) -> set[str]:
        achados = set(taxonomia.canonizar(self.nome))
        for s in self.sinonimos:
            achados |= set(taxonomia.canonizar(s))
        return achados


class Bullet(BaseModel):
    id: str
    texto: str
    tags: list[str] = Field(default_factory=list)


class Experiencia(BaseModel):
    id: str
    empresa: str
    cargo: Bilingue
    local: str = ""
    inicio: str
    fim: str | None = None
    tipo: str = "profissional"
    conta_experiencia: bool = True
    tecnologias: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)


class Formacao(BaseModel):
    instituicao: str
    curso: Bilingue
    local: str = ""
    inicio: str
    fim: str
    detalhes: Bilingue | None = None


class Mestre(BaseModel):
    cargo_alvo: Bilingue
    inicio_carreira: str
    resumo_base: Bilingue
    experiencias: list[Experiencia]
    formacao: list[Formacao]
    certificacoes: list[Bilingue] = Field(default_factory=list)
    idiomas: list[Bilingue] = Field(default_factory=list)
    skills: list[Skill]

    # ----- consultas -----
    def experiencia(self, exp_id: str) -> Experiencia | None:
        return next((e for e in self.experiencias if e.id == exp_id), None)

    def bullet(self, exp_id: str, bullet_id: str) -> Bullet | None:
        e = self.experiencia(exp_id)
        return next((b for b in e.bullets if b.id == bullet_id), None) if e else None

    def canonicos_skills(self) -> set[str]:
        return set().union(*(s.canonicos for s in self.skills))

    def canonicos_experiencia(self, exp: Experiencia) -> set[str]:
        achados: set[str] = set()
        for t in exp.tecnologias:
            achados |= set(taxonomia.canonizar(t))
        for b in exp.bullets:
            achados |= set(taxonomia.encontrar(b.texto))
            for t in b.tags:
                achados |= set(taxonomia.encontrar(t))
        return achados

    def meses_experiencia(self, hoje: date | None = None) -> int:
        hoje = hoje or date.today()
        ano, mes = (int(x) for x in self.inicio_carreira.split("-"))
        return max(0, (hoje.year - ano) * 12 + (hoje.month - mes))

    def anos_experiencia_texto(self, idioma: str, hoje: date | None = None) -> str:
        meses = self.meses_experiencia(hoje)
        anos, resto = divmod(meses, 12)
        meio = resto >= 6
        if idioma == "en":
            return f"over {anos}.5 years" if meio else f"over {anos} years"
        return f"mais de {anos} anos e meio" if meio else f"mais de {anos} anos"


class Pessoais(BaseModel):
    nome: str
    email: str
    telefone: str = ""
    localizacao: str = ""
    links: list[dict[str, str]] = Field(default_factory=list)

    def valores_sensiveis(self) -> list[str]:
        """Strings que jamais podem aparecer em um prompt enviado a uma LLM."""
        valores = [self.nome, self.email, self.telefone]
        valores += [l.get("url", "") for l in self.links]
        partes_nome = [p for p in self.nome.split() if len(p) > 3]
        return [v for v in valores + partes_nome if v]


def _ler_yaml(caminho: str | Path, exemplo: str) -> dict:
    p = Path(caminho)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} não existe. Copie {p.with_name(exemplo)} para {p.name} e preencha com os seus dados "
            f"(o arquivo real fica fora do git).")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def carregar_mestre(caminho: str | Path) -> Mestre:
    return Mestre.model_validate(_ler_yaml(caminho, "curriculo_mestre.example.yaml"))


def carregar_pessoais(caminho: str | Path) -> Pessoais:
    return Pessoais.model_validate(_ler_yaml(caminho, "dados_pessoais.example.yaml"))
