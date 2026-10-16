"""Fontes internacionais de vagas remotas (APIs JSON públicas e RSS): Remotive, RemoteOK, Jobicy, Himalayas,
Arbeitnow e We Work Remotely.

Todas são `internacional = True`: o pipeline só as consulta quando "incluir vagas internacionais" está ligado.
Mesmo assim, só ficam vagas elegíveis para quem mora no Brasil (`elegivel_do_brasil`): "Worldwide", "Anywhere",
LATAM, Brasil ou sem restrição de país. "USA only", "Europe" etc. são descartadas aqui, antes de gravar.
Os termos dessas APIs pedem crédito à fonte: `Vaga.fonte` guarda o nome e `Vaga.url` aponta para o anúncio original.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .base import (USER_AGENT, VagaBruta, data_epoch, data_iso, elegivel_do_brasil, html_para_texto,
                   idade_horas)


class _ColetorHttp:
    nome = ""
    internacional = True
    prefiltrar_titulo = True

    def __init__(self, cliente: httpx.Client | None = None, idade_maxima_horas: float | None = None):
        self._cliente = cliente
        self.idade_maxima = idade_maxima_horas
        self.stats: dict[str, int] = {"lidas": 0, "fora_do_pais": 0, "antigas": 0, "paginas": 0}

    def _get(self, url: str, **params: Any) -> httpx.Response:
        if self._cliente is None:
            self._cliente = httpx.Client(timeout=30)
        r = self._cliente.get(url, params=params or None, headers={"User-Agent": USER_AGENT,
                                                                    "Accept": "application/json, */*"})
        self.stats["paginas"] += 1
        r.raise_for_status()
        return r

    def _velha(self, publicada: datetime | None) -> bool:
        idade = idade_horas(publicada)
        return self.idade_maxima is not None and idade is not None and idade > self.idade_maxima

    def _aceita(self, local: str, publicada: datetime | None) -> bool:
        """Conta e rejeita vaga fora do país elegível ou velha demais."""
        self.stats["lidas"] += 1
        if not elegivel_do_brasil(local):
            self.stats["fora_do_pais"] += 1
            return False
        if self._velha(publicada):
            self.stats["antigas"] += 1
            return False
        return True

    def coletar(self) -> list[VagaBruta]:  # pragma: no cover - interface
        raise NotImplementedError


def _data_rfc822(texto: str | None) -> datetime | None:
    """'Wed, 30 Sep 2026 07:31:24 +0000' -> datetime ingênuo em UTC."""
    try:
        d = parsedate_to_datetime(texto or "")
    except (TypeError, ValueError):
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def _salario(minimo: Any, maximo: Any, moeda: str = "", periodo: str = "") -> str:
    try:
        lo, hi = int(float(minimo or 0)), int(float(maximo or 0))
    except (TypeError, ValueError):
        return ""
    if not lo and not hi:
        return ""
    faixa = f"{lo:,}–{hi:,}" if lo and hi and lo != hi else f"{max(lo, hi):,}"
    return " ".join(p for p in (moeda, faixa.replace(",", "."), f"/{periodo}" if periodo else "") if p)


class ColetorRemotive(_ColetorHttp):
    """https://remotive.com/api/remote-jobs — os termos pedem no máximo ~4 consultas por dia e crédito à fonte.
    A API atrasa as vagas em 24 h, então com a regra de 24 h poucas sobram."""

    nome = "remotive"
    URL = "https://remotive.com/api/remote-jobs"

    def coletar(self) -> list[VagaBruta]:
        vagas = []
        for j in self._get(self.URL, category="software-dev", limit=100).json().get("jobs", []):
            publicada = data_iso(j.get("publication_date"))
            local = j.get("candidate_required_location") or ""
            if not self._aceita(local, publicada):
                continue
            vagas.append(VagaBruta(
                fonte=self.nome, url=j["url"], titulo=(j.get("title") or "").strip(),
                empresa=(j.get("company_name") or "").strip(), descricao=html_para_texto(j.get("description", "")),
                local=local, modelo_trabalho="remoto", salario=j.get("salary") or "", data_publicacao=publicada,
                labels=[str(t) for t in j.get("tags", [])] + ([j["job_type"]] if j.get("job_type") else []),
                idioma="en"))
        return vagas


class ColetorRemoteOK(_ColetorHttp):
    """https://remoteok.com/api — o 1º item é um aviso legal; exige link de volta (guardamos a URL deles)."""

    nome = "remoteok"
    URL = "https://remoteok.com/api"

    def coletar(self) -> list[VagaBruta]:
        vagas = []
        for j in self._get(self.URL).json():
            if not isinstance(j, dict) or "position" not in j:
                continue
            publicada = data_epoch(j.get("epoch")) or data_iso(j.get("date"))
            local = j.get("location") or ""
            if not self._aceita(local, publicada):
                continue
            vagas.append(VagaBruta(
                fonte=self.nome, url=j.get("url") or j.get("apply_url") or "", titulo=html_para_texto(j["position"]),
                empresa=html_para_texto(j.get("company", "")), descricao=html_para_texto(j.get("description", "")),
                local=local, modelo_trabalho="remoto",
                salario=_salario(j.get("salary_min"), j.get("salary_max"), "USD", "ano"),
                data_publicacao=publicada, labels=[str(t) for t in j.get("tags", [])], idioma="en"))
        return [v for v in vagas if v.url]


