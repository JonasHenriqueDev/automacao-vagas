from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

from app.config import PADRAO, Config
from app.curriculo.mestre import Pessoais, carregar_mestre
from app.db.sessao import criar_engine, criar_fabrica, criar_tabelas
from app.pipeline import Servicos

RAIZ = Path(__file__).resolve().parents[1]

PESSOAIS = {
    "nome": "Maria Ferreira Lacerda",
    "email": "maria.teste@exemplo.org",
    "telefone": "(11) 91234-5678",
    "localizacao": "Recife, PE, Brasil",
    "links": [{"rotulo": "GitHub", "url": "github.com/mariateste"}],
}


@pytest.fixture
def mestre():
    return carregar_mestre(RAIZ / "data" / "curriculo_mestre.example.yaml")


@pytest.fixture
def pessoais() -> Pessoais:
    return Pessoais.model_validate(PESSOAIS)


@pytest.fixture
def fabrica_servicos(tmp_path, monkeypatch):
    """Cria Servicos com banco/saída em tmp_path e LLMs/HTTP atendidos por um handler de teste."""
    for nome in ("GEMINI_API_KEY", "GROQ_API_KEY", "GITHUB_MODELS_TOKEN", "MISTRAL_API_KEY",
                 "CLOUDFLARE_API_TOKEN", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(nome, raising=False)
    pessoais_yaml = tmp_path / "pessoais.yaml"
    pessoais_yaml.write_text(yaml.safe_dump(PESSOAIS, allow_unicode=True), encoding="utf-8")
    mestre_yaml = tmp_path / "curriculo_mestre.yaml"  # cópia: o painel edita este arquivo, nunca o de exemplo
    shutil.copy(RAIZ / "data" / "curriculo_mestre.example.yaml", mestre_yaml)

    def criar(handler=None, ajustes: dict | None = None) -> Servicos:
        dados = copy.deepcopy(PADRAO)
        dados["caminhos"].update({
            "banco": str(tmp_path / "vagas.db"), "saida": str(tmp_path / "saida"),
            "mestre": str(mestre_yaml), "pessoais": str(pessoais_yaml),
            "templates_cv": str(RAIZ / "templates_cv"),
        })
        for nome in ("gupy", "quadros", "programathor", "vagas_com", "remotive", "remoteok", "jobicy",
                     "himalayas", "arbeitnow", "weworkremotely"):
            dados["fontes"][nome]["ativo"] = False  # os testes ligam só as fontes que exercitam
        dados["painel"]["cookie_seguro"] = False
        dados["llm"]["backoff_segundos"] = 0
        for k, v in (ajustes or {}).items():
            dados[k].update(v) if isinstance(v, dict) else dados.__setitem__(k, v)
        cfg = Config(dados, raiz=RAIZ)
        engine = criar_engine(tmp_path / "vagas.db")
        criar_tabelas(engine)
        fabrica = criar_fabrica(engine)
        cfg.ligar_banco(fabrica)
        transporte = httpx.MockTransport(handler) if handler else None
        return Servicos(cfg, fabrica, transporte=transporte)

    return criar


def resposta_chat(conteudo: str | dict, status: int = 200) -> httpx.Response:
    texto = conteudo if isinstance(conteudo, str) else json.dumps(conteudo, ensure_ascii=False)
    return httpx.Response(status, json={"choices": [{"message": {"content": texto}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
