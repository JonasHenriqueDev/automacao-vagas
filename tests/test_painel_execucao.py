"""Painel: idade das vagas, link original, progresso/log das execuções e histórico de LLM."""
import re
import time
from datetime import timedelta

from sqlalchemy import select

from app.db.models import Execucao, ExecucaoLog, LlmChamada, Match, Vaga, agora_utc
from app.painel.rotas import idade_texto
from tests.test_painel import cliente, csrf_de, logado, painel, token  # noqa: F401  (fixtures)


def test_idade_texto():
    agora = agora_utc()
    assert idade_texto(agora - timedelta(minutes=20)) == "há 20 min"
    assert idade_texto(agora - timedelta(hours=3, minutes=20)) == "há 3 h 20 min"
    assert idade_texto(agora - timedelta(hours=5)) == "há 5 h"
    assert idade_texto(agora - timedelta(hours=60)) == "há 2 dias"
    assert idade_texto(None) == "data desconhecida"


def test_lista_mostra_idade_e_botao_para_a_vaga_original(logado, painel):
    sv, _ = painel
    html = logado.get("/").text
    assert "aberta há" in html
    with sv.fabrica() as s:
        urls = [v.url for v in s.scalars(select(Vaga).join(Match).where(Match.score >= 70)).all()]
    assert urls
    for url in urls:  # um botão "Vaga original" por vaga, abrindo em nova aba e sem vazar o referrer
        assert f'href="{url}" target="_blank" rel="noopener noreferrer">Vaga original' in html


def test_lista_mostra_so_vagas_das_ultimas_24h_e_permite_incluir_antigas(logado, painel):
    sv, _ = painel
    with sv.fabrica() as s:
        v = s.scalars(select(Vaga).join(Match).where(Match.score >= 70)).first()
        titulo, vid = v.titulo, v.id
        v.data_publicacao = agora_utc() - timedelta(hours=30)
        s.commit()
    assert titulo not in logado.get("/").text
    assert titulo in logado.get("/?antigas=1").text
    assert "só vagas abertas nas últimas 24 h" in logado.get("/").text
    with sv.fabrica() as s:
        s.get(Vaga, vid).data_publicacao = agora_utc() - timedelta(hours=2)
        s.commit()
    assert titulo in logado.get("/").text


def test_lista_ordena_da_mais_nova_para_a_mais_antiga(logado, painel):
    sv, _ = painel
    with sv.fabrica() as s:
        vagas = s.scalars(select(Vaga).join(Match)).all()  # a boa (≥70%) e a de Go (abaixo do limite)
        assert len(vagas) >= 2
        vagas[0].data_publicacao = agora_utc() - timedelta(hours=10)
        vagas[1].data_publicacao = agora_utc() - timedelta(hours=1)
        antigo, novo = vagas[0].titulo, vagas[1].titulo
        s.commit()
    html = logado.get("/?todas=1").text
    assert html.index(novo) < html.index(antigo)


def test_rodar_agora_leva_para_a_tela_de_progresso(logado, painel):
    sv, _ = painel
    r = logado.post("/rodar-agora", data={"csrf": token(logado)}, follow_redirects=False)
    assert r.status_code == 303
    destino = r.headers["location"]
    assert re.fullmatch(r"/execucoes/\d+", destino)
    for _ in range(200):  # espera a rodada em segundo plano terminar
        with sv.fabrica() as s:
            ex = s.get(Execucao, int(destino.rsplit("/", 1)[1]))
            if ex.status != "rodando":
                break
        time.sleep(0.1)
    html = logado.get(destino).text
    assert 'role="progressbar"' in html and "Log completo" in html and "baixar .log" in html


def test_fragmento_de_progresso_faz_polling_so_enquanto_roda(logado, painel):
    sv, _ = painel
    with sv.fabrica() as s:
        ex = Execucao(origem="manual", status="rodando", etapa="Gerando CV 2/4: Dev Backend", progresso=72.0)
        s.add(ex)
        s.commit()
        eid = ex.id
        s.add(ExecucaoLog(execucao_id=eid, nivel="aviso", etapa="llm", mensagem="LLM gemini/x FALHOU (cota)"))
        s.commit()
    frag = logado.get(f"/execucoes/{eid}/progresso").text
    assert 'hx-trigger="every 2s"' in frag and "72%" in frag and "Gerando CV 2/4" in frag
    assert 'style="width: 72.0%"' in frag and "LLM gemini/x FALHOU" in frag
    with sv.fabrica() as s:
        e = s.get(Execucao, eid)
        e.status, e.progresso, e.fim = "concluida", 100.0, agora_utc()
        s.commit()
    assert "hx-trigger" not in logado.get(f"/execucoes/{eid}/progresso").text  # parou de atualizar


