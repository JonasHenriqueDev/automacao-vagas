"""Aba Gerar CV: colar vaga, perguntar sobre as lacunas, gravar no mestre e gerar o PDF."""
import json
import shutil
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.curriculo import edicao, lacunas
from app.curriculo.mestre import carregar_mestre
from app.db.models import Match, Vaga
from app.match.requisitos import RequisitosVaga
from app.painel.app import criar_app
from app.painel.auth import definir_credenciais
from tests.conftest import RAIZ, resposta_chat
from tests.test_curriculo import saida_valida
from tests.test_painel import SENHA, logar, token
from tests.test_pipeline import preparar

DESCRICAO = ("Buscamos pessoa desenvolvedora backend, 100% remoto. Requisitos: Node.js, NestJS, Kubernetes e "
             "Terraform. Diferencial: Go. " * 3)

EXTRACAO = {"obrigatorios": ["Node.js", "NestJS", "Kubernetes", "Terraform"], "desejaveis": ["Go"],
            "senioridade": "pleno", "modelo_trabalho": "remoto", "local": "", "salario": "", "idioma": "pt"}


@pytest.fixture
def arquivo(tmp_path) -> Path:
    destino = tmp_path / "curriculo_mestre.yaml"
    shutil.copy(RAIZ / "data" / "curriculo_mestre.example.yaml", destino)
    return destino


# ------------------------------------------------------------------ lacunas
def test_lacunas_sao_o_que_nenhuma_skill_ativa_cobre(arquivo):
    req = RequisitosVaga(obrigatorios=["Node.js", "Kubernetes", "kubernetes", "Terraform"], desejaveis=["Go", "AWS"])
    achadas = lacunas.encontrar(req, carregar_mestre(arquivo))
    assert [(l.termo, l.obrigatoria) for l in achadas] == [
        ("Kubernetes", True), ("Terraform", True), ("Go", False)]  # sem repetição e sem o que o mestre já cobre


def test_skill_desativada_vira_oferta_de_reativar(arquivo):
    edicao.aplicar(arquivo, "alternar", {"tipo": "skill", "ref": "Redis"})
    [l] = lacunas.encontrar(RequisitosVaga(obrigatorios=["Redis"]), carregar_mestre(arquivo))
    assert (l.termo, l.inativa) == ("Redis", "Redis")


# ------------------------------------------------------------------ gravação no mestre
def test_tenho_adiciona_skill_tecnologia_e_bullet(arquivo):
    msg = edicao.aplicar(arquivo, "conhecimentos_aplicar", {
        "n": "2",
        "resp_0": "sim", "nome_0": "Kubernetes", "nivel_0": "2", "cat_0": "Infra e DevOps", "exp_0": "emp_b",
        "texto_0": "Publiquei serviços em um cluster Kubernetes do time.",
        "resp_1": "nao", "nome_1": "Terraform",
    })
    assert "Kubernetes" in msg and "Terraform" not in msg
    m = carregar_mestre(arquivo)
    skill = next(s for s in m.skills if s.nome == "Kubernetes")
    assert (skill.categoria, skill.nivel, skill.academico) == ("Infra e DevOps", 2, False)
    emp_b = m.experiencia("emp_b")
    assert "Kubernetes" in emp_b.tecnologias
    assert any("cluster Kubernetes" in b.texto and b.tags == ["Kubernetes"] for b in emp_b.bullets)
    assert not any(s.nome == "Terraform" for s in m.skills)
    assert list((arquivo.parent / "backups").glob("*.yaml"))  # a alteração deixou backup


def test_tenho_sem_experiencia_so_cria_a_skill_em_outras(arquivo):
    edicao.aplicar(arquivo, "conhecimentos_aplicar", {"n": "1", "resp_0": "sim", "nome_0": "Terraform", "nivel_0": "3"})
    m = carregar_mestre(arquivo)
    s = next(s for s in m.skills if s.nome == "Terraform")
    assert s.categoria == "Outras"
    assert all("Terraform" not in e.tecnologias for e in m.experiencias)


def test_reativar_e_tenho_em_skill_existente_nao_duplica(arquivo):
    edicao.aplicar(arquivo, "alternar", {"tipo": "skill", "ref": "Redis"})
    edicao.aplicar(arquivo, "conhecimentos_aplicar", {"n": "2", "resp_0": "reativar", "reativar_0": "Redis",
                                                       "resp_1": "sim", "nome_1": "redis"})
    skills = [s for s in carregar_mestre(arquivo).skills if s.nome.lower() == "redis"]
    assert len(skills) == 1 and skills[0].ativo


