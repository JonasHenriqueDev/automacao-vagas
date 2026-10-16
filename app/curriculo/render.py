"""Renderização do PDF com Typst. Usa o pacote `typst` (compilador embutido, sem binário externo);
se não estiver instalado, tenta o executável `typst` do PATH."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .mestre import Pessoais
from .personalizar import ConteudoCV

ROTULOS = {
    "pt": {"tecnologias": "Tecnologias", "experiencia": "Experiência profissional", "skills": "Competências técnicas",
           "formacao": "Formação acadêmica", "certificacoes": "Certificações", "idiomas": "Idiomas"},
    "en": {"tecnologias": "Technologies", "experiencia": "Professional experience", "skills": "Technical skills",
           "formacao": "Education", "certificacoes": "Certifications", "idiomas": "Languages"},
}


class ErroRender(RuntimeError):
    pass


def _url(valor: str) -> str:
    """Garante o esquema (https://) em links sem ele."""
    valor = valor.strip()
    return valor if re.match(r"^[a-z][a-z0-9+.-]*:", valor, re.I) else f"https://{valor}"


def _contatos(p: Pessoais) -> list[dict]:
    """Itens de contato; os que têm destino viram hiperlinks clicáveis no PDF (sem cor azul)."""
    itens: list[dict] = []
    if p.email:
        itens.append({"texto": p.email, "url": f"mailto:{p.email.strip()}"})
    if p.telefone:
        digitos = re.sub(r"\D", "", p.telefone)
        itens.append({"texto": p.telefone, "url": f"tel:{digitos}" if digitos else ""})
    if p.localizacao:
        itens.append({"texto": p.localizacao, "url": ""})
    for l in p.links:
        if l.get("url"):
            itens.append({"texto": l["url"], "url": _url(l["url"])})
    return itens


def _dados_template(c: ConteudoCV, p: Pessoais) -> dict:
    contato = _contatos(p)
    return {
        "nome": p.nome,
        "titulo": c.titulo,
        "idioma": c.idioma,
        "contato": contato,
        "rotulos": ROTULOS.get(c.idioma, ROTULOS["pt"]),
        "experiencias": [e.model_dump() for e in c.experiencias],
        "skills": [g.model_dump() for g in c.skills],
        "formacao": c.formacao,
        "certificacoes": c.certificacoes,
        "idiomas": c.idiomas,
    }


def _compilar(typ: Path, pdf: Path) -> None:
    try:
        import typst  # type: ignore

        typst.compile(str(typ), output=str(pdf))
        return
    except ImportError:
        pass
    exe = shutil.which("typst")
    if not exe:
        raise ErroRender("Typst não encontrado: instale o pacote Python `typst` ou o binário no PATH.")
    r = subprocess.run([exe, "compile", str(typ), str(pdf)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise ErroRender(r.stderr[-500:])


def validar_pdf(caminho: Path) -> None:
    dados = caminho.read_bytes()
    if not dados.startswith(b"%PDF") or b"%%EOF" not in dados[-1024:] or len(dados) < 2000:
        raise ErroRender("PDF gerado é inválido ou truncado.")


def slug(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")[:40] or "vaga"


def renderizar(conteudo: ConteudoCV, pessoais: Pessoais, pasta_templates: Path, destino: Path) -> Path:
    """Gera o PDF em `destino` e o valida. Os arquivos temporários ficam fora da pasta de saída."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    modelo = pasta_templates / "cv.typ"
    if not modelo.exists():
        raise ErroRender(f"Template não encontrado: {modelo}")
    with tempfile.TemporaryDirectory(prefix="cv_") as tmp:
        tmp_dir = Path(tmp)
        shutil.copy(modelo, tmp_dir / "cv.typ")
        (tmp_dir / "dados.json").write_text(
            json.dumps(_dados_template(conteudo, pessoais), ensure_ascii=False), encoding="utf-8")
        saida = tmp_dir / "cv.pdf"
        try:
            _compilar(tmp_dir / "cv.typ", saida)
        except ErroRender:
            raise
        except Exception as e:
            raise ErroRender(f"Falha ao compilar o Typst: {e}") from e
        validar_pdf(saida)
        shutil.move(str(saida), destino)
    return destino
