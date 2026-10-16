"""Telas de execução (barra de progresso + log completo) e histórico de chamadas de LLM."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import case, func, select

from ..db.models import Execucao, ExecucaoLog, LlmChamada, Vaga, agora_utc
from .auth import exigir_login
from .rotas import hora_local, pagina, sv_de, templates

router = APIRouter(dependencies=[Depends(exigir_login)])

LINHAS_AO_VIVO = 400
POR_PAGINA = 100
NIVEIS = ("info", "aviso", "erro")


def _dados_execucao(request: Request, exec_id: int, nivel: str = "", etapa: str = "") -> dict:
    sv = sv_de(request)
    with sv.fabrica() as s:
        ex = s.get(Execucao, exec_id)
        if ex is None:
            raise HTTPException(404, "Execução não encontrada.")
        q = select(ExecucaoLog).where(ExecucaoLog.execucao_id == exec_id)
        if nivel in NIVEIS:
            q = q.where(ExecucaoLog.nivel == nivel)
        if etapa:
            q = q.where(ExecucaoLog.etapa == etapa)
        total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
        logs = s.scalars(q.order_by(ExecucaoLog.id.desc()).limit(LINHAS_AO_VIVO)).all()
        contagem = dict(s.execute(select(ExecucaoLog.nivel, func.count()).where(ExecucaoLog.execucao_id == exec_id)
                                  .group_by(ExecucaoLog.nivel)).all())
        etapas = [e for (e,) in s.execute(select(ExecucaoLog.etapa).where(ExecucaoLog.execucao_id == exec_id)
                                          .distinct().order_by(ExecucaoLog.etapa)).all()]
        chamadas = s.execute(select(func.count(), func.coalesce(func.sum(LlmChamada.sucesso), 0))
                             .where(LlmChamada.execucao_id == exec_id)).one()
        # expunge para usar fora da sessão
        s.expunge_all()
    return {"ex": ex, "logs": logs, "total": total, "contagem": contagem, "etapas": etapas,
            "nivel": nivel, "etapa_filtro": etapa, "chamadas": chamadas[0], "chamadas_ok": int(chamadas[1] or 0),
            "mostrando": len(logs), "limite_linhas": LINHAS_AO_VIVO}


@router.get("/execucoes", response_class=HTMLResponse)
def lista_execucoes(request: Request):
    sv = sv_de(request)
    with sv.fabrica() as s:
        execs = s.scalars(select(Execucao).order_by(Execucao.id.desc()).limit(60)).all()
        return pagina(request, "execucoes.html", execs=execs)


@router.get("/execucoes/{exec_id}", response_class=HTMLResponse)
def ver_execucao(request: Request, exec_id: int, nivel: str = "", etapa: str = ""):
    return pagina(request, "execucao.html", **_dados_execucao(request, exec_id, nivel, etapa))


@router.get("/execucoes/{exec_id}/progresso", response_class=HTMLResponse)
def progresso_execucao(request: Request, exec_id: int, nivel: str = "", etapa: str = ""):
    """Fragmento HTMX: se a execução ainda roda, ele se atualiza sozinho a cada 2 s."""
    dados = _dados_execucao(request, exec_id, nivel, etapa)
    return templates.TemplateResponse(request, "_progresso.html", {"request": request, **dados})


@router.get("/execucoes/{exec_id}/log.txt", response_class=PlainTextResponse)
def baixar_log(request: Request, exec_id: int):
    """Log completo (sem o limite de linhas da tela), em texto puro."""
    sv = sv_de(request)
    with sv.fabrica() as s:
        ex = s.get(Execucao, exec_id)
        if ex is None:
            raise HTTPException(404, "Execução não encontrada.")
        linhas = s.scalars(select(ExecucaoLog).where(ExecucaoLog.execucao_id == exec_id)
                           .order_by(ExecucaoLog.id)).all()
        cab = (f"Execução #{ex.id} ({ex.origem}) — {ex.status} — início {hora_local(ex.inicio, '%d/%m/%Y %H:%M:%S')}"
               f" — fim {hora_local(ex.fim, '%d/%m/%Y %H:%M:%S')}\n")
        corpo = "".join(f"{hora_local(l.criado_em, '%H:%M:%S')} [{l.nivel.upper():5}] {l.etapa:<7} {l.mensagem}\n"
                        for l in linhas)
    return PlainTextResponse(cab + "=" * 80 + "\n" + corpo, headers={
        "Content-Disposition": f'attachment; filename="execucao-{exec_id}.log"'})


# ------------------------------------------------------------------ histórico de LLM
def resumo_provedores(s) -> list[dict]:
    """Uso e falhas por provedor: 24 h e total, último sucesso/erro."""
    corte = agora_utc() - timedelta(hours=24)
    linhas = s.execute(
        select(
            LlmChamada.provedor,
            func.count(),
            func.coalesce(func.sum(case((LlmChamada.sucesso.is_(True), 1), else_=0)), 0),
            func.coalesce(func.sum(case((LlmChamada.criado_em >= corte, 1), else_=0)), 0),
            func.coalesce(func.sum(case(((LlmChamada.criado_em >= corte) & LlmChamada.sucesso.is_(False), 1), else_=0)), 0),
            func.coalesce(func.sum(LlmChamada.tokens_in), 0),
            func.coalesce(func.sum(LlmChamada.tokens_out), 0),
            func.max(case((LlmChamada.sucesso.is_(True), LlmChamada.criado_em))),
            func.max(case((LlmChamada.sucesso.is_(False), LlmChamada.criado_em))),
            func.avg(case((LlmChamada.sucesso.is_(True), LlmChamada.duracao_ms))),
        ).group_by(LlmChamada.provedor).order_by(LlmChamada.provedor)).all()
    resultado = []
    for prov, total, ok, n24, falhas24, tin, tout, ult_ok, ult_erro, media in linhas:
        tipos = dict(s.execute(select(LlmChamada.tipo_erro, func.count()).where(
            LlmChamada.provedor == prov, LlmChamada.sucesso.is_(False)).group_by(LlmChamada.tipo_erro)).all())
        ultimo_erro = s.scalar(select(LlmChamada.erro).where(
            LlmChamada.provedor == prov, LlmChamada.sucesso.is_(False)).order_by(LlmChamada.id.desc()).limit(1))
        modelos = [m for (m,) in s.execute(select(LlmChamada.modelo).where(LlmChamada.provedor == prov)
                                           .group_by(LlmChamada.modelo).order_by(func.max(LlmChamada.id).desc())).all()]
        resultado.append({"provedor": prov, "total": total, "ok": int(ok), "falhas": total - int(ok),
                          "n24": int(n24), "falhas24": int(falhas24), "tokens_in": int(tin), "tokens_out": int(tout),
                          "ultimo_ok": ult_ok, "ultimo_erro_em": ult_erro, "media_ms": int(media or 0),
                          "tipos": tipos, "ultimo_erro": (ultimo_erro or "")[:300], "modelos": modelos})
    return resultado


def _inteiro_ou_none(valor: str) -> int | None:
    """Formulário GET envia `execucao=` vazio quando o campo não é preenchido: vazio ou inválido = sem filtro."""
    try:
        return int(valor) if valor.strip() else None
    except ValueError:
        return None


def _consulta_chamadas(provedor: str, resultado: str, tipo: str, execucao: int | None):
    q = select(LlmChamada)
    if provedor:
        q = q.where(LlmChamada.provedor == provedor)
    if resultado == "ok":
        q = q.where(LlmChamada.sucesso.is_(True))
    elif resultado == "falha":
        q = q.where(LlmChamada.sucesso.is_(False))
    if tipo:
        q = q.where(LlmChamada.tipo_erro == tipo)
    if execucao:
        q = q.where(LlmChamada.execucao_id == execucao)
    return q


@router.get("/llm-log", response_class=HTMLResponse)
def llm_log(request: Request, provedor: str = "", resultado: str = "", tipo: str = "",
            execucao: str = "", pagina_n: int = 1):
    """Log completo de todas as chamadas de LLM: quem foi usado, quem falhou, por quê, quanto demorou."""
    sv = sv_de(request)
    pagina_n = max(1, pagina_n)
    execucao = _inteiro_ou_none(execucao)
    with sv.fabrica() as s:
        base = _consulta_chamadas(provedor, resultado, tipo, execucao)
        total = s.scalar(select(func.count()).select_from(base.subquery())) or 0
        chamadas = s.scalars(base.order_by(LlmChamada.id.desc()).offset((pagina_n - 1) * POR_PAGINA)
                             .limit(POR_PAGINA)).all()
        vagas = {}
        ids = {c.vaga_id for c in chamadas if c.vaga_id}
        if ids:
            vagas = {v.id: v.titulo for v in s.scalars(select(Vaga).where(Vaga.id.in_(ids))).all()}
        provedores = [p for (p,) in s.execute(select(LlmChamada.provedor).distinct().order_by(LlmChamada.provedor)).all()]
        tipos = [t for (t,) in s.execute(select(LlmChamada.tipo_erro).where(LlmChamada.tipo_erro != "")
                                         .distinct().order_by(LlmChamada.tipo_erro)).all()]
        return pagina(request, "llm_log.html", chamadas=chamadas, vagas=vagas, total=total, pagina_n=pagina_n,
                      paginas=max(1, -(-total // POR_PAGINA)), provedor=provedor, resultado=resultado, tipo=tipo,
                      execucao=execucao, provedores=provedores, tipos=tipos)


@router.get("/llm-log.csv", response_class=PlainTextResponse)
def llm_log_csv(request: Request, provedor: str = "", resultado: str = "", tipo: str = "",
                execucao: str = ""):
    sv = sv_de(request)
    execucao = _inteiro_ou_none(execucao)
    saida = io.StringIO()
    w = csv.writer(saida)
    w.writerow(["id", "quando_utc", "provedor", "modelo", "tarefa", "sucesso", "tipo_erro", "http_status",
                "duracao_ms", "tokens_in", "tokens_out", "tentativa", "execucao_id", "vaga_id", "erro"])
    with sv.fabrica() as s:
        for c in s.scalars(_consulta_chamadas(provedor, resultado, tipo, execucao).order_by(LlmChamada.id.desc())):
            w.writerow([c.id, c.criado_em.isoformat(), c.provedor, c.modelo, c.tarefa, int(c.sucesso), c.tipo_erro,
                        c.http_status or "", c.duracao_ms, c.tokens_in, c.tokens_out, c.tentativa,
                        c.execucao_id or "", c.vaga_id or "", c.erro])
    return PlainTextResponse(saida.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="llm-log.csv"'})