@pytest.mark.parametrize("campos, trecho", [
    ({"n": "1", "resp_0": "sim", "nome_0": "Kafka X", "acad_0": "on", "exp_0": "emp_a"}, "acadêmico"),
    ({"n": "1", "resp_0": "sim", "nome_0": "Kafka X", "texto_0": "Fiz coisas."}, "escolha em qual experiência"),
    ({"n": "1", "resp_0": "sim", "nome_0": "", "nivel_0": "3"}, "Preencha"),
    ({"n": "1", "resp_0": "sim", "nome_0": "X", "nivel_0": "9"}, "1 a 5"),
    ({"n": "1", "resp_0": "sim", "nome_0": "X", "exp_0": "nao_existe"}, "Experiência não encontrada"),
    ({"n": "1", "resp_0": "talvez"}, "Resposta inválida"),
    ({"n": "999"}, "Formulário inválido"),
])
def test_respostas_invalidas_nao_gravam_nada(arquivo, campos, trecho):
    antes = arquivo.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match=trecho):
        edicao.aplicar(arquivo, "conhecimentos_aplicar", campos)
    assert arquivo.read_text(encoding="utf-8") == antes


# ------------------------------------------------------------------ painel de ponta a ponta
def handler_llm(chamadas: list):
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.host in ("api.github.com", "discord.com"):
            return httpx.Response(200, json=[])
        corpo = json.loads(req.content)
        sistema = corpo["messages"][0]["content"]
        chamadas.append(corpo["messages"][1]["content"])
        if "extrai requisitos" in sistema:
            return resposta_chat(EXTRACAO)
        return resposta_chat(saida_valida())
    return h


@pytest.fixture
def painel_gerar(fabrica_servicos, monkeypatch):
    chamadas: list = []
    sv = preparar(fabrica_servicos, monkeypatch, handler_llm(chamadas))
    definir_credenciais(sv, "admin", SENHA)
    return sv, criar_app(sv, com_agendador=False), chamadas


@pytest.fixture
def web(painel_gerar):
    with TestClient(painel_gerar[1], base_url="http://testserver") as c:
        assert logar(c).status_code == 303
        yield c


