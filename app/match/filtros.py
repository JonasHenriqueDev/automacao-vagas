"""Filtros eliminatórios por regras, sem LLM (seção 5): remoto, região, senioridade, área e palavras bloqueadas."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..coletores.base import VagaBruta
from .taxonomia import norm


@dataclass
class ResultadoFiltro:
    aprovada: bool
    motivo: str = ""
    modelo_trabalho: str = "desconhecido"
    senioridade: str = "desconhecida"  # junior | pleno | desconhecida


def _tem(termo: str, texto: str) -> bool:
    t = norm(termo)
    if not t:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", texto) is not None


def _alguma(termos: list[str], texto: str) -> str | None:
    return next((t for t in termos if _tem(t, texto)), None)


_REMOTO = ["remoto", "remote", "home office", "homeoffice", "100% remoto", "fully remote", "trabalho remoto",
           "work from home", "anywhere", "teletrabalho"]
_HIBRIDO = ["hibrido", "hybrid", "semipresencial", "semi-presencial"]
_PRESENCIAL = ["presencial", "on-site", "onsite", "on site", "in-office", "in office"]
_NEGA_REMOTO = ["nao e remoto", "not remote", "sem opcao remota", "no remote"]


def detectar_modelo(v: VagaBruta) -> str:
    """Título e labels pesam mais que a descrição (que pode citar 'remoto' em outro contexto)."""
    curto = norm(" ".join([v.titulo, v.local, *v.labels]))
    longo = norm(v.descricao)
    for nome, termos in (("hibrido", _HIBRIDO), ("presencial", _PRESENCIAL)):
        if _alguma(termos, curto):
            return nome
    if _alguma(_NEGA_REMOTO, longo):
        return "presencial"
    if _alguma(_REMOTO, curto):
        return "remoto"
    if v.modelo_trabalho in {"remoto", "hibrido", "presencial"}:
        return v.modelo_trabalho
    # descrição: só aceita sinais explícitos ("100% remoto") e sem menção a híbrido/presencial
    if _alguma(_HIBRIDO, longo):
        return "hibrido"
    if _alguma(["100% remoto", "fully remote", "100% remote", "totalmente remoto"], longo):
        return "remoto"
    if _alguma(_PRESENCIAL, longo):
        return "presencial"
    if _alguma(_REMOTO, longo):
        return "remoto"
    return "desconhecido"


def detectar_senioridade(v: VagaBruta, cfg: dict) -> tuple[str, str | None]:
    """(nível, termo_bloqueado). Título e labels decidem; 'Pleno/Sênior' é aceito."""
    texto = norm(" ".join([v.titulo, *v.labels]))
    aceito = _alguma(cfg["senioridade_aceita"], texto)
    bloqueado = _alguma(cfg["senioridade_bloqueada"], texto)
    if bloqueado and not aceito:
        return "bloqueada", bloqueado
    if aceito:
        nivel = "junior" if _alguma(["junior", "júnior", "jr"], texto) else "pleno"
        return nivel, None
    return "desconhecida", None


def _motivo_area(texto_titulo: str, cfg: dict) -> str | None:
    """Área: só front-end / só mobile é descartada; o título precisa parecer vaga de desenvolvimento."""
    area_ok = _alguma(cfg["areas_aceitas"], texto_titulo)
    area_ruim = _alguma(cfg["areas_bloqueadas"], texto_titulo)
    if area_ruim and not _alguma(
        ["full stack", "fullstack", "full-stack", "back-end", "backend", "back end", "integra"], texto_titulo
    ):
        return f"área fora do alvo ({area_ruim})"
    if not area_ok:
        return "título não parece vaga de desenvolvimento"
    return None


def prefiltro_titulo(v: VagaBruta, cfg: dict) -> str | None:
    """Motivo para nem gravar a vaga (título fora da área ou de senioridade bloqueada), ou None.

    Só para fontes amplas (milhares de vagas de todas as áreas); as demais gravam tudo e mostram o motivo da
    eliminação no painel. Usa exatamente as mesmas regras de `avaliar`."""
    _, bloqueado = detectar_senioridade(v, cfg)
    if bloqueado:
        return f"senioridade fora do alvo ({bloqueado})"
    return _motivo_area(norm(" ".join([v.titulo, *v.labels])), cfg)


def avaliar(v: VagaBruta, cfg: dict, assume_remoto: bool = False) -> ResultadoFiltro:
    """`cfg` é a seção `filtros` da configuração."""
    texto_titulo = norm(" ".join([v.titulo, *v.labels]))
    texto_total = norm(" ".join([v.titulo, v.local, v.descricao, *v.labels]))

    modelo = detectar_modelo(v)
    if modelo in {"hibrido", "presencial"}:
        return ResultadoFiltro(False, f"modelo de trabalho {modelo}", modelo)
    if modelo == "desconhecido":
        if not assume_remoto:
            return ResultadoFiltro(False, "sem indicação de vaga 100% remota", modelo)
        modelo = "remoto"

    nivel, bloqueado = detectar_senioridade(v, cfg)
    if bloqueado:
        return ResultadoFiltro(False, f"senioridade fora do alvo ({bloqueado})", modelo, nivel)

    motivo_area = _motivo_area(texto_titulo, cfg)
    if motivo_area:
        return ResultadoFiltro(False, motivo_area, modelo, nivel)

    for palavra in cfg.get("palavras_bloqueadas", []):
        if _tem(palavra, texto_total):
            return ResultadoFiltro(False, f"palavra bloqueada ({palavra})", modelo, nivel)

    # Região: descarta exigência de residência/autorização fora do Brasil, a menos que aceite o Brasil.
    restricao = next((p for p in cfg["restricoes_pais"] if re.search(p, texto_total, re.I)), None)
    if restricao:
        aceita = any(re.search(p, texto_total, re.I) for p in cfg["aceita_pais"])
        if not aceita:
            return ResultadoFiltro(False, "exige residência/autorização de trabalho fora do Brasil", modelo, nivel)

    return ResultadoFiltro(True, "", modelo, nivel)
