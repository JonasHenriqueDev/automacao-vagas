"""Aba Gerar CV: cola a descrição de uma vaga, pergunta sobre o que falta no mestre e gera o CV."""
from __future__ import annotations

import json
from collections import OrderedDict
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ..curriculo import edicao, lacunas
from ..curriculo.mestre import carregar_mestre
from ..db.models import Vaga
from ..match import score
from ..pipeline import (
    FONTE_MANUAL,
    analisar_vaga_manual,
    criar_vaga_manual,
    gerar_cv_manual,
    requisitos_locais,
)
from .auth import checar_csrf, exigir_login
from .rotas import avisar, pagina, sv_de, voltar

router = APIRouter(dependencies=[Depends(exigir_login)])

MIN_DESCRICAO = 80
MAX_DESCRICAO = 30000


def _url_http(url: str) -> str:
    p = urlsplit(url.strip())
    return url.strip()[:2000] if p.scheme in ("http", "https") and p.netloc else ""


def _vaga_manual(s, vaga_id: int) -> Vaga:
    v = s.get(Vaga, vaga_id)
    if v is None or v.fonte != FONTE_MANUAL:
        raise HTTPException(404, "Vaga não encontrada.")
    return v


@router.get("/gerar", response_class=HTMLResponse)
def formulario(request: Request):
    sv = sv_de(request)
    with sv.fabrica() as s:
        recentes = [{"v": v, "n_cv": len(v.curriculos)} for v in s.scalars(
            select(Vaga).where(Vaga.fonte == FONTE_MANUAL).order_by(Vaga.id.desc()).limit(10)).all()]
    return pagina(request, "gerar.html", etapa="colar", recentes=recentes, min_descricao=MIN_DESCRICAO,
                  max_descricao=MAX_DESCRICAO)


@router.post("/gerar/analisar", dependencies=[Depends(checar_csrf)])
def analisar(request: Request, titulo: str = Form(""), empresa: str = Form(""), url: str = Form(""),
             idioma: str = Form("auto"), descricao: str = Form("")):
    if len(descricao.strip()) < MIN_DESCRICAO:
        avisar(request, f"Cole a descrição completa da vaga (mínimo de {MIN_DESCRICAO} caracteres).")
        return voltar("/gerar")
    sv = sv_de(request)
    vaga_id, nova = criar_vaga_manual(sv, titulo[:300], empresa[:200], _url_http(url), descricao[:MAX_DESCRICAO], idioma)
    if nova:
        analisar_vaga_manual(sv, vaga_id)
    else:
        avisar(request, "Esta descrição já tinha sido colada; reaproveitei a análise.")
    return voltar(f"/gerar/{vaga_id}")


@router.get("/gerar/{vaga_id}", response_class=HTMLResponse)
def revisar_lacunas(request: Request, vaga_id: int):
    sv = sv_de(request)
    try:
        completo = carregar_mestre(sv.config.caminho("mestre"))
    except FileNotFoundError as e:
        return pagina(request, "gerar.html", etapa="erro", erro_arquivo=str(e))
    with sv.fabrica() as s:
        v = _vaga_manual(s, vaga_id)
        req, modo = requisitos_locais(v)
        peso_aca = float(sv.config.obter("match.peso_academico", 0.5))
        pendentes = lacunas.encontrar(req, completo, peso_aca)
        det = score.calcular(req, score.Perfil(completo.somente_ativos(), peso_aca), sv.config.obter("match.pesos"),
                             modo=modo)
        categorias = list(OrderedDict.fromkeys(sk.categoria or "Outras" for sk in completo.skills))
        if "Outras" not in categorias:
            categorias.append("Outras")
        return pagina(
            request, "gerar.html", etapa="lacunas", v=v, req=req, modo=modo, det=det.como_dict(),
            lacunas=pendentes, categorias=categorias,
            experiencias=[{"id": e.id, "rotulo": f"{e.empresa} — {e.cargo.pt} ({e.inicio} → {e.fim or 'atual'})"
                           + ("" if e.ativo else " [desativada]")} for e in completo.experiencias],
            curriculos=v.curriculos, avisos=json.loads(v.avisos_json or "[]"))


@router.post("/gerar/{vaga_id}/reanalisar", dependencies=[Depends(checar_csrf)])
def reanalisar(request: Request, vaga_id: int):
    sv = sv_de(request)
    with sv.fabrica() as s:
        _vaga_manual(s, vaga_id)
    modo = analisar_vaga_manual(sv, vaga_id, refazer=True)
    avisar(request, "Requisitos reanalisados com a LLM." if modo == "llm"
           else "A LLM continua indisponível; mantive a análise por palavras-chave.")
    return voltar(f"/gerar/{vaga_id}")


@router.post("/gerar/{vaga_id}/gerar", dependencies=[Depends(checar_csrf)])
async def gerar(request: Request, vaga_id: int):
    sv = sv_de(request)
    with sv.fabrica() as s:
        _vaga_manual(s, vaga_id)
    form = await request.form()
    campos = {k: str(v) for k, v in form.items()}
    mensagens: list[str] = []
    if any(campos.get(k) in ("sim", "reativar") for k in campos if k.startswith("resp_")):
        try:
            mensagens.append(edicao.aplicar(sv.config.caminho("mestre"), "conhecimentos_aplicar", campos))
        except (ValueError, FileNotFoundError) as e:
            avisar(request, f"Nada foi salvo: {e}")
            return voltar(f"/gerar/{vaga_id}")
    gerado, resultado = gerar_cv_manual(sv, vaga_id)
    mensagens.append(resultado)
    avisar(request, " ".join(mensagens))
    return voltar(f"/vaga/{vaga_id}" if gerado else f"/gerar/{vaga_id}")
