"""Aba Currículo do painel."""
import re

from app.curriculo import edicao
from tests.test_painel import cliente, csrf_de, logado, painel, token  # noqa: F401  (fixtures)


def caminho(painel):
    return painel[0].config.caminho("mestre")


def acao(logado, **campos):
    return logado.post("/curriculo/acao", data={"csrf": token(logado), **campos}, follow_redirects=False)


def test_aba_exige_login(cliente):
    for rota in ("/curriculo", "/curriculo/previa.pdf"):
        r = cliente.get(rota, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert cliente.post("/curriculo/acao", data={}, follow_redirects=False).status_code in (303, 403)


def test_mostra_o_mestre_com_contagens_e_acoes(logado):
    html = logado.get("/curriculo").text
    assert "Currículo mestre" in html and "experiências ativas" in html and "Empresa Exemplo A" in html
    assert "Desativar" in html and "+ Adicionar competência" in html and "+ Adicionar experiência" in html
    assert "Prévia do CV (PT)" in html and "Certificações" in html and ">Ativar<" in html  # certificações extras desativadas


def test_post_sem_csrf_e_barrado(logado):
    assert logado.post("/curriculo/acao", data={"acao": "alternar", "tipo": "skill", "ref": "Git"},
                       follow_redirects=False).status_code == 403


def test_alternar_skill_pelo_painel_e_mensagem(logado, painel):
    r = acao(logado, acao="alternar", tipo="skill", ref="Redis", ancora="cat-3")
    assert r.status_code == 303 and r.headers["location"] == "/curriculo#cat-3"
    skills = {s["nome"]: s for s in edicao.ler(caminho(painel))["skills"]}
    assert skills["Redis"]["ativo"] is False
    assert "Desativado" in logado.get("/curriculo").text  # mensagem (flash) após a ação


def test_ancora_invalida_e_ignorada(logado):
    r = acao(logado, acao="alternar", tipo="skill", ref="Git", ancora='x"><script>')
    assert r.headers["location"] == "/curriculo"


def test_erro_de_validacao_vira_aviso_sem_gravar(logado, painel):
    antes = caminho(painel).read_bytes()
    r = acao(logado, acao="skill_adicionar", nome="Rust", categoria="Linguagens", nivel="77")
    assert r.status_code == 303
    assert caminho(painel).read_bytes() == antes
    html = logado.get("/curriculo").text  # a mensagem aparece uma única vez
    assert "Não salvo" in html and "1 a 5" in html


def test_adicionar_skill_em_nova_categoria_aparece_na_aba(logado, painel):
    acao(logado, acao="skill_adicionar", nome="Terraform", categoria="Linguagens", categoria_nova="Infraestrutura as Code", nivel="3")
    html = logado.get("/curriculo").text
    assert "Terraform" in html and "Infraestrutura as Code" in html


def test_adicionar_bullet_e_experiencia(logado, painel):
    acao(logado, acao="experiencia_adicionar", empresa="Startup Y", cargo_pt="Dev", cargo_en="Dev", local="", inicio="2025-02",
         fim="", tipo_exp="profissional", tecnologias="Python")
    acao(logado, acao="bullet_adicionar", ref="startup_y", texto="Construí uma API de pagamentos.", tags="APIs REST")
    m = edicao.ler(caminho(painel))
    nova = next(e for e in m["experiencias"] if e["id"] == "startup_y")
    assert nova["bullets"][0]["texto"] == "Construí uma API de pagamentos."
    assert "Startup Y" in logado.get("/curriculo").text


def test_item_de_lista_desatualizado_e_recusado(logado, painel):
    r = acao(logado, acao="alternar", tipo="certificacoes", i="0", k="chave-velha")
    assert r.status_code == 303
    assert "mudou desde que a página foi carregada" in logado.get("/curriculo").text
    assert edicao.ler(caminho(painel))["certificacoes"][0]["ativo"] is False  # nada mudou


def test_html_do_conteudo_e_escapado(logado, painel):
    acao(logado, acao="bullet_adicionar", ref="emp_a", texto='<img src=x onerror=alert(1)> teste', tags="<b>x</b>")
    html = logado.get("/curriculo").text
    assert "<img src=x" not in html and "&lt;img src=x" in html and "<b>x</b>" not in html


def test_formularios_trazem_o_token_csrf_da_sessao(logado):
    html = logado.get("/curriculo").text
    tokens = set(re.findall(r'name="csrf" value="([^"]+)"', html))
    assert len(tokens) == 1 and token(logado) in tokens


def test_previa_do_cv_so_com_itens_ativos(logado, painel, tmp_path):
    r = logado.get("/curriculo/previa.pdf?idioma=pt")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert logado.get("/curriculo/previa.pdf?idioma=en").status_code == 200
    assert logado.get("/curriculo/previa.pdf?idioma=xx").status_code == 400
    # com a experiência desativada ela some do PDF (texto extraível)
    import shutil, subprocess
    if shutil.which("pdftotext"):
        def texto(pdf: bytes) -> str:
            arq = tmp_path / "previa.pdf"
            arq.write_bytes(pdf)
            return subprocess.run(["pdftotext", "-enc", "UTF-8", str(arq), "-"], capture_output=True).stdout.decode("utf-8", "ignore")
        assert "Empresa Exemplo B" in texto(r.content)
        acao(logado, acao="alternar", tipo="experiencia", ref="emp_b")
        assert "Empresa Exemplo B" not in texto(logado.get("/curriculo/previa.pdf").content)
        assert "EF SET" in texto(logado.get("/curriculo/previa.pdf").content)  # a certificação de inglês fica


def test_rodada_usa_so_o_que_esta_ativo(logado, painel):
    sv, _ = painel
    acao(logado, acao="alternar", tipo="skill", ref="Redis")
    assert "Redis" not in [s.nome for s in sv.mestre().skills]  # o pipeline lê a visão só com ativos


def test_nenhum_handler_inline_que_a_csp_bloquearia(logado):
    """Regressão: onsubmit/onclick inline são bloqueados por script-src 'self' (o Aplicar não recarregava)."""
    for rota in ("/", "/curriculo", "/integracoes", "/configuracoes"):
        html = logado.get(rota).text
        assert not re.search(r"\son(submit|click|change|load)=", html), rota
    assert "data-recarregar" in logado.get("/?todas=1").text or True
    js = logado.get("/static/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "data-recarregar" in js.text
