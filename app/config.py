"""Configuração: valor salvo no painel (banco) > config.yaml > padrão.

Segredos: valor salvo no painel (criptografado com Fernet) > variável de ambiente (.env).
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import yaml
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy.orm import Session, sessionmaker

from .db.models import Configuracao

RAIZ = Path(__file__).resolve().parents[1]

PADRAO: dict[str, Any] = {
    "agendamento": {"cron": "0 */3 * * *", "fuso": "America/Sao_Paulo"},
    "caminhos": {
        "banco": "data/vagas.db",
        "saida": "saida",
        "mestre": "data/curriculo_mestre.yaml",
        "pessoais": "data/dados_pessoais.yaml",
        "templates_cv": "templates_cv",
    },
    "match": {
        "limite": 70,
        "pesos": {"obrigatorios": 0.6, "desejaveis": 0.2, "senioridade": 0.1, "local_salario": 0.1},
        "peso_academico": 0.5,
        "usar_llm_na_extracao": True,
    },
    "filtros": {
        "idade_maxima_horas": 24,  # só vagas abertas há no máximo isto (padrão: 24 h)
        "palavras_bloqueadas": [],
        "senioridade_bloqueada": [
            "senior", "sênior", "sr.", "staff", "lead", "principal", "especialista",
            "estagio", "estágio", "trainee", "intern", "tech lead", "head",
        ],
        "senioridade_aceita": ["junior", "júnior", "jr", "pleno", "mid", "mid-level", "pl"],
        "areas_bloqueadas": ["front-end", "frontend", "front end", "mobile", "ios", "android", "ui designer"],
        "areas_aceitas": ["full stack", "fullstack", "full-stack", "back-end", "backend", "back end",
                          "integra", "api", "software", "desenvolvedor", "developer", "engineer",
                          "engenheiro", "programador", "dev"],
        "restricoes_pais": [
            r"\bus only\b", r"\busa only\b", r"\bu\.s\. only\b", r"must be (based|located|living) in",
            r"authori[sz]ed to work in", r"work authori[sz]ation", r"\bright to work in\b",
            r"\bresidents? of\b", r"\bonly (candidates|applicants) (in|from|based)",
            r"\bus citizens?\b", r"\bgreen card\b", r"\bsecurity clearance\b",
        ],
        "aceita_pais": [r"worldwide", r"anywhere", r"latam", r"latin america", r"americas",
                        r"\bbrazil\b", r"\bbrasil\b", r"\bbr\b"],
    },
    "fontes": {
        "github_issues": {
            "ativo": True,
            "min_intervalo_horas": 3,
            "max_paginas": 3,
            "repos": [
                {"repo": "backend-br/vagas", "labels": ["Remoto"]},
                {"repo": "frontendbr/vagas", "labels": ["Remoto"]},
                {"repo": "phpdevbr/vagas", "labels": []},
                {"repo": "nodejsdevbr/vagas", "labels": []},
            ],
            "labels_excluir": ["Sênior", "Senior", "Especialista", "Estagiário", "Presencial", "Híbrido",
                               "Hibrido", "Stale"],
        },
        "email": {
            "ativo": False,
            "min_intervalo_horas": 3,
            "servidor": "imap.gmail.com",
            "porta": 993,
            "usuario": "",
            "pasta": "INBOX",
            "remetentes": ["jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com"],
            "assume_remoto": True,
            "marcar_como_lido": True,
            "max_emails": 50,
        },
    },
    "llm": {
        "tentativas_temporarias": 2,
        "backoff_segundos": 2,
        "provedores": {
            "gemini": {"ativo": True, "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                       "chave_env": "GEMINI_API_KEY"},
            "groq": {"ativo": True, "base_url": "https://api.groq.com/openai/v1", "chave_env": "GROQ_API_KEY"},
            "github": {"ativo": True, "base_url": "https://models.github.ai/inference",
                       "chave_env": "GITHUB_MODELS_TOKEN"},
            "mistral": {"ativo": True, "base_url": "https://api.mistral.ai/v1", "chave_env": "MISTRAL_API_KEY"},
            "cloudflare": {"ativo": True,
                           "base_url": "https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1",
                           "chave_env": "CLOUDFLARE_API_TOKEN"},
            "openrouter": {"ativo": True, "base_url": "https://openrouter.ai/api/v1",
                           "chave_env": "OPENROUTER_API_KEY"},
        },
        # Nomes de modelo mudam com frequência: ajuste no painel (botão "Testar" lista os disponíveis).
        "ranking": {
            "reescrita_cv": [
                {"provedor": "gemini", "modelo": "gemini-flash-latest", "ativo": True},
                {"provedor": "groq", "modelo": "openai/gpt-oss-120b", "ativo": True},
                {"provedor": "github", "modelo": "openai/gpt-4.1", "ativo": True},
                {"provedor": "mistral", "modelo": "mistral-large-latest", "ativo": True},
                {"provedor": "openrouter", "modelo": "openai/gpt-oss-120b:free", "ativo": True},
            ],
            "extracao": [
                {"provedor": "groq", "modelo": "llama-3.3-70b-versatile", "ativo": True},
                {"provedor": "cloudflare", "modelo": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "ativo": True},
                {"provedor": "gemini", "modelo": "gemini-flash-lite-latest", "ativo": True},
                {"provedor": "mistral", "modelo": "mistral-small-latest", "ativo": True},
                {"provedor": "github", "modelo": "openai/gpt-4.1-mini", "ativo": True},
                {"provedor": "openrouter", "modelo": "openai/gpt-oss-20b:free", "ativo": True},
            ],
        },
    },
    "discord": {"ativo": True, "maximo_top": 3},
    "painel": {
        "host": "127.0.0.1",
        "porta": 8090,
        "url_publica": "https://vagasautomacao.duckdns.org",
        "cookie_seguro": True,
        "max_tentativas_login": 5,
        "janela_login_minutos": 15,
    },
}

#: chaves que o painel pode sobrescrever (valor guardado como JSON em `configuracoes`)
CHAVES_EDITAVEIS = {
    "agendamento.cron",
    "match.limite",
    "match.pesos",
    "llm.ranking.reescrita_cv",
    "llm.ranking.extracao",
    "llm.provedores",
    "filtros.palavras_bloqueadas",
    "filtros.idade_maxima_horas",
    "fontes.github_issues.repos",
    "fontes.email.servidor",
    "fontes.email.porta",
    "fontes.email.usuario",
    "fontes.email.pasta",
    "fontes.email.ativo",
    "discord.ativo",
}

SEGREDOS_CONHECIDOS = [
    "GEMINI_API_KEY", "GROQ_API_KEY", "GITHUB_MODELS_TOKEN", "MISTRAL_API_KEY",
    "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OPENROUTER_API_KEY",
    "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL", "EMAIL_SENHA",
]


def _mesclar(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _mesclar(base[k], v)
        else:
            base[k] = v
    return base


class Segredos:
    """Leitura/gravação de segredos: banco (criptografado) tem prioridade sobre o ambiente."""

    def __init__(self, fabrica: sessionmaker[Session] | None):
        self._fabrica = fabrica

    @staticmethod
    def _fernet() -> Fernet | None:
        chave = os.environ.get("CHAVE_MESTRA", "").strip()
        if not chave:
            return None
        try:
            return Fernet(chave.encode())
        except ValueError:
            return None

    @property
    def pode_gravar(self) -> bool:
        return self._fabrica is not None and self._fernet() is not None

    def obter(self, nome: str) -> str:
        if self._fabrica is not None:
            f = self._fernet()
            with self._fabrica() as s:
                reg = s.get(Configuracao, f"segredo.{nome}")
                if reg and reg.valor and f:
                    try:
                        return f.decrypt(reg.valor.encode()).decode()
                    except InvalidToken:
                        pass  # chave mestra trocada: cai para o ambiente
        return os.environ.get(nome, "")

    def gravar(self, nome: str, valor: str) -> None:
        f = self._fernet()
        if f is None or self._fabrica is None:
            raise RuntimeError("CHAVE_MESTRA ausente ou inválida no .env: não é possível gravar segredos.")
        token = f.encrypt(valor.encode()).decode()
        with self._fabrica() as s:
            reg = s.get(Configuracao, f"segredo.{nome}")
            if reg is None:
                s.add(Configuracao(chave=f"segredo.{nome}", valor=token, segredo=True))
            else:
                reg.valor = token
            s.commit()

    def remover(self, nome: str) -> None:
        if self._fabrica is None:
            return
        with self._fabrica() as s:
            reg = s.get(Configuracao, f"segredo.{nome}")
            if reg:
                s.delete(reg)
                s.commit()

    def final(self, nome: str) -> str:
        """Só os últimos 4 caracteres do segredo salvo NO PAINEL (nunca o valor inteiro)."""
        if self._fabrica is None:
            return ""
        f = self._fernet()
        with self._fabrica() as s:
            reg = s.get(Configuracao, f"segredo.{nome}")
            if not reg or not reg.valor or not f:
                return ""
            try:
                v = f.decrypt(reg.valor.encode()).decode()
            except InvalidToken:
                return ""
        return f"••••{v[-4:]}"

    @staticmethod
    def no_ambiente(nome: str) -> bool:
        return bool(os.environ.get(nome, ""))


class Config:
    def __init__(self, dados: dict[str, Any], raiz: Path = RAIZ, fabrica: sessionmaker[Session] | None = None):
        self._dados = dados
        self.raiz = raiz
        self._fabrica = fabrica
        self.segredos = Segredos(fabrica)

    # ----- carga -----
    @classmethod
    def carregar(cls, caminho: str | Path | None = None, raiz: Path = RAIZ) -> "Config":
        load_dotenv(raiz / ".env")
        dados = copy.deepcopy(PADRAO)
        caminho = Path(caminho) if caminho else raiz / "config.yaml"
        if caminho.exists():
            extra = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
            _mesclar(dados, extra)
        return cls(dados, raiz)

    def ligar_banco(self, fabrica: sessionmaker[Session]) -> None:
        self._fabrica = fabrica
        self.segredos = Segredos(fabrica)

    # ----- leitura -----
    def _yaml(self, caminho: str, padrao: Any = None) -> Any:
        no: Any = self._dados
        for parte in caminho.split("."):
            if not isinstance(no, dict) or parte not in no:
                return padrao
            no = no[parte]
        return no

    def obter(self, caminho: str, padrao: Any = None) -> Any:
        if self._fabrica is not None and caminho in CHAVES_EDITAVEIS:
            with self._fabrica() as s:
                reg = s.get(Configuracao, f"cfg.{caminho}")
                if reg is not None:
                    try:
                        return json.loads(reg.valor)
                    except json.JSONDecodeError:
                        pass
        valor = self._yaml(caminho, padrao)
        return copy.deepcopy(valor)

    def definir(self, caminho: str, valor: Any) -> None:
        if caminho not in CHAVES_EDITAVEIS:
            raise KeyError(f"Configuração não editável pelo painel: {caminho}")
        if self._fabrica is None:
            raise RuntimeError("Banco não ligado à configuração.")
        with self._fabrica() as s:
            reg = s.get(Configuracao, f"cfg.{caminho}")
            texto = json.dumps(valor, ensure_ascii=False)
            if reg is None:
                s.add(Configuracao(chave=f"cfg.{caminho}", valor=texto))
            else:
                reg.valor = texto
            s.commit()

    def caminho(self, nome: str) -> Path:
        p = Path(self._yaml(f"caminhos.{nome}"))
        return p if p.is_absolute() else self.raiz / p
