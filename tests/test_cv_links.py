import re

from app.curriculo.personalizar import validar_saida
from app.curriculo.render import _contatos, _url, renderizar
from tests.conftest import RAIZ


def test_url_ganha_esquema_https():
    assert _url("linkedin.com/in/x") == "https://linkedin.com/in/x"
    assert _url("https://github.com/x") == "https://github.com/x"
    assert _url("mailto:a@b.co") == "mailto:a@b.co"


def test_contatos_viram_links_exceto_localizacao(pessoais):
    por_texto = {c["texto"]: c["url"] for c in _contatos(pessoais)}
    assert por_texto[pessoais.email] == f"mailto:{pessoais.email}"
    assert por_texto[pessoais.telefone] == "tel:11912345678"
    assert por_texto[pessoais.localizacao] == ""
    assert por_texto["github.com/mariateste"] == "https://github.com/mariateste"


def test_pdf_tem_hiperlinks_clicaveis_sem_cor_azul(tmp_path, mestre, pessoais):
    pdf = renderizar(validar_saida({}, mestre, "pt"), pessoais, RAIZ / "templates_cv", tmp_path / "cv.pdf")
    dados = pdf.read_bytes()
    uris = set(re.findall(rb"/URI\s*\(([^)]*)\)", dados))
    assert b"https://github.com/mariateste" in uris and b"mailto:maria.teste@exemplo.org" in uris
    assert b"tel:11912345678" in uris
    # o texto do link usa a mesma cor do resto (preto): o template força `show link: set text(fill: black)`
    assert "show link: set text(fill: black)" in (RAIZ / "templates_cv" / "cv.typ").read_text(encoding="utf-8")