def test_log_filtra_por_nivel_e_baixa_completo(logado, painel):
    sv, _ = painel
    with sv.fabrica() as s:
        ex = Execucao(origem="agendada", status="concluida", progresso=100.0)
        s.add(ex)
        s.commit()
        eid = ex.id
        for i in range(450):  # mais que o limite da tela (400)
            s.add(ExecucaoLog(execucao_id=eid, nivel="info", etapa="match", mensagem=f"linha {i}"))
        s.add(ExecucaoLog(execucao_id=eid, nivel="erro", etapa="cv", mensagem="FALHA XYZ"))
        s.commit()
    tela = logado.get(f"/execucoes/{eid}").text
    assert "Mostrando as 400 linhas mais recentes de 451" in tela
    so_erros = logado.get(f"/execucoes/{eid}?nivel=erro").text
    assert "FALHA XYZ" in so_erros and "linha 3<" not in so_erros
    txt = logado.get(f"/execucoes/{eid}/log.txt")
    assert txt.status_code == 200 and "attachment" in txt.headers["content-disposition"]
    assert "linha 0" in txt.text and "linha 449" in txt.text and "[ERRO ]" in txt.text  # arquivo tem TODAS as linhas
    assert logado.get("/execucoes/99999").status_code == 404


def test_lista_de_execucoes(logado):
    html = logado.get("/execucoes").text
    assert "Execuções" in html and "Rodar agora" in html and "#1" in html


def test_integracoes_mostra_historico_das_llms(logado, painel):
    sv, _ = painel
    with sv.fabrica() as s:
        s.add(LlmChamada(provedor="gemini", modelo="gemini-flash-latest", tarefa="reescrita_cv", sucesso=False,
                         erro="HTTP 503: high demand", tipo_erro="temporario", http_status=503, duracao_ms=2300))
        s.add(LlmChamada(provedor="gemini", modelo="gemini-flash-latest", tarefa="reescrita_cv", sucesso=True,
                         tokens_in=2000, tokens_out=900, duracao_ms=61000))
        s.commit()
    html = logado.get("/integracoes").text
    assert "Histórico das LLMs" in html and "gemini-flash-latest" in html
    assert "temporario: 1" in html and "HTTP 503: high demand" in html


def test_llm_log_filtra_e_exporta_csv(logado, painel):
    sv, _ = painel
    with sv.fabrica() as s:
        s.add(LlmChamada(provedor="groq", modelo="m1", tarefa="extracao", sucesso=False, erro="quota esgotada " * 40,
                         tipo_erro="cota", http_status=429, duracao_ms=400, execucao_id=1, vaga_id=1))
        s.commit()
    tudo = logado.get("/llm-log").text
    assert "Log completo das chamadas de LLM" in tudo and "cota" in tudo
    falhas = logado.get("/llm-log?resultado=falha&provedor=groq&tipo=cota").text
    assert "quota esgotada" in falhas and "erro completo" in falhas
    assert "quota esgotada" not in logado.get("/llm-log?resultado=ok").text
    # o formulário GET envia execucao= vazio quando o campo não é preenchido
    assert "quota esgotada" in logado.get("/llm-log?provedor=&resultado=&tipo=&execucao=").text
    assert logado.get("/llm-log.csv?execucao=").status_code == 200
    assert logado.get("/llm-log?execucao=1").status_code == 200
    csv_ = logado.get("/llm-log.csv?provedor=groq")
    assert csv_.status_code == 200 and csv_.text.splitlines()[0].startswith("id,quando_utc,provedor")
    assert "quota esgotada" in csv_.text and "429" in csv_.text  # erro completo, sem truncar


def test_telas_novas_exigem_login(cliente):
    for rota in ("/execucoes", "/execucoes/1", "/execucoes/1/progresso", "/execucoes/1/log.txt", "/llm-log",
                 "/llm-log.csv"):
        r = cliente.get(rota, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", rota


def test_idade_maxima_editavel_pelo_painel(logado, painel):
    sv, _ = painel
    t = token(logado)
    base = {"csrf": t, "repos": "backend-br/vagas | Remoto", "bloqueadas": ""}
    logado.post("/configuracoes/fontes", data={**base, "idade_max": "48"})
    assert sv.config.obter("filtros.idade_maxima_horas") == 48
    logado.post("/configuracoes/fontes", data={**base, "idade_max": "abc"})
    assert sv.config.obter("filtros.idade_maxima_horas") == 48  # entrada inválida não altera
    assert 'value="48"' in logado.get("/configuracoes").text


def test_htmx_e_servido_com_mime_de_javascript(cliente):
    """Regressão: com MIME text/plain + nosniff o navegador recusa o htmx e nenhum botão/polling funciona."""
    r = cliente.get("/static/htmx.min.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "css" in cliente.get("/static/estilo.css").headers["content-type"]
