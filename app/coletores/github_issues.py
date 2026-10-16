"""Coletor de repositórios de vagas no GitHub (backend-br/vagas etc.), pela API oficial de issues."""
from __future__ import annotations

import re
from datetime import datetime

import httpx

from ..match.taxonomia import norm
from .base import USER_AGENT, VagaBruta, idade_horas

API = "https://api.github.com"

_ROTULOS_EMPRESA = re.compile(
    r"^\W*(?:nome da empresa|empresa|company(?: name)?)\W*[:\-–]\s*(.+)$", re.I | re.M
)
_MAIUSC = "A-ZÀ-ÝÇ0-9"
# "... na Acme", "... at Acme", "... @ Acme", "... | Acme", "... - Acme", "... (Acme)" no fim do título
_TITULO_EMPRESA = [
    re.compile(rf"\s(?:na|no|at|@)\s+([{_MAIUSC}][^\[\]()|]{{1,60}}?)\s*$"),
    re.compile(rf"\s[|\-–—]\s*([{_MAIUSC}][^\[\]()|/]{{1,40}}?)\s*$"),
    re.compile(rf"\(([{_MAIUSC}][\w .&'-]{{1,40}})\)\s*$"),
]
_NAO_EMPRESA = re.compile(r"^(remoto|remote|pleno|j[uú]nior|junior|usd|net|\.net|clt|pj|brasil|brazil)$", re.I)


def _empresa(titulo: str, corpo: str) -> str:
    m = _ROTULOS_EMPRESA.search(corpo or "")
    if m:
        return re.sub(r"[*_`#]", "", m.group(1)).strip()[:120]
    titulo = re.sub(r"^\s*(\[[^\]]+\]\s*)+", "", titulo).strip()
    for padrao in _TITULO_EMPRESA:
        m = padrao.search(titulo)
        if m and not _NAO_EMPRESA.match(m.group(1).strip()):
            return m.group(1).strip()
    return ""


def _modelo_pelos_labels(labels: list[str]) -> str:
    n = [norm(re.sub(r"[^\w\s%-]", "", l)) for l in labels]
    if any("hibrido" in l or "hybrid" in l for l in n):
        return "hibrido"
    if any("presencial" in l for l in n):
        return "presencial"
    if any("remoto" in l or "remote" in l for l in n):
        return "remoto"
    return "desconhecido"


def issue_para_vaga(issue: dict, repo: str) -> VagaBruta:
    labels = [l["name"] if isinstance(l, dict) else str(l) for l in issue.get("labels", [])]
    corpo = issue.get("body") or ""
    titulo = (issue.get("title") or "").strip()
    local = "Exterior" if any("exterior" in norm(l) or "fora-do-pais" in norm(l) for l in labels) else ""
    publicado = issue.get("created_at")
    return VagaBruta(
        fonte=f"github:{repo}",
        url=issue["html_url"],
        titulo=re.sub(r"^\s*(\[[^\]]+\]\s*)+", "", titulo) or titulo,
        empresa=_empresa(titulo, corpo),
        descricao=corpo,
        local=local,
        modelo_trabalho=_modelo_pelos_labels(labels),
        data_publicacao=datetime.fromisoformat(publicado.replace("Z", "+00:00")).replace(tzinfo=None)
        if publicado else None,
        labels=labels + re.findall(r"^\s*\[([^\]]+)\]", titulo),
    )


class ColetorGithubIssues:
    def __init__(self, repo: str, labels: list[str], labels_excluir: list[str], token: str = "",
                 max_paginas: int = 3, cliente: httpx.Client | None = None,
                 idade_maxima_horas: float | None = None):
        self.nome = f"github:{repo}"
        self.repo = repo
        self.labels = labels
        self.excluir = {norm(l) for l in labels_excluir}
        self.token = token
        self.max_paginas = max_paginas
        self.idade_maxima = idade_maxima_horas
        self._cliente = cliente
        self.stats: dict[str, int] = {"lidas": 0, "prs": 0, "label_excluida": 0, "antigas": 0, "paginas": 0}

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
             "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def coletar(self) -> list[VagaBruta]:
        """Issues abertas, da mais nova para a mais antiga (por data de CRIAÇÃO, não de atualização:
        uma vaga de 2025 comentada hoje continua sendo velha). Para ao passar da idade máxima."""
        cliente = self._cliente or httpx.Client(timeout=30)
        vagas: list[VagaBruta] = []
        try:
            for pagina in range(1, self.max_paginas + 1):
                params: dict[str, str | int] = {"state": "open", "per_page": 100, "page": pagina,
                                                "sort": "created", "direction": "desc"}
                if self.labels:
                    params["labels"] = ",".join(self.labels)
                r = cliente.get(f"{API}/repos/{self.repo}/issues", params=params, headers=self._headers())
                if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
                    raise RuntimeError(f"Limite de requisições do GitHub esgotado ({self.repo}).")
                r.raise_for_status()
                itens = r.json()
                self.stats["paginas"] += 1
                chegou_nas_antigas = False
                for issue in itens:
                    self.stats["lidas"] += 1
                    if "pull_request" in issue:
                        self.stats["prs"] += 1
                        continue
                    nomes = {norm(l["name"]) for l in issue.get("labels", []) if isinstance(l, dict)}
                    if nomes & self.excluir:
                        self.stats["label_excluida"] += 1
                        continue
                    vaga = issue_para_vaga(issue, self.repo)
                    idade = idade_horas(vaga.data_publicacao)
                    if self.idade_maxima is not None and idade is not None and idade > self.idade_maxima:
                        self.stats["antigas"] += 1
                        chegou_nas_antigas = True
                        continue
                    vagas.append(vaga)
                if len(itens) < 100 or chegou_nas_antigas:
                    break  # ordenado por criação: o resto da lista é mais velho ainda
        finally:
            if self._cliente is None:
                cliente.close()
        return vagas
