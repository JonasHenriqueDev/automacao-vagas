"""Melhoria de um bullet escrito pelo usuário (aba Gerar CV, «O que você fez com isso?»).

A LLM só reescreve o que o usuário contou, sem acrescentar fatos: a saída passa pela mesma regra
anti-invenção do CV (nenhum número ou tecnologia que não esteja no texto original ou no nome da skill).
O texto vai como veio do usuário: se citar nome de empresa ou dado pessoal, a verificação de privacidade barra.
"""
from __future__ import annotations

import json
import re

from ..llm.rodizio import Rodizio
from ..match import taxonomia
from .mestre import Mestre, Pessoais
from .personalizar import MAX_BULLET_CHARS, _numeros, _termos_ok, verificar_privacidade

SISTEMA = (
    "Você melhora a redação de um item de currículo escrito pelo próprio candidato. REGRA INEGOCIÁVEL: use "
    "somente os fatos do texto dele; não invente tecnologias, números, resultados, empresas ou responsabilidades. "
    "Responda SOMENTE com um objeto JSON válido."
)

_INSTRUCAO = """Reescreva o texto abaixo como UM bullet de currículo em português do Brasil: comece com verbo de ação
no passado ou substantivo de ação (ex.: "Implementação de…"), seja direto e específico, destaque a tecnologia
«{skill}» e o resultado, se o texto citar algum. Corrija ortografia e clareza. Mantenha todos os números do texto
exatamente como estão e não acrescente nenhum. No máximo {max} caracteres, uma única frase ou duas curtas.
Responda com: {{"texto": "bullet reescrito"}}

TEXTO DO CANDIDATO:
{texto}"""


def _validador(original: str, skill: str):
    permitidos = taxonomia.com_implicacoes(set(taxonomia.encontrar(f"{original} {skill}")))

    def validar(resposta: str) -> str:
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", resposta.strip(), flags=re.I)
        try:
            dados = json.loads(t)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", t, re.S)
            if not m:
                raise ValueError("resposta sem JSON") from None
            dados = json.loads(m.group(0))
        texto = re.sub(r"\s+", " ", str((dados or {}).get("texto") or "")).strip() if isinstance(dados, dict) else ""
        if not texto or len(texto) > MAX_BULLET_CHARS:
            raise ValueError("texto vazio ou longo demais")
        if novos := _numeros(texto) - _numeros(original):
            raise ValueError(f"números inventados: {', '.join(sorted(novos))}")
        if fora := _termos_ok(texto, permitidos):
            raise ValueError(f"tecnologias fora do texto original: {', '.join(fora)}")
        return texto

    return validar


def melhorar_bullet(rodizio: Rodizio, mestre: Mestre, pessoais: Pessoais, skill: str, texto: str) -> str:
    """Devolve o bullet reescrito. Levanta ErroPrivacidade, ProvedoresEsgotados ou ValueError (entrada vazia)."""
    texto = " ".join(texto.split())
    if not texto:
        raise ValueError("Escreva primeiro o que você fez; a LLM só melhora o que você contou.")
    mensagens = [
        {"role": "system", "content": SISTEMA},
        {"role": "user", "content": _INSTRUCAO.format(skill=skill, max=MAX_BULLET_CHARS, texto=texto)},
    ]
    verificar_privacidade(mensagens, pessoais, mestre)
    _, melhorado = rodizio.executar("reescrita_cv", mensagens, json_mode=True, validador=_validador(texto, skill),
                                    temperatura=0.3, max_tokens=400)
    return str(melhorado)
