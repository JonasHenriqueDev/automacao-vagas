"""Gupy (portal.gupy.io): maior plataforma de recrutamento do Brasil.

Usa o mesmo endpoint JSON que o próprio portal chama no navegador (`/api/job-search/jobs`). Ele não é uma API
documentada e pode mudar: se a Gupy alterar o formato, o coletor falha com erro claro e os outros seguem.
Consultas leves (poucos termos, ordenadas pela mais recente, parando na primeira vaga velha) e com pausa entre
chamadas, para não sobrecarregar o portal.
"""
from __future__ import annotations

import time

import httpx

from .base import USER_AGENT, VagaBruta, data_iso, idade_horas

URL = "https://portal.gupy.io/api/job-search/jobs"
_MODELO = {"remote": "remoto", "hybrid": "hibrido", "on-site": "presencial", "onsite": "presencial"}
# Tipos que não são uma vaga aberta de verdade para este caso de uso
_TIPOS_IGNORADOS = {"vacancy_type_apprentice", "vacancy_type_internship", "vacancy_type_summer",
                    "vacancy_type_talent_pool", "vacancy_type_trainee", "vacancy_type_volunteer"}


class ColetorGupy:
    nome = "gupy"
    internacional = False
    prefiltrar_titulo = True

    def __init__(self, termos: list[str], max_paginas: int = 3, por_pagina: int = 50,
                 cliente: httpx.Client | None = None, idade_maxima_horas: float | None = None,
                 pausa_segundos: float = 0.5):
        self.termos = [t for t in termos if t.strip()]
        self.max_paginas = max_paginas
        self.por_pagina = por_pagina
        self.idade_maxima = idade_maxima_horas
        self.pausa = pausa_segundos
        self._cliente = cliente
        self.stats: dict[str, int] = {"lidas": 0, "antigas": 0, "tipo_ignorado": 0, "paginas": 0}

    def _pagina(self, termo: str, offset: int) -> list[dict]:
        if self._cliente is None:
            self._cliente = httpx.Client(timeout=30)
        r = self._cliente.get(URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, params={
            "jobName": termo, "workplaceType": "remote", "limit": self.por_pagina, "offset": offset,
            "sortBy": "publishedDate", "sortOrder": "desc"})
        r.raise_for_status()
        try:
            dados = r.json()["data"]
        except (ValueError, KeyError, TypeError) as e:
            raise RuntimeError("Gupy: formato de resposta inesperado (o portal pode ter mudado a API).") from e
        self.stats["paginas"] += 1
        return dados

    def coletar(self) -> list[VagaBruta]:
        vistas: set[int] = set()
        vagas: list[VagaBruta] = []
        for termo in self.termos:
            for pagina in range(self.max_paginas):
                if self.pausa:
                    time.sleep(self.pausa)
                itens = self._pagina(termo, pagina * self.por_pagina)
                chegou_nas_antigas = False
                for j in itens:
                    self.stats["lidas"] += 1
                    publicada = data_iso(j.get("publishedDate"))
                    idade = idade_horas(publicada)
                    if self.idade_maxima is not None and idade is not None and idade > self.idade_maxima:
                        self.stats["antigas"] += 1
                        chegou_nas_antigas = True
                        continue
                    if j.get("type") in _TIPOS_IGNORADOS:
                        self.stats["tipo_ignorado"] += 1
                        continue
                    if j["id"] in vistas:
                        continue
                    vistas.add(j["id"])
                    local = ", ".join(p for p in (j.get("city"), j.get("state")) if p)
                    vagas.append(VagaBruta(
                        fonte=self.nome, url=j["jobUrl"], titulo=(j.get("name") or "").strip(),
                        empresa=(j.get("careerPageName") or "").strip(), descricao=j.get("description") or "",
                        local=local or "Remoto", modelo_trabalho=_MODELO.get(j.get("workplaceType", ""), "desconhecido"),
                        data_publicacao=publicada, idioma="pt"))
                if len(itens) < self.por_pagina or chegou_nas_antigas:
                    break
        return vagas
