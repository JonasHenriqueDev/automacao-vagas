"""Personalização do CV com LLM + validação anti-invenção (seções 3 e 7).

Privacidade: a LLM recebe só conteúdo profissional anonimizado (sem nome, e-mail, telefone, links e sem
nomes de empresas: as experiências vão como ids). Nome e contato entram apenas na montagem final do PDF.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, Field

from ..llm.rodizio import Rodizio
from ..match import taxonomia
from ..match.requisitos import RequisitosVaga
from ..match.seguranca import AVISO_PROMPT, DELIM_FIM, DELIM_INI, delimitar
from .mestre import Mestre, Pessoais

MAX_BULLETS = {"profissional": 5, "bolsa": 3, "monitoria": 2}
MAX_BULLET_CHARS = 420
MAX_RESUMO_CHARS = 500


class ErroPrivacidade(RuntimeError):
    """Um dado pessoal apareceria em um prompt enviado à LLM."""


# ----------------------------- saída final -----------------------------
class BulletFinal(BaseModel):
    origem: str
    texto: str
    corrigido: bool = False


class ExperienciaFinal(BaseModel):
    id: str
    empresa: str
    cargo: str
    local: str = ""
    periodo: str
    bullets: list[BulletFinal]


class GrupoSkills(BaseModel):
    categoria: str
    itens: list[str]


class ConteudoCV(BaseModel):
    idioma: str
    titulo: str
    resumo: str
    experiencias: list[ExperienciaFinal]
    skills: list[GrupoSkills]
    formacao: list[dict] = Field(default_factory=list)
    certificacoes: list[str] = Field(default_factory=list)
    idiomas: list[str] = Field(default_factory=list)
    correcoes: list[str] = Field(default_factory=list)


# ----------------------------- entrada para a LLM -----------------------------
SISTEMA = (
    "Você personaliza currículos selecionando, reordenando e reescrevendo bullets de um currículo mestre para "
    "uma vaga. REGRA INEGOCIÁVEL: nunca invente. Use somente fatos, tecnologias e números presentes nos "
    "bullets de origem; não adicione tecnologias, empresas, cargos, resultados ou números novos. Responda "
    "SOMENTE com um objeto JSON válido. " + AVISO_PROMPT
)

_FORMATO = """Responda com este JSON:
{
  "resumo": "2 frases no máximo, destacando o que a vaga pede e o candidato tem. NÃO cite números nem anos de experiência.",
  "experiencias": [
    {"id": "<id da experiência>", "bullets": [{"origem": "<id do bullet de origem>", "texto": "bullet reescrito"}]}
  ],
  "skills": ["skills do candidato mais relevantes para a vaga, na ordem de relevância, usando só nomes da lista de skills"]
}
Regras:
- Cada bullet reescrito deve derivar de UM bullet de origem (campo "origem") e pode apenas reordenar/condensar/ênfase
  nas partes relevantes à vaga. Mantenha números exatamente como na origem.
