"""Score ponderado de compatibilidade (seção 5): obrigatórios 60%, desejáveis 20%, senioridade 10%,
local/salário 10% (pesos configuráveis). Categorias sem itens têm o peso redistribuído."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..curriculo.mestre import Mestre
from . import taxonomia
from .requisitos import RequisitosVaga


@dataclass
class Detalhamento:
    score: float
    bateram: list[str] = field(default_factory=list)
    faltaram: list[str] = field(default_factory=list)
    bateram_desejaveis: list[str] = field(default_factory=list)
    faltaram_desejaveis: list[str] = field(default_factory=list)
    academicos: list[str] = field(default_factory=list)
    componentes: dict[str, float] = field(default_factory=dict)
    preliminar: bool = False
    modo: str = "palavras"

    def como_dict(self) -> dict:
        return asdict(self)


class Perfil:
    """Skills do mestre já resolvidas para nomes canônicos, com o peso de cada uma."""

    def __init__(self, mestre: Mestre, peso_academico: float = 0.5):
        self.pesos: dict[str, float] = {}
        self.academicos: set[str] = set()
        for s in mestre.skills:
            for c in taxonomia.com_implicacoes(s.canonicos):
                peso = peso_academico if s.academico else 1.0
                if peso >= self.pesos.get(c, 0.0):
                    self.pesos[c] = peso
        for s in mestre.skills:
            if s.academico:
                for c in s.canonicos:
                    if self.pesos.get(c) == peso_academico:
                        self.academicos.add(c)

    def peso_do(self, termo: str) -> float:
        melhor = 0.0
        for c in taxonomia.canonizar(termo):
            melhor = max(melhor, self.pesos.get(c, 0.0))
        return melhor


def _cobertura(itens: list[str], perfil: Perfil) -> tuple[float, list[str], list[str], list[str]]:
    bateram, faltaram, academicos = [], [], []
    soma = 0.0
    for item in itens:
        p = perfil.peso_do(item)
        if p > 0:
            soma += p
            bateram.append(item)
            if p < 1.0:
                academicos.append(item)
        else:
            faltaram.append(item)
    return (soma / len(itens) if itens else 0.0), bateram, faltaram, academicos


def _senioridade(nivel: str) -> float:
    return {"junior": 1.0, "pleno": 1.0, "desconhecida": 0.7, "senior": 0.0}.get(nivel, 0.7)


def _local_salario(req: RequisitosVaga, local: str) -> float:
    pontos = 0.0
    pontos += {"remoto": 1.0, "desconhecido": 0.5}.get(req.modelo_trabalho, 0.0)
    return pontos


def calcular(req: RequisitosVaga, perfil: Perfil, pesos: dict[str, float], *,
             preliminar: bool = False, modo: str = "palavras", local: str = "") -> Detalhamento:
    cov_o, bat_o, fal_o, aca_o = _cobertura(req.obrigatorios, perfil)
    cov_d, bat_d, fal_d, aca_d = _cobertura(req.desejaveis, perfil)

    componentes = {
        "obrigatorios": cov_o,
        "desejaveis": cov_d,
        "senioridade": _senioridade(req.senioridade),
        "local_salario": _local_salario(req, local),
    }
    ativos = dict(pesos)
    if not req.obrigatorios:
        ativos.pop("obrigatorios", None)
    if not req.desejaveis:
        ativos.pop("desejaveis", None)
    total_peso = sum(ativos.values()) or 1.0
    bruto = sum(componentes[k] * w for k, w in ativos.items()) / total_peso

    # Sem nenhum requisito técnico detectado não dá para afirmar compatibilidade.
    if not req.obrigatorios and not req.desejaveis:
        bruto = min(bruto, 0.4)

    return Detalhamento(
        score=round(bruto * 100, 1),
        bateram=bat_o,
        faltaram=fal_o,
        bateram_desejaveis=bat_d,
        faltaram_desejaveis=fal_d,
        academicos=sorted(set(aca_o + aca_d)),
        componentes={k: round(v, 3) for k, v in componentes.items()},
        preliminar=preliminar,
        modo=modo,
    )