class ColetorJobicy(_ColetorHttp):
    """https://jobicy.com/api/v2/remote-jobs — `jobGeo` lista países ('Anywhere', 'LATAM, USA'...)."""

    nome = "jobicy"
    URL = "https://jobicy.com/api/v2/remote-jobs"

    def coletar(self) -> list[VagaBruta]:
        vistas: set[str] = set()
        vagas = []
        for extra in ({}, {"geo": "latam"}):
            dados = self._get(self.URL, count=100, industry="dev", **extra).json()
            for j in dados.get("jobs", []):
                if j.get("url") in vistas:
                    continue
                publicada = data_iso(j.get("pubDate"))
                local = j.get("jobGeo") or ""
                if not self._aceita(local, publicada):
                    continue
                vistas.add(j["url"])
                nivel = j.get("jobLevel") or ""
                vagas.append(VagaBruta(
                    fonte=self.nome, url=j["url"], titulo=html_para_texto(j.get("jobTitle", "")),
                    empresa=html_para_texto(j.get("companyName", "")),
                    descricao=html_para_texto(j.get("jobDescription") or j.get("jobExcerpt", "")),
                    local=local, modelo_trabalho="remoto",
                    salario=_salario(j.get("salaryMin"), j.get("salaryMax"), j.get("salaryCurrency", ""),
                                     {"yearly": "ano", "monthly": "mês"}.get(j.get("salaryPeriod", ""), "")),
                    data_publicacao=publicada, idioma="en",
                    labels=[*(j.get("jobType") or []), *([nivel] if nivel and nivel.lower() != "any" else [])]))
        return vagas


