"""Lacunas entre os requisitos de uma vaga e o currículo mestre: o que a vaga pede e nenhuma skill ativa cobre.

Alimenta o formulário «Você tem conhecimento nisso?» da aba Gerar CV. Usa o mesmo critério do score
(`score.Perfil`), então uma lacuna é exatamente um requisito que hoje conta como «faltou» no match.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..match import taxonomia
from ..match.requisitos import RequisitosVaga
from ..match.score import Perfil
from .mestre import Mestre

MAX_LACUNAS = 25
MAX_TERMO = 60


@dataclass
class Lacuna:
    termo: str
    obrigatoria: bool
    #: nome de uma skill DESATIVADA do mestre que cobriria o requisito (basta reativar)
    inativa: str = ""


def _skill_inativa(termo: str, completo: Mestre) -> str:
    alvo = set(taxonomia.canonizar(termo))
    for s in completo.skills:
        if not s.ativo and taxonomia.com_implicacoes(s.canonicos) & alvo:
            return s.nome
    return ""


def encontrar(req: RequisitosVaga, completo: Mestre, peso_academico: float = 0.5) -> list[Lacuna]:
    """Requisitos (obrigatórios primeiro) sem cobertura nas skills ativas de `completo`, sem repetições."""
    perfil = Perfil(completo.somente_ativos(), peso_academico)
    vistos: set[tuple[str, ...]] = set()
    lacunas: list[Lacuna] = []
    for obrigatoria, itens in ((True, req.obrigatorios), (False, req.desejaveis)):
        for bruto in itens:
            termo = " ".join(str(bruto).split())[:MAX_TERMO]
            if not termo:
                continue
            chave = tuple(sorted(taxonomia.canonizar(termo)))
            if chave in vistos or perfil.peso_do(termo) > 0:
                continue
            vistos.add(chave)
            lacunas.append(Lacuna(termo, obrigatoria, _skill_inativa(termo, completo)))
    return lacunas[:MAX_LACUNAS]
