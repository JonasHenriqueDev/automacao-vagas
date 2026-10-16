"""Extração estruturada dos requisitos da vaga com LLM (tarefa `extracao`). O texto da vaga vai
sempre como dado delimitado (seção 4.7)."""
from __future__ import annotations

import json
import re

from ..llm.rodizio import Rodizio
from .requisitos import RequisitosVaga
from .seguranca import AVISO_PROMPT, delimitar

MAX_CHARS = 9000

SISTEMA = (
    "Você extrai requisitos de vagas de emprego e responde SOMENTE com um objeto JSON válido, sem texto "
    "fora dele. " + AVISO_PROMPT
)

INSTRUCAO = """Extraia da vaga o JSON com exatamente estas chaves:
{
  "obrigatorios": ["tecnologias/ferramentas/habilidades técnicas exigidas, uma por item, nome curto (ex.: \\"Node.js\\", \\"PostgreSQL\\")"],
  "desejaveis": ["tecnologias/habilidades desejáveis, diferenciais ou 'nice to have'"],
  "senioridade": "junior | pleno | senior | desconhecida",
  "modelo_trabalho": "remoto | hibrido | presencial | desconhecido",
  "local": "restrição de país/região, se houver, senão string vazia",
  "salario": "faixa salarial se citada, senão string vazia",
  "idioma": "pt | en  (idioma em que a vaga foi escrita)"
}
Regras: liste só tecnologias, ferramentas, metodologias e conhecimentos técnicos (não liste anos de experiência,
soft skills genéricas nem benefícios). Não invente itens que não estejam no texto."""


def _json(texto: str) -> dict:
    t = texto.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ValueError("resposta sem JSON")
        return json.loads(m.group(0))


def validar(texto: str) -> RequisitosVaga:
    try:
        return RequisitosVaga.model_validate(_json(texto))
    except Exception as e:  # pydantic / json -> o rodízio trata ValueError como saída inválida
        raise ValueError(str(e)[:200]) from e


def extrair(rodizio: Rodizio, titulo: str, empresa: str, descricao: str) -> tuple[RequisitosVaga, str, str]:
    """Devolve (requisitos, provedor, modelo). Levanta ProvedoresEsgotados se não houver provedor."""
    corpo = f"Título: {titulo}\nEmpresa: {empresa}\n\n{descricao[:MAX_CHARS]}"
    mensagens = [
        {"role": "system", "content": SISTEMA},
        {"role": "user", "content": INSTRUCAO + "\n\n" + delimitar(corpo)},
    ]
    resp, req = rodizio.executar("extracao", mensagens, json_mode=True, validador=validar, max_tokens=900)
    return req, resp.provedor, resp.modelo  # type: ignore[return-value]
