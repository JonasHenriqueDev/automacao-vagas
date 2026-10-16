"""Taxonomia de tecnologias: nome canônico -> sinônimos. Base do match sem LLM e da normalização
dos requisitos extraídos pela LLM (tabela de sinônimos, seção 5 do plano)."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def norm(texto: str) -> str:
    return sem_acento(texto).lower().strip()


# canônico -> sinônimos (já em minúsculas, sem acento)
TAXONOMIA: dict[str, list[str]] = {
    "javascript": ["javascript", "ecmascript", "es6"],
    "typescript": ["typescript"],
    "php": ["php"],
    "sql": ["sql", "t-sql", "pl/sql", "procedures sql", "stored procedures"],
    "java": ["java"],
    "python": ["python"],
    "golang": ["golang"],
    "ruby": ["ruby"],
    "c#": ["c#", "csharp"],
    ".net": [".net", "dotnet", "asp.net"],
    "c++": ["c++"],
    "rust": ["rust"],
    "kotlin": ["kotlin"],
    "node.js": ["node.js", "nodejs", "node js", "node"],
    "express": ["express", "express.js", "expressjs"],
    "nestjs": ["nestjs", "nest.js", "nest js"],
    "laravel": ["laravel"],
    "laravel sanctum": ["laravel sanctum", "sanctum"],
    "symfony": ["symfony"],
    "spring boot": ["spring boot", "springboot", "spring"],
    "django": ["django"],
    "flask": ["flask"],
    "fastapi": ["fastapi"],
    "rails": ["ruby on rails", "rails"],
    "react": ["react", "reactjs", "react.js"],
    "angular": ["angular", "angularjs"],
    "vue": ["vue", "vue.js", "vuejs"],
    "next.js": ["next.js", "nextjs", "next js"],
    "react native": ["react native", "react-native"],
    "material ui": ["material ui", "material-ui", "mui"],
    "tailwind": ["tailwind", "tailwindcss"],
    "html/css": ["html", "css", "html5", "css3"],
    "postgresql": ["postgresql", "postgres", "postgre"],
    "mysql": ["mysql", "mariadb"],
    "mongodb": ["mongodb", "mongo"],
    "redis": ["redis"],
    "sqlite": ["sqlite"],
    "sql server": ["sql server", "mssql"],
    "oracle": ["oracle"],
    "elasticsearch": ["elasticsearch", "elastic search"],
    "dynamodb": ["dynamodb"],
    "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"],
    "aws": ["aws", "amazon web services"],
    "aws lambda": ["aws lambda", "lambda", "serverless"],
    "amazon s3": ["amazon s3", "s3"],
    "azure": ["azure"],
    "gcp": ["gcp", "google cloud"],
    "terraform": ["terraform"],
    "ci/cd": ["ci/cd", "cicd", "ci-cd", "github actions", "gitlab ci", "jenkins"],
    "linux": ["linux"],
    "apis rest": ["apis rest", "api rest", "rest api", "rest apis", "restful", "api restful", "apis restful",
                  "web api", "web apis", "apis"],
    "graphql": ["graphql"],
    "webhooks": ["webhook", "webhooks"],
    "grpc": ["grpc"],
    "microsservicos": ["microservices", "microsservicos", "microservicos", "microservice", "microsservico"],
    "kafka": ["kafka"],
    "rabbitmq": ["rabbitmq"],
    "sqs": ["sqs", "aws sqs"],
    "arquitetura hexagonal": ["arquitetura hexagonal", "hexagonal architecture", "ports and adapters",
                              "hexagonal"],
    "clean architecture": ["clean architecture", "arquitetura limpa"],
    "ddd": ["ddd", "domain-driven design", "domain driven design"],
    "solid": ["solid principles", "principios solid", "principios s.o.l.i.d"],
    "tdd": ["tdd"],
    "testes": ["testes automatizados", "teste automatizado", "unit test", "unit tests", "testes unitarios",
               "jest", "phpunit", "pytest", "testing", "testes", "automated tests"],
    "git": ["git"],
    "github": ["github"],
    "gitlab": ["gitlab"],
    "jira": ["jira"],
    "postman": ["postman"],
    "swagger": ["swagger", "openapi"],
    "n8n": ["n8n"],
    "stripe": ["stripe"],
    "hubspot": ["hubspot"],
    "pipedrive": ["pipedrive"],
    "zapsign": ["zapsign"],
    "twilio": ["twilio"],
    "sms": ["sms"],
    "rcs": ["rcs"],
    "ia generativa": ["ia generativa", "generative ai", "genai", "llm", "llms", "agentes de ia", "ai agents",
                      "inteligencia artificial", "openai", "langchain"],
    "rbac": ["rbac"],
    "multitenancy": ["multitenancy", "multi-tenant", "multi tenant", "multitenant", "multi-tenancy"],
    "idempotencia": ["idempotencia", "idempotency", "idempotente", "idempotent"],
    "jwt": ["jwt"],
    "oauth": ["oauth", "oauth2", "oauth 2.0"],
    "qualidade de software": ["qualidade de software", "software quality"],
    "erp": ["erp"],
    "crm": ["crm"],
}

# Quem sabe X também atende vaga que pede Y.
IMPLICA: dict[str, list[str]] = {
    "nestjs": ["node.js", "typescript", "javascript"],
    "express": ["node.js", "javascript"],
    "next.js": ["react", "javascript"],
    "react native": ["react", "javascript"],
    "typescript": ["javascript"],
    "node.js": ["javascript"],
    "laravel": ["php"],
    "laravel sanctum": ["laravel", "php"],
    "spring boot": ["java"],
    "aws lambda": ["aws"],
    "amazon s3": ["aws"],
    "postgresql": ["sql"],
    "mysql": ["sql"],
    "apis rest": ["webhooks"],
    "webhooks": ["apis rest"],
    "material ui": ["react"],
    "github": ["git"],
    "gitlab": ["git"],
}

_RUIM = r"[\w+#]"


@lru_cache(maxsize=1)
def _padroes() -> list[tuple[str, re.Pattern[str]]]:
    # termos mais longos primeiro para "react native" ganhar de "react"
    pares = [(c, s) for c, sins in TAXONOMIA.items() for s in sins]
    pares.sort(key=lambda p: -len(p[1]))
    return [
        (c, re.compile(rf"(?<!{_RUIM}){re.escape(s)}(?!{_RUIM})"))
        for c, s in pares
    ]


def encontrar(texto: str) -> list[str]:
    """Tecnologias canônicas citadas no texto, na ordem da primeira ocorrência."""
    t = norm(texto)
    achados: dict[str, int] = {}
    ocupado: list[tuple[int, int]] = []
    for canon, pat in _padroes():
        for m in pat.finditer(t):
            if any(a < m.end() and m.start() < b for a, b in ocupado):
                continue  # parte de um termo maior já reconhecido ("react" dentro de "react native")
            ocupado.append((m.start(), m.end()))
            achados.setdefault(canon, m.start())
    return [c for c, _ in sorted(achados.items(), key=lambda kv: kv[1])]


def canonizar(termo: str) -> list[str]:
    """Canônicos de um termo livre (ex.: 'Node.js e Express'); sem correspondência, devolve o termo normalizado."""
    achados = encontrar(termo)
    return achados or [norm(termo)]


def com_implicacoes(canonicos: set[str]) -> set[str]:
    resultado = set(canonicos)
    pilha = list(canonicos)
    while pilha:
        for extra in IMPLICA.get(pilha.pop(), []):
            if extra not in resultado:
                resultado.add(extra)
                pilha.append(extra)
    return resultado
