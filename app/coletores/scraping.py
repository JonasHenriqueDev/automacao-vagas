"""Fontes brasileiras sem API: ProgramaThor e Vagas.com (HTML estático, sem navegador).

Leitura frágil por natureza: se o site mudar o layout, o coletor falha com erro claro (ou devolve 0 vagas) e os
demais seguem. As duas fontes permitem a leitura em robots.txt; usamos o User-Agent do projeto, poucas páginas
e pausa entre as chamadas.

- ProgramaThor: a lista não traz data nem descrição (e a página da vaga recusa leitores automáticos), então as
  vagas saem sem data e com `descricao_completa=False` (score preliminar; a descrição pode ser colada no painel).
- Vagas.com: a lista traz a data relativa ("Há 2 dias") e um trecho; a descrição completa vem do JSON-LD da página
  da vaga, buscada só para vagas novas e dentro da idade máxima.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Callable
from urllib.parse import quote

import httpx

from .base import USER_AGENT, VagaBruta, html_para_texto, idade_horas

_CABECALHOS = {"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "pt-BR,pt;q=0.9"}


def _limpar(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


class _ColetorHtml:
    internacional = False
    prefiltrar_titulo = True

    def __init__(self, cliente: httpx.Client | None = None, idade_maxima_horas: float | None = None,
                 pausa_segundos: float = 0.5):
        self._cliente = cliente
        self.idade_maxima = idade_maxima_horas
        self.pausa = pausa_segundos
        self.stats: dict[str, int] = {"lidas": 0, "antigas": 0, "paginas": 0, "descricoes": 0}

    def _get(self, url: str) -> httpx.Response:
        if self._cliente is None:
            self._cliente = httpx.Client(timeout=30, follow_redirects=True)
        if self.pausa:
            time.sleep(self.pausa)
        r = self._cliente.get(url, headers=_CABECALHOS, follow_redirects=True)
        self.stats["paginas"] += 1
        r.raise_for_status()
        return r


# ------------------------------------------------------------------ ProgramaThor
_CARTAO_PT = re.compile(r'<div class="cell-list[^"]*">\s*<a href="(/jobs/[^"]+)">(.*?)</a>', re.S)


def _icone(bloco: str, icone: str) -> str:
    m = re.search(rf"""<i class=['"][^'"]*{icone}[^'"]*['"]></i>(.*?)</span>""", bloco, re.S)
    return _limpar(m.group(1)) if m else ""


def parse_programathor(html: str) -> list[VagaBruta]:
    vagas = []
    for href, bloco in _CARTAO_PT.findall(html):
        m = re.search(r"<h3[^>]*>(.*?)</h3>", bloco, re.S)
        if not m:
            continue
        titulo = _limpar(m.group(1))
        nivel, contrato = _icone(bloco, "fa-chart-bar"), _icone(bloco, "fa-file-alt")
        tags = [_limpar(t) for t in re.findall(r"""<span class=['"]tag-list[^'"]*['"]>(.*?)</span>""", bloco, re.S)]
        descricao = "\n".join(p for p in (
            f"Tecnologias: {', '.join(tags)}" if tags else "", f"Nível: {nivel}" if nivel else "",
            f"Contratação: {contrato}" if contrato else "", "(Descrição completa no site da vaga.)") if p)
        vagas.append(VagaBruta(
            fonte="programathor", url=f"https://programathor.com.br{href}", titulo=titulo,
            empresa=_icone(bloco, "fa-briefcase"), descricao=descricao,
            local=_icone(bloco, "fa-map-marker-alt"), modelo_trabalho="remoto",
            labels=[x for x in (nivel, contrato, *tags) if x], descricao_completa=False, idioma="pt"))
    return vagas


class ColetorProgramaThor(_ColetorHtml):
    nome = "programathor"
    URL = "https://programathor.com.br/jobs-city/remoto"

    def coletar(self) -> list[VagaBruta]:
        vagas = parse_programathor(self._get(self.URL).text)
        self.stats["lidas"] = len(vagas)
        return vagas


# ------------------------------------------------------------------ Vagas.com
_ITEM_VC = re.compile(r'<li class="vaga[^"]*">(.*?)</li>', re.S)


def data_relativa(texto: str, agora: datetime | None = None) -> datetime | None:
    """'Há 2 dias', 'Hoje', 'Ontem', 'Há 3 horas', '2 days ago' -> datetime ingênuo em UTC (aproximado)."""
    agora = agora or datetime.now(timezone.utc).replace(tzinfo=None)
    t = _limpar(texto).lower()
    if t in ("hoje", "today"):
        return agora
    if t in ("ontem", "yesterday"):
        return agora - timedelta(days=1)
    m = re.search(r"(\d+)\s*(minuto|min|hora|hour|dia|day|semana|week|m[eê]s|month)", t)
    if not m:
        return None
    n, unidade = int(m.group(1)), m.group(2)
    horas = {"minuto": 1 / 60, "min": 1 / 60, "hora": 1, "hour": 1, "dia": 24, "day": 24, "semana": 168,
             "week": 168, "mês": 720, "mes": 720, "month": 720}[unidade]
    return agora - timedelta(hours=n * horas)


def parse_vagas_com(html: str, agora: datetime | None = None) -> list[VagaBruta]:
    vagas = []
    for bloco in _ITEM_VC.findall(html):
        a = re.search(r'<a class="link-detalhes-vaga"[^>]*title="([^"]*)"[^>]*href="([^"]+)"', bloco)
        if not a:
            a = re.search(r'<a class="link-detalhes-vaga"[^>]*href="([^"]+)"[^>]*title="([^"]*)"', bloco)
            if not a:
                continue
            href, titulo = a.group(1), a.group(2)
        else:
            titulo, href = a.group(1), a.group(2)
        empresa = re.search(r'class="emprVaga">(.*?)</span>', bloco, re.S)
        nivel = re.search(r'class="nivelVaga">(.*?)</span>', bloco, re.S)
        trecho = re.search(r'<div class="detalhes">\s*<p>(.*?)</p>', bloco, re.S)
        local = re.search(r'class="vaga-local">(.*?)</span>', bloco, re.S)
        data = re.search(r'class="data-publicacao">(.*?)</span>', bloco, re.S)
        vagas.append(VagaBruta(
            fonte="vagas.com", url=f"https://www.vagas.com.br{href}", titulo=_limpar(unescape(titulo)),
            empresa=_limpar(empresa.group(1)) if empresa else "",
            descricao=_limpar(trecho.group(1)) if trecho else "", local=_limpar(local.group(1)) if local else "",
            labels=[_limpar(nivel.group(1))] if nivel and _limpar(nivel.group(1)) else [],
            data_publicacao=data_relativa(data.group(1), agora) if data else None,
            descricao_completa=False, idioma="pt"))
    return vagas


def descricao_json_ld(html: str) -> tuple[str, str]:
    """(descrição, tipo de local) do JobPosting em JSON-LD da página da vaga; vazios se não houver."""
    for bruto in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(bruto)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            return html_para_texto(d.get("description", "")), str(d.get("jobLocationType", ""))
    return "", ""


class ColetorVagasCom(_ColetorHtml):
    nome = "vagas.com"
    MAX_DESCRICOES = 25

    def __init__(self, termos: list[str], cliente: httpx.Client | None = None,
                 idade_maxima_horas: float | None = None, pausa_segundos: float = 0.5,
                 url_conhecida: Callable[[str], bool] | None = None):
        super().__init__(cliente, idade_maxima_horas, pausa_segundos)
        self.termos = [t for t in termos if t.strip()]
        self._conhecida = url_conhecida or (lambda _url: False)

    def coletar(self) -> list[VagaBruta]:
        vistas: set[str] = set()
        vagas: list[VagaBruta] = []
        for termo in self.termos:
            slug = quote(re.sub(r"\s+", "-", termo.strip().lower()))
            for sufixo in ("", "-home-office"):
                url = f"https://www.vagas.com.br/vagas-de-{slug}{sufixo}?ordenar_por=mais_recentes"
                for v in parse_vagas_com(self._get(url).text):
                    self.stats["lidas"] += 1
                    if v.url in vistas:
                        continue
                    idade = idade_horas(v.data_publicacao)
                    if self.idade_maxima is not None and idade is not None and idade > self.idade_maxima:
                        self.stats["antigas"] += 1
                        continue
                    vistas.add(v.url)
                    v.modelo_trabalho = "remoto" if sufixo or re.search(
                        r"home ?office|remot", v.local, re.I) else "desconhecido"
                    vagas.append(v)
        for v in vagas:
            if (v.modelo_trabalho != "remoto" or self.stats["descricoes"] >= self.MAX_DESCRICOES
                    or self._conhecida(v.url)):
                continue
            try:
                texto, tipo_local = descricao_json_ld(self._get(v.url).text)
            except httpx.HTTPError:
                continue  # fica com o trecho da lista (score preliminar)
            if texto:
                v.descricao, v.descricao_completa = texto, True
                self.stats["descricoes"] += 1
            if tipo_local.upper() == "TELECOMMUTE":
                v.modelo_trabalho = "remoto"
        return vagas
