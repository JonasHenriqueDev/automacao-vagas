"""Quadros públicos de vagas por empresa: Greenhouse, Lever e Ashby (APIs JSON abertas, sem login).

Cada quadro é uma fonte (`greenhouse:quintoandar`). Empresas globais publicam vagas do mundo todo, então a
localização decide: com "incluir vagas internacionais" desligado ficam só as do Brasil/LATAM; ligado, entram
também as sem restrição de país ("Remote", "Worldwide").
"""
from __future__ import annotations

import httpx

from .. import registro
from .base import (USER_AGENT, VagaBruta, data_epoch, data_iso, elegivel_do_brasil, html_para_texto,
                   idade_horas, local_brasil)

PLATAFORMAS = ("greenhouse", "lever", "ashby")


class _ColetorQuadro:
    plataforma = ""
    prefiltrar_titulo = True

    def __init__(self, quadro: str, internacional: bool = False, cliente: httpx.Client | None = None,
                 idade_maxima_horas: float | None = None):
        self.quadro = quadro
        self.nome = f"{self.plataforma}:{quadro}"
        self.internacional = False  # o quadro roda sempre; `incluir_internacional` só afina o filtro de local
        self.aceita_internacional = internacional
        self.idade_maxima = idade_maxima_horas
        self._cliente = cliente
        self.stats: dict[str, int] = {"lidas": 0, "fora_do_pais": 0, "antigas": 0, "paginas": 0}

    def _baixar(self, url: str, **params: str) -> object | None:
        if self._cliente is None:
            self._cliente = httpx.Client(timeout=30)
        r = self._cliente.get(url, params=params or None, headers={"User-Agent": USER_AGENT})
        self.stats["paginas"] += 1
        if r.status_code == 404:  # a empresa trocou/desativou o quadro: avisa no log, sem alarmar o Discord
            registro.log(f"{self.nome}: quadro não encontrado (404); confira o nome em Configurações.",
                         "aviso", "coleta")
            return None
        r.raise_for_status()
        return r.json()

    def _passa(self, local: str, publicada) -> bool:
        self.stats["lidas"] += 1
        if not (local_brasil(local) or (self.aceita_internacional and elegivel_do_brasil(local))):
            self.stats["fora_do_pais"] += 1
            return False
        idade = idade_horas(publicada)
        if self.idade_maxima is not None and idade is not None and idade > self.idade_maxima:
            self.stats["antigas"] += 1
            return False
        return True

    def coletar(self) -> list[VagaBruta]:  # pragma: no cover - interface
        raise NotImplementedError


def _nome_empresa(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


class ColetorGreenhouse(_ColetorQuadro):
    plataforma = "greenhouse"

    def coletar(self) -> list[VagaBruta]:
        dados = self._baixar(f"https://boards-api.greenhouse.io/v1/boards/{self.quadro}/jobs", content="true")
        vagas = []
        for j in (dados or {}).get("jobs", []):  # type: ignore[union-attr]
            local = (j.get("location") or {}).get("name", "")
            publicada = data_iso(j.get("first_published")) or data_iso(j.get("updated_at"))
            if not self._passa(local, publicada):
                continue
            vagas.append(VagaBruta(
                fonte=self.nome, url=j["absolute_url"], titulo=(j.get("title") or "").strip(),
                empresa=j.get("company_name") or _nome_empresa(self.quadro),
                descricao=html_para_texto(j.get("content", "")), local=local, data_publicacao=publicada,
                labels=[d["name"] for d in j.get("departments", []) if d.get("name")][:4],
                idioma=j.get("language") or ""))
        return vagas


class ColetorLever(_ColetorQuadro):
    plataforma = "lever"

    @staticmethod
    def _descricao(j: dict) -> str:
        partes = [j.get("descriptionPlain") or html_para_texto(j.get("description", ""))]
        for lista in j.get("lists", []):
            partes.append(f"{lista.get('text', '')}\n{html_para_texto(lista.get('content', ''))}")
        partes.append(j.get("additionalPlain") or html_para_texto(j.get("additional", "")))
        return "\n\n".join(p.strip() for p in partes if p and p.strip())

    def coletar(self) -> list[VagaBruta]:
        dados = self._baixar(f"https://api.lever.co/v0/postings/{self.quadro}", mode="json")
        vagas = []
        for j in dados or []:  # type: ignore[union-attr]
            cat = j.get("categories") or {}
            local = ", ".join([*(cat.get("allLocations") or [cat.get("location", "")]), j.get("country") or ""])
            publicada = data_epoch(j.get("createdAt"))
            if not self._passa(local, publicada):
                continue
            modelo = {"remote": "remoto", "hybrid": "hibrido", "on-site": "presencial"}.get(
                j.get("workplaceType", ""), "desconhecido")
            vagas.append(VagaBruta(
                fonte=self.nome, url=j["hostedUrl"], titulo=(j.get("text") or "").strip(),
                empresa=_nome_empresa(self.quadro), descricao=self._descricao(j), local=local,
                modelo_trabalho=modelo, data_publicacao=publicada,
                labels=[x for x in (cat.get("team"), cat.get("department"), cat.get("commitment")) if x]))
        return vagas


class ColetorAshby(_ColetorQuadro):
    plataforma = "ashby"

    def coletar(self) -> list[VagaBruta]:
        dados = self._baixar(f"https://api.ashbyhq.com/posting-api/job-board/{self.quadro}",
                             includeCompensation="true")
        vagas = []
        for j in (dados or {}).get("jobs", []):  # type: ignore[union-attr]
            if j.get("isListed") is False:
                continue
            local = ", ".join([j.get("location") or "",
                               *(s.get("location", "") for s in j.get("secondaryLocations", []))]).strip(", ")
            publicada = data_iso(j.get("publishedAt"))
            if not self._passa(local, publicada):
                continue
            modelo = {"remote": "remoto", "hybrid": "hibrido", "onsite": "presencial"}.get(
                str(j.get("workplaceType", "")).lower(), "remoto" if j.get("isRemote") else "desconhecido")
            vagas.append(VagaBruta(
                fonte=self.nome, url=j["jobUrl"], titulo=(j.get("title") or "").strip(),
                empresa=_nome_empresa(self.quadro),
                descricao=j.get("descriptionPlain") or html_para_texto(j.get("descriptionHtml", "")),
                local=local, modelo_trabalho=modelo, data_publicacao=publicada,
                salario=((j.get("compensation") or {}).get("compensationTierSummary") or ""),
                labels=[x for x in (j.get("department"), j.get("team"), j.get("employmentType")) if x]))
        return vagas


COLETORES = {"greenhouse": ColetorGreenhouse, "lever": ColetorLever, "ashby": ColetorAshby}


def parse_quadro(linha: str) -> tuple[str, str] | None:
    """'greenhouse:quintoandar' -> ('greenhouse', 'quintoandar'); None se inválido."""
    plataforma, _, quadro = linha.strip().partition(":")
    plataforma, quadro = plataforma.strip().lower(), quadro.strip()
    if plataforma not in COLETORES or not quadro or not all(c.isalnum() or c in "-_." for c in quadro):
        return None
    return plataforma, quadro
