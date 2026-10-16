"""Edição do currículo mestre pelo painel: ativar/desativar itens, editar e adicionar.

Trabalha sobre o dicionário do YAML (preserva campos desconhecidos), valida o resultado com o modelo `Mestre`,
guarda um backup datado e grava de forma atômica. Cada ação recebe os campos do formulário e devolve uma
mensagem curta; erros de entrada viram `ValueError` com texto para o usuário.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

import yaml
from pydantic import ValidationError

from .mestre import Mestre

TRAVA = threading.Lock()
MAX_BACKUPS = 30
MAX_TEXTO = 700
MES = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIPOS_EXPERIENCIA = ("profissional", "bolsa", "monitoria", "projeto")


# ------------------------------------------------------------------ arquivo
def ler(caminho: str | Path) -> dict:
    p = Path(caminho)
    if not p.exists():
        raise FileNotFoundError(f"{p} não existe. Copie {p.with_name('curriculo_mestre.example.yaml')} para {p.name}.")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _backup(p: Path) -> None:
    if not p.exists():
        return
    pasta = p.parent / "backups"
    pasta.mkdir(exist_ok=True)
    (pasta / f"{p.stem}-{datetime.now():%Y%m%d-%H%M%S-%f}.yaml").write_bytes(p.read_bytes())
    for antigo in sorted(pasta.glob(f"{p.stem}-*.yaml"))[:-MAX_BACKUPS]:
        antigo.unlink(missing_ok=True)


def salvar(caminho: str | Path, dados: dict) -> None:
    """Valida, faz backup e grava (arquivo temporário + rename). Não grava nada se a validação falhar."""
    try:
        Mestre.model_validate(dados)
    except ValidationError as e:
        campos = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:3])
        raise ValueError(f"Currículo inválido, nada foi salvo ({campos}).") from e
    p = Path(caminho)
    _backup(p)
    texto = yaml.safe_dump(dados, allow_unicode=True, sort_keys=False, width=110, default_flow_style=False)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".mestre-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(texto)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def aplicar(caminho: str | Path, acao: str, campos: Mapping[str, str]) -> str:
    """Executa uma ação do painel sobre o arquivo (ler → alterar → validar → gravar)."""
    if acao not in ACOES:
        raise ValueError("Ação desconhecida.")
    with TRAVA:
        dados = ler(caminho)
        mensagem = ACOES[acao](dados, campos)
        salvar(caminho, dados)
    return mensagem


# ------------------------------------------------------------------ utilitários
def chave(item) -> str:
    """Impressão digital curta de um item de lista: detecta página desatualizada antes de alterar o item errado."""
    return hashlib.sha1(json.dumps(item, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:8]


def _slug(texto: str) -> str:
    s = unicodedata.normalize("NFD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:30] or "item"


def _texto(campos: Mapping[str, str], nome: str, obrigatorio: bool = True, maximo: int = MAX_TEXTO) -> str:
    v = re.sub(r"[ \t]+", " ", str(campos.get(nome, "")).replace("\r", "")).strip()
    if obrigatorio and not v:
        raise ValueError(f"Preencha o campo «{nome}».")
    if len(v) > maximo:
        raise ValueError(f"O campo «{nome}» passa de {maximo} caracteres.")
    return v


def _lista(valor: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n;]", valor) if x.strip()][:40]


def _mes(valor: str, nome: str, obrigatorio: bool = True) -> str | None:
    v = valor.strip()
    if not v and not obrigatorio:
        return None
    if not MES.match(v):
        raise ValueError(f"«{nome}» deve estar no formato AAAA-MM (ex.: 2025-03).")
    return v


def _bool(campos: Mapping[str, str], nome: str) -> bool:
    return str(campos.get(nome, "")).lower() in ("on", "true", "1", "sim")


def _nivel(campos: Mapping[str, str]) -> int:
    try:
        n = int(str(campos.get("nivel", "3")))
    except ValueError:
        raise ValueError("Nível deve ser um número de 1 a 5.") from None
    if not 1 <= n <= 5:
        raise ValueError("Nível deve ser um número de 1 a 5.")
    return n


def _exp(dados: dict, exp_id: str) -> dict:
    e = next((x for x in dados.get("experiencias", []) if x.get("id") == exp_id), None)
    if e is None:
        raise ValueError("Experiência não encontrada (a página pode estar desatualizada).")
    return e


def _skill(dados: dict, nome: str) -> dict:
    s = next((x for x in dados.get("skills", []) if str(x.get("nome", "")).lower() == nome.lower()), None)
    if s is None:
        raise ValueError(f"Skill «{nome}» não encontrada (a página pode estar desatualizada).")
    return s


def _item_lista(dados: dict, lista: str, campos: Mapping[str, str]) -> dict:
    """Item de uma lista sem id (formação, certificações, idiomas), conferindo índice + impressão digital."""
    itens = dados.get(lista, [])
    try:
        i = int(campos.get("i", "-1"))
        item = itens[i]
    except (ValueError, IndexError):
        raise ValueError("Item não encontrado (a página pode estar desatualizada).") from None
    if chave(item) != campos.get("k"):
        raise ValueError("O item mudou desde que a página foi carregada. Recarregue e tente de novo.")
    return item


LISTAS = {"certificacoes": "certificação", "idiomas": "idioma", "formacao": "formação"}


# ------------------------------------------------------------------ ações
def _geral_salvar(dados: dict, f: Mapping[str, str]) -> str:
    dados["cargo_alvo"] = {"pt": _texto(f, "cargo_pt", maximo=150), "en": _texto(f, "cargo_en", False, 150)}
    dados["inicio_carreira"] = _mes(f.get("inicio_carreira", ""), "Início da carreira")
    return "Dados gerais salvos."


def _alternar(dados: dict, f: Mapping[str, str]) -> str:
    tipo = f.get("tipo", "")
    if tipo == "experiencia":
        alvo = _exp(dados, f.get("ref", ""))
    elif tipo == "bullet":
        bullet_id = f.get("ref2", "")
        alvo = next((b for b in _exp(dados, f.get("ref", "")).get("bullets", []) if b.get("id") == bullet_id), None)
        if alvo is None:
            raise ValueError("Bullet não encontrado (a página pode estar desatualizada).")
    elif tipo == "skill":
        alvo = _skill(dados, f.get("ref", ""))
    elif tipo in LISTAS:
        alvo = _item_lista(dados, tipo, f)
    else:
        raise ValueError("Tipo desconhecido.")
    alvo["ativo"] = not alvo.get("ativo", True)
    return "Ativado." if alvo["ativo"] else "Desativado (continua salvo; reative quando quiser)."


def _remover(dados: dict, f: Mapping[str, str]) -> str:
    tipo = f.get("tipo", "")
    if tipo == "experiencia":
        dados["experiencias"].remove(_exp(dados, f.get("ref", "")))
    elif tipo == "bullet":
        e = _exp(dados, f.get("ref", ""))
        b = next((x for x in e.get("bullets", []) if x.get("id") == f.get("ref2")), None)
        if b is None:
            raise ValueError("Bullet não encontrado.")
        e["bullets"].remove(b)
    elif tipo == "skill":
        dados["skills"].remove(_skill(dados, f.get("ref", "")))
    elif tipo in LISTAS:
        dados[tipo].remove(_item_lista(dados, tipo, f))
    else:
        raise ValueError("Tipo desconhecido.")
    return "Removido. (Há backup datado em data/backups/; para só esconder do CV, use Desativar.)"


def _skill_adicionar(dados: dict, f: Mapping[str, str]) -> str:
    nome = _texto(f, "nome", maximo=60)
    if any(str(s.get("nome", "")).lower() == nome.lower() for s in dados.get("skills", [])):
        raise ValueError(f"A skill «{nome}» já existe.")
    categoria = _texto(f, "categoria_nova", False, 40) or _texto(f, "categoria", maximo=40)
    skill = {"nome": nome, "categoria": categoria, "nivel": _nivel(f)}
    if _bool(f, "academico"):
        skill["academico"] = True
    sinonimos = _lista(str(f.get("sinonimos", "")))
    if sinonimos:
        skill["sinonimos"] = sinonimos
    dados.setdefault("skills", []).append(skill)
    return f"Skill «{nome}» adicionada em {categoria}."


def _skill_editar(dados: dict, f: Mapping[str, str]) -> str:
    s = _skill(dados, f.get("ref", ""))
    s["nivel"] = _nivel(f)
    s["categoria"] = _texto(f, "categoria_nova", False, 40) or _texto(f, "categoria", maximo=40)
    s["academico"] = _bool(f, "academico")
    if not s["academico"]:
        s.pop("academico")
    sinonimos = _lista(str(f.get("sinonimos", "")))
    if sinonimos:
        s["sinonimos"] = sinonimos
    else:
        s.pop("sinonimos", None)
    return f"Skill «{s['nome']}» atualizada."


def _bullet_adicionar(dados: dict, f: Mapping[str, str]) -> str:
    e = _exp(dados, f.get("ref", ""))
    texto = _texto(f, "texto")
    usados = {b.get("id") for b in e.get("bullets", [])}
    base, n = f"{e['id']}.{_slug(texto.split('.')[0][:40])}", 1
    novo_id = base
    while novo_id in usados:
        n += 1
        novo_id = f"{base}_{n}"
    bullet = {"id": novo_id, "texto": texto}
    tags = _lista(str(f.get("tags", "")))
    if tags:
        bullet["tags"] = tags
    e.setdefault("bullets", []).append(bullet)
    return "Bullet adicionado."


def _bullet_editar(dados: dict, f: Mapping[str, str]) -> str:
    e = _exp(dados, f.get("ref", ""))
    b = next((x for x in e.get("bullets", []) if x.get("id") == f.get("ref2")), None)
    if b is None:
        raise ValueError("Bullet não encontrado (a página pode estar desatualizada).")
    b["texto"] = _texto(f, "texto")
    tags = _lista(str(f.get("tags", "")))
    if tags:
        b["tags"] = tags
    else:
        b.pop("tags", None)
    return "Bullet atualizado."


def _campos_experiencia(f: Mapping[str, str]) -> dict:
    tipo = _texto(f, "tipo_exp", maximo=20)
    if tipo not in TIPOS_EXPERIENCIA:
        raise ValueError(f"Tipo deve ser um de: {', '.join(TIPOS_EXPERIENCIA)}.")
    return {
        "empresa": _texto(f, "empresa", maximo=120),
        "cargo": {"pt": _texto(f, "cargo_pt", maximo=120), "en": _texto(f, "cargo_en", False, 120)},
        "local": _texto(f, "local", False, 80),
        "inicio": _mes(f.get("inicio", ""), "Início"),
        "fim": _mes(f.get("fim", ""), "Fim", obrigatorio=False),
        "tipo": tipo,
        "tecnologias": _lista(str(f.get("tecnologias", ""))),
    }


def _experiencia_adicionar(dados: dict, f: Mapping[str, str]) -> str:
    campos = _campos_experiencia(f)
    usados = {e.get("id") for e in dados.get("experiencias", [])}
    base, n = _slug(campos["empresa"]), 1
    novo_id = base
    while novo_id in usados:
        n += 1
        novo_id = f"{base}_{n}"
    campos["conta_experiencia"] = campos["tipo"] in ("profissional", "bolsa")
    dados.setdefault("experiencias", []).append({"id": novo_id, **campos, "bullets": []})
    return f"Experiência «{campos['empresa']}» adicionada. Adicione bullets nela (sem bullet ativo ela não entra no CV)."


def _experiencia_editar(dados: dict, f: Mapping[str, str]) -> str:
    e = _exp(dados, f.get("ref", ""))
    e.update(_campos_experiencia(f))
    return f"Experiência «{e['empresa']}» atualizada."


def _campos_formacao(f: Mapping[str, str]) -> dict:
    item = {
        "instituicao": _texto(f, "instituicao", maximo=120),
        "curso": {"pt": _texto(f, "curso_pt", maximo=150), "en": _texto(f, "curso_en", False, 150)},
        "local": _texto(f, "local", False, 80),
        "inicio": _mes(f.get("inicio", ""), "Início"),
        "fim": _mes(f.get("fim", ""), "Fim"),
    }
    pt, en = _texto(f, "detalhes_pt", False), _texto(f, "detalhes_en", False)
    if pt:
        item["detalhes"] = {"pt": pt, "en": en}
    return item


def _formacao_adicionar(dados: dict, f: Mapping[str, str]) -> str:
    dados.setdefault("formacao", []).append(_campos_formacao(f))
    return "Formação adicionada."


def _formacao_editar(dados: dict, f: Mapping[str, str]) -> str:
    item = _item_lista(dados, "formacao", f)
    ativo = item.get("ativo", True)
    item.clear()
    item.update(_campos_formacao(f))
    if not ativo:
        item["ativo"] = False
    return "Formação atualizada."


def _item_adicionar(dados: dict, f: Mapping[str, str]) -> str:
    lista = f.get("lista", "")
    if lista not in ("certificacoes", "idiomas"):
        raise ValueError("Lista desconhecida.")
    dados.setdefault(lista, []).append({"pt": _texto(f, "pt", maximo=200), "en": _texto(f, "en", False, 200)})
    return f"Adicionado em {LISTAS[lista]}s."


def _item_editar(dados: dict, f: Mapping[str, str]) -> str:
    lista = f.get("lista", "")
    if lista not in ("certificacoes", "idiomas"):
        raise ValueError("Lista desconhecida.")
    item = _item_lista(dados, lista, f)
    item["pt"], item["en"] = _texto(f, "pt", maximo=200), _texto(f, "en", False, 200)
    return "Atualizado."


ACOES: dict[str, Callable[[dict, Mapping[str, str]], str]] = {
    "geral_salvar": _geral_salvar,
    "alternar": _alternar,
    "remover": _remover,
    "skill_adicionar": _skill_adicionar,
    "skill_editar": _skill_editar,
    "bullet_adicionar": _bullet_adicionar,
    "bullet_editar": _bullet_editar,
    "experiencia_adicionar": _experiencia_adicionar,
    "experiencia_editar": _experiencia_editar,
    "formacao_adicionar": _formacao_adicionar,
    "formacao_editar": _formacao_editar,
    "item_adicionar": _item_adicionar,
    "item_editar": _item_editar,
}
