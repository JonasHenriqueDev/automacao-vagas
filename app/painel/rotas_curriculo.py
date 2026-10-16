"""Aba Currículo: ver o mestre, ativar/desativar itens, editar e adicionar."""
from __future__ import annotations

import re
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from ..curriculo import edicao
from ..curriculo.mestre import carregar_mestre
from ..curriculo.personalizar import validar_saida
from ..curriculo.render import ErroRender, renderizar
from .auth import checar_csrf, exigir_login
from .rotas import avisar, pagina, sv_de, templates, voltar

router = APIRouter(dependencies=[Depends(exigir_login)])
templates.env.globals["chave_item"] = edicao.chave


def _contagem(itens: list[dict]) -> tuple[int, int]:
    return sum(1 for i in itens if i.get("ativo", True)), len(itens)


@router.get("/curriculo", response_class=HTMLResponse)
def ver_curriculo(request: Request):
    sv = sv_de(request)
    caminho = sv.config.caminho("mestre")
    try:
        dados = edicao.ler(caminho)
    except FileNotFoundError as e:
        return pagina(request, "curriculo.html", dados=None, erro_arquivo=str(e))
    categorias: "OrderedDict[str, list[dict]]" = OrderedDict()
    for s in dados.get("skills", []):
        categorias.setdefault(s.get("categoria") or "Outras", []).append(s)
    backups = sorted((caminho.parent / "backups").glob(f"{caminho.stem}-*.yaml")) if (caminho.parent / "backups").exists() else []
    return pagina(
        request, "curriculo.html", dados=dados, erro_arquivo=None, categorias=categorias,
        n_exp=_contagem(dados.get("experiencias", [])), n_skills=_contagem(dados.get("skills", [])),
        n_cert=_contagem(dados.get("certificacoes", [])), n_idiomas=_contagem(dados.get("idiomas", [])),
        n_form=_contagem(dados.get("formacao", [])), n_backups=len(backups),
        tipos_exp=edicao.TIPOS_EXPERIENCIA)


@router.post("/curriculo/acao", dependencies=[Depends(checar_csrf)])
async def acao_curriculo(request: Request):
    form = await request.form()
    campos = {k: str(v) for k, v in form.items()}
    ancora = campos.get("ancora", "")
    destino = "/curriculo" + (f"#{ancora}" if re.fullmatch(r"[a-z0-9_-]{1,60}", ancora) else "")
    try:
        avisar(request, edicao.aplicar(sv_de(request).config.caminho("mestre"), campos.get("acao", ""), campos))
    except (ValueError, FileNotFoundError) as e:
        avisar(request, f"Não salvo: {e}")
    return voltar(destino)


@router.get("/curriculo/previa.pdf")
def previa(request: Request, idioma: str = "pt"):
    """CV de exemplo com os itens ATIVOS (sem LLM): mostra como o mestre atual vira PDF."""
    if idioma not in ("pt", "en"):
        raise HTTPException(400, "Idioma inválido.")
    sv = sv_de(request)
    try:
        mestre = carregar_mestre(sv.config.caminho("mestre")).somente_ativos()
        conteudo = validar_saida({}, mestre, idioma)
        destino = sv.config.caminho("saida") / f"previa_{idioma}.pdf"
        renderizar(conteudo, sv.pessoais(), sv.config.caminho("templates_cv"), destino)
    except (FileNotFoundError, ErroRender) as e:
        raise HTTPException(500, f"Não foi possível gerar a prévia: {e}") from e
    return FileResponse(destino, media_type="application/pdf", filename=destino.name,
                        content_disposition_type="inline")