- Máximo de bullets por experiência: {limites}. Ordene do mais ao menos relevante para a vaga.
- Escreva todo o texto em {idioma_nome}."""


def montar_mensagens(mestre: Mestre, req: RequisitosVaga, titulo: str, descricao: str, idioma: str) -> list[dict]:
    limites = ", ".join(f"{k}: {v}" for k, v in MAX_BULLETS.items())
    candidato = {
        "alvo": mestre.cargo_alvo.em(idioma),
        "experiencias": [
            {
                "id": e.id,
                "tipo": e.tipo,
                "cargo": e.cargo.em("pt"),
                "tecnologias": e.tecnologias,
                "bullets": [{"id": b.id, "texto": b.texto} for b in e.bullets],
            }
            for e in mestre.experiencias
        ],
        "skills": [s.nome + (" (acadêmico)" if s.academico else "") for s in mestre.skills],
    }
    vaga = {"titulo": titulo, "requisitos_extraidos": req.model_dump(), "descricao": descricao[:7000]}
    usuario = (
        _FORMATO.replace("{limites}", limites).replace("{idioma_nome}", "inglês" if idioma == "en" else "português do Brasil")
        + "\n\nCURRÍCULO MESTRE (JSON):\n"
        + json.dumps(candidato, ensure_ascii=False)
        + "\n\nVAGA:\n"
        + delimitar(json.dumps(vaga, ensure_ascii=False))
    )
    return [{"role": "system", "content": SISTEMA}, {"role": "user", "content": usuario}]


def verificar_privacidade(mensagens: list[dict], pessoais: Pessoais, mestre: Mestre) -> None:
    """Falha se qualquer dado pessoal ou nome de empresa do candidato estiver no prompt (seção 6 > Privacidade).

    O trecho da vaga (texto de terceiros, entre os delimitadores) fica de fora da checagem: o que importa é
    que NOSSOS dados não saiam, e a vaga pode citar, por exemplo, a cidade de Santos.
    """
    texto = "\n".join(m["content"] for m in mensagens)
    texto = re.sub(re.escape(DELIM_INI) + r".*?" + re.escape(DELIM_FIM), " ", texto, flags=re.S).lower()
    proibidos = pessoais.valores_sensiveis() + [e.empresa for e in mestre.experiencias]
    for valor in proibidos:
        if valor and re.search(rf"(?<!\w){re.escape(valor.lower())}(?!\w)", texto):
            raise ErroPrivacidade(f"Dado pessoal/identificável no prompt: {valor[:20]}…")


# ----------------------------- validação anti-invenção -----------------------------
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numeros(texto: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUM.findall(texto)}


def _termos_ok(texto: str, permitidos: set[str]) -> list[str]:
    """Tecnologias citadas no texto que não estão no conjunto permitido (já com implicações)."""
    citados = set(taxonomia.encontrar(texto))
    return sorted(c for c in citados if c not in permitidos)


def _periodo(inicio: str, fim: str | None, idioma: str) -> str:
    meses_pt = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    meses_en = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    meses = meses_en if idioma == "en" else meses_pt

    def fmt(v: str) -> str:
        a, m = v.split("-")
        return f"{meses[int(m) - 1]}/{a}" if idioma != "en" else f"{meses[int(m) - 1]} {a}"

    atual = "Present" if idioma == "en" else "atual"
    return f"{fmt(inicio)} – {fmt(fim) if fim else atual}"


def _resumo_padrao(mestre: Mestre, idioma: str, hoje: date | None) -> str:
    anos = mestre.anos_experiencia_texto(idioma, hoje)
    cargo = mestre.cargo_alvo.em(idioma).split("|")[0].strip()
    if idioma == "en":
        return f"{cargo} with {anos} of experience."
    return f"{cargo} com {anos} de experiência."


def validar_saida(saida: dict, mestre: Mestre, idioma: str, hoje: date | None = None) -> ConteudoCV:
    """Compara a saída da LLM com o mestre; corrige ou descarta o que não existir lá."""
    correcoes: list[str] = []
    skills_mestre = mestre.canonicos_skills()

    # --- experiências e bullets ---
    por_id = {str(e.get("id")): e for e in (saida.get("experiencias") or []) if isinstance(e, dict)}
    exps: list[ExperienciaFinal] = []
    for exp in mestre.experiencias:  # ordem do mestre (cronológica inversa)
        proposta = por_id.get(exp.id)
        if proposta is None and exp.tipo not in ("profissional", "bolsa"):
            continue  # monitoria só entra se a LLM a escolheu
        permitidos = taxonomia.com_implicacoes(mestre.canonicos_experiencia(exp))
        limite = MAX_BULLETS.get(exp.tipo, 3)
        finais: list[BulletFinal] = []
        usados: set[str] = set()
        for b in (proposta or {}).get("bullets", []) if proposta else []:
            if not isinstance(b, dict):
                continue
            origem = mestre.bullet(exp.id, str(b.get("origem")))
            if origem is None:
                correcoes.append(f"{exp.id}: bullet com origem inexistente descartado ({b.get('origem')!r})")
                continue
            if origem.id in usados:
                continue
            texto = re.sub(r"\s+", " ", str(b.get("texto") or "")).strip()
            invalidos = _termos_ok(texto, permitidos)
            novos_numeros = _numeros(texto) - _numeros(origem.texto)
            if not texto or len(texto) > MAX_BULLET_CHARS or invalidos or novos_numeros:
                motivo = (f"tecnologias fora do mestre: {', '.join(invalidos)}" if invalidos else
                          f"números inventados: {', '.join(sorted(novos_numeros))}" if novos_numeros else
                          "texto vazio ou longo demais")
                correcoes.append(f"{origem.id}: reescrita rejeitada ({motivo}); mantido o texto original")
                finais.append(BulletFinal(origem=origem.id, texto=" ".join(origem.texto.split()), corrigido=True))
            else:
                finais.append(BulletFinal(origem=origem.id, texto=texto))
            usados.add(origem.id)
            if len(finais) >= limite:
                break
        if not finais:  # LLM não escolheu nada utilizável: usa os primeiros bullets do mestre
            for b in exp.bullets[: min(limite, 3)]:
                finais.append(BulletFinal(origem=b.id, texto=" ".join(b.texto.split()), corrigido=proposta is not None))
            if proposta is not None:
                correcoes.append(f"{exp.id}: sem bullets válidos da LLM; usados os do mestre")
        exps.append(ExperienciaFinal(
            id=exp.id, empresa=exp.empresa, cargo=exp.cargo.em(idioma), local=exp.local,
            periodo=_periodo(exp.inicio, exp.fim, idioma), bullets=finais,
        ))

    # --- skills ---
    escolhidas: list[str] = []
    por_nome = {taxonomia.norm(s.nome): s for s in mestre.skills}
    for nome in saida.get("skills") or []:
        s = por_nome.get(taxonomia.norm(re.sub(r"\s*\(acad[eê]mico\)", "", str(nome), flags=re.I)))
        if s is None:
            alvo = set(taxonomia.canonizar(str(nome)))
            s = next((x for x in mestre.skills if alvo & x.canonicos), None)
            if s is None or not alvo <= skills_mestre:
                correcoes.append(f"skill inexistente no mestre removida: {nome!r}")
                continue
        if s.nome not in escolhidas:
            escolhidas.append(s.nome)
    if len(escolhidas) < 6:  # completa com as de maior nível, para não ficar uma seção rala
        for s in sorted(mestre.skills, key=lambda x: (-x.nivel, x.academico)):
            if s.nome not in escolhidas and not s.academico and len(escolhidas) < 12:
                escolhidas.append(s.nome)
    grupos: dict[str, list[str]] = {}
    for nome in escolhidas:
        s = por_nome[taxonomia.norm(nome)]
        rotulo = f"{s.nome} (acadêmico)" if s.academico and idioma != "en" else (
            f"{s.nome} (academic)" if s.academico else s.nome)
        grupos.setdefault(s.categoria or "Outras", []).append(rotulo)
    skills = [GrupoSkills(categoria=c, itens=i) for c, i in grupos.items()]

    # --- resumo: frase de anos feita por nós; trecho da LLM sem números e só com tecnologias do mestre ---
    resumo = _resumo_padrao(mestre, idioma, hoje)
    extra = re.sub(r"\s+", " ", str(saida.get("resumo") or "")).strip()
    if extra:
        fora = _termos_ok(extra, taxonomia.com_implicacoes(skills_mestre))
        if _NUM.search(extra) or fora or len(extra) > MAX_RESUMO_CHARS:
            correcoes.append("resumo da LLM rejeitado (números, tecnologias fora do mestre ou longo demais)")
        else:
            resumo = f"{resumo} {extra}"

    return ConteudoCV(
        idioma=idioma,
        titulo=mestre.cargo_alvo.em(idioma),
        resumo=resumo,
        experiencias=exps,
        skills=skills,
        formacao=[
            {
                "instituicao": f.instituicao,
                "curso": f.curso.em(idioma),
                "local": f.local,
                "periodo": _periodo(f.inicio, f.fim, idioma),
                "detalhes": f.detalhes.em(idioma) if f.detalhes else "",
            }
            for f in mestre.formacao
        ],
        certificacoes=[c.em(idioma) for c in mestre.certificacoes],
        idiomas=[i.em(idioma) for i in mestre.idiomas],
        correcoes=correcoes,
    )


# ----------------------------- orquestração -----------------------------
@dataclass
class ResultadoPersonalizacao:
    conteudo: ConteudoCV
    provedor: str
    modelo: str
    avisos: list[str] = field(default_factory=list)


def _json_saida(texto: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto.strip(), flags=re.I)
    try:
        dados = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ValueError("resposta sem JSON")
        dados = json.loads(m.group(0))
    if not isinstance(dados, dict) or not isinstance(dados.get("experiencias"), list):
        raise ValueError("JSON sem a lista 'experiencias'")
    return dados


def personalizar(rodizio: Rodizio, mestre: Mestre, pessoais: Pessoais, req: RequisitosVaga,
                 titulo: str, descricao: str, idioma: str, hoje: date | None = None) -> ResultadoPersonalizacao:
    mensagens = montar_mensagens(mestre, req, titulo, descricao, idioma)
    verificar_privacidade(mensagens, pessoais, mestre)
    resp, saida = rodizio.executar("reescrita_cv", mensagens, json_mode=True, validador=_json_saida,
                                   temperatura=0.3, max_tokens=2500)
    conteudo = validar_saida(saida, mestre, idioma, hoje)  # type: ignore[arg-type]
    return ResultadoPersonalizacao(conteudo=conteudo, provedor=resp.provedor, modelo=resp.modelo)