def colar(web, **extra):
    r = web.post("/gerar/analisar", data={"csrf": token(web), "descricao": DESCRICAO, **extra}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def test_aba_aparece_no_menu_e_exige_login(web, painel_gerar):
    with TestClient(painel_gerar[1], base_url="http://testserver") as anonimo:
        assert anonimo.get("/gerar", follow_redirects=False).headers["location"] == "/login"
        assert anonimo.get("/gerar/1", follow_redirects=False).headers["location"] == "/login"
        assert anonimo.post("/gerar/analisar", data={}, follow_redirects=False).status_code in (303, 403)
    assert 'href="/gerar"' in web.get("/gerar").text and "Analisar vaga" in web.get("/gerar").text


def test_descricao_curta_e_recusada_sem_criar_vaga(web, painel_gerar):
    sv, _, _ = painel_gerar
    r = web.post("/gerar/analisar", data={"csrf": token(web), "descricao": "curta"}, follow_redirects=False)
    assert r.headers["location"] == "/gerar"
    with sv.fabrica() as s:
        assert s.scalar(select(Vaga.id)) is None


def test_analise_lista_so_o_que_falta_no_mestre(web):
    destino = colar(web, titulo="Dev Backend", empresa="Acme")
    html = web.get(destino).text
    assert "Dev Backend" in html and "Você tem conhecimento nisso?" in html
    for termo in ("Kubernetes", "Terraform"):
        assert f'name="termo_{["Kubernetes", "Terraform", "Go"].index(termo)}" value="{termo}"' in html
    assert 'value="Node.js"' not in html and 'value="NestJS"' not in html  # o mestre já cobre
    assert 'name="n" value="3"' in html


def test_responder_tenho_grava_no_mestre_e_gera_o_cv(web, painel_gerar):
    sv, _, chamadas = painel_gerar
    destino = colar(web, titulo="Dev Backend", empresa="Acme", url="javascript:alert(1)")
    vaga_id = int(destino.rsplit("/", 1)[1])
    r = web.post(f"{destino}/gerar", data={
        "csrf": token(web), "n": "3",
        "resp_0": "sim", "nome_0": "Kubernetes", "nivel_0": "2", "cat_0": "Outras", "exp_0": "emp_b",
        "texto_0": "Subi serviços em um cluster Kubernetes.",
        "resp_1": "nao", "resp_2": "nao",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/vaga/{vaga_id}"

    m = carregar_mestre(sv.config.caminho("mestre"))
    assert "Kubernetes" in [s.nome for s in m.skills] and "Terraform" not in [s.nome for s in m.skills]
    with sv.fabrica() as s:
        v = s.get(Vaga, vaga_id)
        assert v.fonte == "manual" and v.url == ""  # URL que não é http(s) é descartada
        assert len(v.curriculos) == 1 and Path(v.curriculos[0].caminho_pdf).read_bytes().startswith(b"%PDF")
        assert v.match.notificado is True and v.match.status == "novo"
    detalhe = web.get(f"/vaga/{vaga_id}").text
    assert "Kubernetes" in detalhe and "CV gerado" in detalhe
    # com Kubernetes no mestre, a vaga deixa de ter essa lacuna
    assert 'value="Kubernetes"' not in web.get(destino).text
    # nenhum dado pessoal vai à LLM (o fluxo reaproveita a checagem de privacidade da geração normal)
    assert not any("maria.teste" in c.lower() for c in chamadas)


def test_gerar_sem_respostas_nao_mexe_no_mestre(web, painel_gerar):
    sv, _, _ = painel_gerar
    antes = sv.config.caminho("mestre").read_text(encoding="utf-8")
    destino = colar(web)
    web.post(f"{destino}/gerar", data={"csrf": token(web), "n": "3", "resp_0": "nao", "resp_1": "nao", "resp_2": "nao"},
             follow_redirects=False)
    assert sv.config.caminho("mestre").read_text(encoding="utf-8") == antes


def test_resposta_invalida_volta_com_aviso_e_nao_gera_cv(web, painel_gerar):
    sv, _, _ = painel_gerar
    destino = colar(web)
    r = web.post(f"{destino}/gerar", data={"csrf": token(web), "n": "1", "resp_0": "sim", "nome_0": "Kafka X",
                                           "acad_0": "on", "exp_0": "emp_a"}, follow_redirects=False)
    assert r.headers["location"] == destino
    assert "Nada foi salvo" in web.get(destino).text
    with sv.fabrica() as s:
        assert s.scalar(select(Vaga)).curriculos == []


def test_mesma_descricao_reaproveita_a_vaga(web, painel_gerar):
    sv, _, _ = painel_gerar
    assert colar(web) == colar(web)
    with sv.fabrica() as s:
        assert len(s.scalars(select(Vaga)).all()) == 1


def test_vaga_manual_fica_fora_da_rodada_agendada(web, painel_gerar):
    from app.pipeline import processar_pendentes
    sv, _, chamadas = painel_gerar
    colar(web)
    chamadas.clear()
    processar_pendentes(sv)
    with sv.fabrica() as s:
        assert s.scalar(select(Match.id)) is None  # a rodada não pontuou a vaga colada
    assert chamadas == []


def test_llm_indisponivel_cai_para_palavras_e_oferece_tentar_de_novo(fabrica_servicos, monkeypatch):
    sv = preparar(fabrica_servicos, monkeypatch, lambda req: httpx.Response(429, json={"error": "cota"}))
    definir_credenciais(sv, "admin", SENHA)
    with TestClient(criar_app(sv, com_agendador=False), base_url="http://testserver") as c:
        logar(c)
        destino = colar(c)
        html = c.get(destino).text
        assert "análise por palavras-chave" in html and "Tentar de novo com a LLM" in html
        assert 'name="termo_0" value="kubernetes"' in html  # a taxonomia ainda detecta as tecnologias conhecidas
        r = c.post(f"{destino}/gerar", data={"csrf": token(c), "n": "0"}, follow_redirects=False)
        assert r.headers["location"] == destino and "CV adiado" in c.get(destino).text


# ------------------------------------------------------------------ «Melhorar com a LLM»
def painel_melhorar(fabrica_servicos, monkeypatch, resposta, na_reescrita=None):
    """Painel cuja LLM responde `resposta` (dict) à tarefa de reescrita; guarda os prompts recebidos.
    `na_reescrita` (opcional) é chamado quando a LLM recebe a tarefa de reescrita, antes de responder."""
    prompts: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        corpo = json.loads(req.content)
        prompts.append(corpo["messages"][1]["content"])
        if "extrai requisitos" in corpo["messages"][0]["content"]:
            return resposta_chat(EXTRACAO)
        if na_reescrita:
            na_reescrita()
        return resposta_chat(resposta)

    sv = preparar(fabrica_servicos, monkeypatch, h)
    definir_credenciais(sv, "admin", SENHA)
    return sv, criar_app(sv, com_agendador=False), prompts


def melhorar(c, destino, texto, **extra):
    return c.post(f"{destino}/melhorar-texto", data={
        "i": "0", "termo_0": "Kubernetes", "nome_0": "Kubernetes", "texto_0": texto, **extra},
        headers={"X-CSRF-Token": token(c)})


def test_melhorar_devolve_o_texto_sugerido_sem_gravar_nada(fabrica_servicos, monkeypatch):
    sv, app, prompts = painel_melhorar(fabrica_servicos, monkeypatch, {
        "texto": "Publicação de serviços em cluster Kubernetes do time, com 3 deploys por semana."})
    with TestClient(app, base_url="http://testserver") as c:
        logar(c)
        destino = colar(c)
        antes = sv.config.caminho("mestre").read_text(encoding="utf-8")
        r = melhorar(c, destino, "subi uns servicos num cluster kubernetes, 3 deploys por semana")
        assert r.status_code == 200 and 'id="bloco_texto_0"' in r.text and "Publicação de serviços" in r.text
        assert "confira se os fatos" in r.text
        assert sv.config.caminho("mestre").read_text(encoding="utf-8") == antes  # só sugere; quem grava é o formulário
    assert "subi uns servicos" in prompts[-1] and "Maria" not in prompts[-1]


def test_melhorar_nao_trava_o_painel_enquanto_a_llm_demora(fabrica_servicos, monkeypatch):
    """Regressão: a chamada síncrona à LLM rodava no event loop e o painel inteiro parava até ela terminar."""
    chegou, liberar = threading.Event(), threading.Event()

    def llm_lenta():
        chegou.set()
        liberar.wait(10)

    _, app, _ = painel_melhorar(fabrica_servicos, monkeypatch, {"texto": "Publicação de serviços Kubernetes."},
                                na_reescrita=llm_lenta)
    with TestClient(app, base_url="http://testserver") as c:
        logar(c)
        destino = colar(c)
        resultado = {}
        t = threading.Thread(target=lambda: resultado.update(r=melhorar(c, destino, "subi servicos no kubernetes")))
        t.start()
        try:
            assert chegou.wait(10), "a requisição de melhorar não chegou à LLM"
            inicio = time.monotonic()
            assert c.get("/healthz").status_code == 200  # responde mesmo com a LLM ainda pendente
            assert time.monotonic() - inicio < 3
        finally:
            liberar.set()
            t.join(15)
        assert resultado["r"].status_code == 200


@pytest.mark.parametrize("sugestao, motivo", [
    ({"texto": "Publicação de serviços Kubernetes com 5 deploys por semana."}, "mantive o seu texto"),  # número novo
    ({"texto": "Publicação de serviços Kubernetes com Terraform e Docker."}, "mantive o seu texto"),  # tecnologia nova
])
def test_melhorar_rejeita_sugestao_que_inventa_fatos(fabrica_servicos, monkeypatch, sugestao, motivo):
    _, app, _ = painel_melhorar(fabrica_servicos, monkeypatch, sugestao)
    with TestClient(app, base_url="http://testserver") as c:
        logar(c)
        destino = colar(c)
        r = melhorar(c, destino, "Subi serviços num cluster Kubernetes, 3 deploys por semana")
        assert motivo in r.text and "Subi serviços num cluster Kubernetes, 3 deploys por semana" in r.text
        assert "5 deploys" not in r.text and "Terraform e Docker" not in r.text


def test_melhorar_exige_texto_e_barra_dados_pessoais(fabrica_servicos, monkeypatch):
    _, app, prompts = painel_melhorar(fabrica_servicos, monkeypatch, {"texto": "qualquer"})
    with TestClient(app, base_url="http://testserver") as c:
        logar(c)
        destino = colar(c)
        chamadas = len(prompts)
        assert "Escreva primeiro" in melhorar(c, destino, "   ").text
        r = melhorar(c, destino, "Trabalhei na Empresa Exemplo B com Kubernetes")
        assert "nome de empresa ou dado pessoal" in r.text and "Empresa Exemplo B" in r.text  # texto do usuário preservado
        assert len(prompts) == chamadas  # nada foi enviado à LLM
        assert melhorar(c, destino, "x", i="99").status_code == 400
        assert c.post(f"{destino}/melhorar-texto", data={"i": "0"}).status_code == 403  # sem CSRF


def test_formulario_traz_o_botao_melhorar_por_item(web):
    html = web.get(colar(web)).text
    assert html.count("Melhorar com a LLM") == 3 and '{"i": "1"}' in html