class ColetorHimalayas(_ColetorHttp):
    """https://himalayas.app/jobs/api/search — ordenado por data; busca por termo, mundial e Brasil."""

    nome = "himalayas"
    URL = "https://himalayas.app/jobs/api/search"
    BUSCAS = ("backend developer", "full stack developer")

    def __init__(self, cliente: httpx.Client | None = None, idade_maxima_horas: float | None = None,
                 max_paginas: int = 2, so_brasil: bool = False):
        super().__init__(cliente, idade_maxima_horas)
        self.max_paginas = max_paginas
        # Com "incluir internacionais" desligado ainda vale buscar as vagas que o Himalayas marca como do Brasil
        self.so_brasil = so_brasil
        self.internacional = not so_brasil

    def coletar(self) -> list[VagaBruta]:
        vistas: set[str] = set()
        vagas = []
        for termo in self.BUSCAS:
            for escopo in ({"country": "BR"},) if self.so_brasil else ({"worldwide": "true"}, {"country": "BR"}):
                for pagina in range(1, self.max_paginas + 1):
                    jobs = self._get(self.URL, q=termo, sort="recent", page=pagina, **escopo).json().get("jobs", [])
                    chegou_nas_antigas = False
                    for j in jobs:
                        url = j.get("applicationLink") or j.get("guid") or ""
                        publicada = data_epoch(j.get("pubDate"))
                        local = ", ".join(j.get("locationRestrictions") or [])
                        if self._velha(publicada):
                            chegou_nas_antigas = True
                        if not url or url in vistas or not self._aceita(local, publicada):
                            continue
                        vistas.add(url)
                        vagas.append(VagaBruta(
                            fonte=self.nome, url=url, titulo=(j.get("title") or "").strip(),
                            empresa=(j.get("companyName") or "").strip(),
                            descricao=html_para_texto(j.get("description") or j.get("excerpt", "")),
                            local=local, modelo_trabalho="remoto",
                            salario=_salario(j.get("minSalary"), j.get("maxSalary"), j.get("currency", ""),
                                             {"annual": "ano", "monthly": "mês"}.get(j.get("salaryPeriod", ""), "")),
                            data_publicacao=publicada, idioma="en", labels=list(j.get("seniority") or [])))
                    if len(jobs) < 20 or chegou_nas_antigas:
                        break
        return vagas


class ColetorArbeitnow(_ColetorHttp):
    """https://www.arbeitnow.com/api/job-board-api — vagas da Europa; só as marcadas `remote` e sem restrição."""

    nome = "arbeitnow"
    URL = "https://www.arbeitnow.com/api/job-board-api"

    def coletar(self) -> list[VagaBruta]:
        vagas = []
        for j in self._get(self.URL).json().get("data", []):
            if not j.get("remote"):
                continue
            publicada = data_epoch(j.get("created_at"))
            local = j.get("location") or ""
            if not self._aceita(local, publicada):
                continue
            vagas.append(VagaBruta(
                fonte=self.nome, url=j["url"], titulo=html_para_texto(j.get("title", "")),
                empresa=html_para_texto(j.get("company_name", "")),
                descricao=html_para_texto(j.get("description", "")), local=local, modelo_trabalho="remoto",
                data_publicacao=publicada, labels=[*j.get("tags", []), *j.get("job_types", [])]))
        return vagas


class ColetorWWR(_ColetorHttp):
    """We Work Remotely (RSS por categoria). Título vem como 'Empresa: Cargo'; <region> diz onde pode morar."""

    nome = "weworkremotely"
    FEEDS = ("remote-full-stack-programming-jobs", "remote-back-end-programming-jobs",
             "remote-programming-jobs")

    def coletar(self) -> list[VagaBruta]:
        vistas: set[str] = set()
        vagas = []
        for feed in self.FEEDS:
            raiz = ET.fromstring(self._get(f"https://weworkremotely.com/categories/{feed}.rss").content)
            for item in raiz.iter("item"):
                url = (item.findtext("link") or item.findtext("guid") or "").strip()
                if not url or url in vistas:
                    continue
                publicada = _data_rfc822(item.findtext("pubDate"))
                local = (item.findtext("region") or "").strip()
                if not self._aceita(local, publicada):
                    continue
                vistas.add(url)
                titulo = html_para_texto(item.findtext("title") or "")
                empresa, sep, cargo = titulo.partition(": ")
                vagas.append(VagaBruta(
                    fonte=self.nome, url=url, titulo=(cargo if sep else titulo).strip(),
                    empresa=empresa.strip() if sep else "", descricao=html_para_texto(item.findtext("description") or ""),
                    local=local, modelo_trabalho="remoto", data_publicacao=publicada, idioma="en"))
        return vagas
