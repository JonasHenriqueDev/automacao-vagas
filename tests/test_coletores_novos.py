"""Coletores novos: APIs remotas, Gupy, quadros (Greenhouse/Lever/Ashby), ProgramaThor e Vagas.com."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.coletores.apis_remotas import (
    ColetorArbeitnow,
    ColetorHimalayas,
    ColetorJobicy,
    ColetorRemoteOK,
    ColetorRemotive,
    ColetorWWR,
)
from app.coletores.ats import ColetorAshby, ColetorGreenhouse, ColetorLever, parse_quadro
from app.coletores.base import (
    VagaBruta,
    data_epoch,
    data_iso,
    elegivel_do_brasil,
    html_para_texto,
    local_brasil,
)
from app.coletores.gupy import ColetorGupy
from app.coletores.scraping import (
    ColetorProgramaThor,
    ColetorVagasCom,
    data_relativa,
    descricao_json_ld,
    parse_programathor,
    parse_vagas_com,
)
from app.match.filtros import prefiltro_titulo
from app.config import PADRAO


def agora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(horas_atras: float) -> str:
    return (agora() - timedelta(hours=horas_atras)).strftime("%Y-%m-%dT%H:%M:%S")


def epoch(horas_atras: float) -> int:
    return int((agora() - timedelta(hours=horas_atras)).replace(tzinfo=timezone.utc).timestamp())


def cliente(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


# ------------------------------------------------------------------ helpers
def test_html_para_texto_trata_escape_duplo_e_mojibake():
    assert html_para_texto("&lt;p&gt;Ol&amp;aacute;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;").startswith("Ol")
    assert "- Python" in html_para_texto("<p>Requisitos</p><ul><li>Python</li><li>SQL</li></ul>")
    assert html_para_texto("<p>QuÃ© es Deazy</p>") == "Qué es Deazy"
    assert "alert" not in html_para_texto("<script>alert(1)</script><p>ok</p>")


@pytest.mark.parametrize("local,esperado", [
    ("", True), ("Remote", True), ("Worldwide", True), ("Anywhere in the World", True), ("LATAM,  Canada", True),
    ("Brazil", True), ("São Paulo, SP", True), ("USA Only", False), ("Germany", False), ("Europe", False),
    ("United States", False), ("Canada,  Germany,  UK,  USA", False),
])
def test_elegivel_do_brasil(local, esperado):
    assert elegivel_do_brasil(local) is esperado


def test_local_brasil_nao_aceita_remote_generico():
    assert local_brasil("Remote - Brazil") and local_brasil("Brasil") and local_brasil("Latin America")
    assert not local_brasil("Remote") and not local_brasil("Worldwide") and not local_brasil("")


def test_datas():
    assert data_iso("2026-10-15T12:00:00Z") == datetime(2026, 10, 15, 12)
    assert data_iso("2026-10-15T12:00:00-04:00") == datetime(2026, 10, 15, 16)
    assert data_iso("lixo") is None and data_iso(None) is None
    assert data_epoch(1792022419) == data_epoch(1792022419000) and data_epoch(0) is None and data_epoch("x") is None
    base = datetime(2026, 10, 16, 12)
    assert data_relativa("Hoje", base) == base and data_relativa("Ontem", base) == base - timedelta(days=1)
    assert data_relativa("Há 2 dias", base) == base - timedelta(days=2)
    assert data_relativa("Há 3 horas", base) == base - timedelta(hours=3)
    assert data_relativa("2 days ago", base) == base - timedelta(days=2) and data_relativa("???", base) is None


def test_parse_quadro():
    assert parse_quadro("greenhouse:quintoandar") == ("greenhouse", "quintoandar")
    assert parse_quadro(" Lever : dlocal ") == ("lever", "dlocal")
    assert parse_quadro("workday:x") is None and parse_quadro("ashby:") is None and parse_quadro("ashby:a/b") is None


def test_prefiltro_titulo_usa_as_regras_do_filtro():
    cfg = PADRAO["filtros"]
    assert prefiltro_titulo(VagaBruta("x", "u", "Senior Backend Engineer"), cfg)
    assert prefiltro_titulo(VagaBruta("x", "u", "Account Executive"), cfg)
    assert prefiltro_titulo(VagaBruta("x", "u", "Desenvolvedor Back-end Pleno"), cfg) is None


# ------------------------------------------------------------------ APIs remotas
def test_remotive_filtra_pais_e_idade():
    def handler(req):
        assert req.url.params["category"] == "software-dev"
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "url": "https://remotive.com/1", "title": "Backend Dev", "company_name": "A",
             "publication_date": iso(3), "candidate_required_location": "Worldwide", "tags": ["python"],
             "job_type": "full_time", "salary": "$50k", "description": "<p>Python</p>"},
            {"id": 2, "url": "https://remotive.com/2", "title": "Dev US", "company_name": "B",
             "publication_date": iso(3), "candidate_required_location": "USA Only", "description": ""},
            {"id": 3, "url": "https://remotive.com/3", "title": "Dev velha", "company_name": "C",
             "publication_date": iso(100), "candidate_required_location": "Worldwide", "description": ""},
        ]})

    col = ColetorRemotive(cliente(handler), 24)
    vagas = col.coletar()
    assert [v.url for v in vagas] == ["https://remotive.com/1"]
    v = vagas[0]
    assert v.modelo_trabalho == "remoto" and v.descricao == "Python" and "python" in v.labels and v.salario == "$50k"
    assert col.stats["fora_do_pais"] == 1 and col.stats["antigas"] == 1 and col.internacional


def test_remoteok_pula_aviso_legal_e_conserta_acentos():
    def handler(req):
        return httpx.Response(200, json=[
            {"legal": "termos"},
            {"id": "9", "epoch": epoch(2), "company": "Somnio", "position": "Dev Node", "tags": ["node", "dev"],
             "description": "<p>QuÃ© es</p>", "location": "", "url": "https://remoteok.com/remote-jobs/9",
             "salary_min": 40000, "salary_max": 60000},
            {"id": "10", "epoch": epoch(2), "company": "X", "position": "Dev EU", "location": "Europe",
             "url": "https://remoteok.com/remote-jobs/10"},
        ])

    vagas = ColetorRemoteOK(cliente(handler), 24).coletar()
    assert len(vagas) == 1 and vagas[0].descricao == "Qué es" and vagas[0].salario.startswith("USD 40")


def test_jobicy_consulta_geral_e_latam_sem_duplicar():
    chamadas = []

    def handler(req):
        chamadas.append(dict(req.url.params))
        job = {"id": 1, "url": "https://jobicy.com/jobs/1", "jobTitle": "Backend Developer", "companyName": "Acme",
               "jobGeo": "LATAM", "jobLevel": "Midweight", "jobDescription": "<p>Go</p>", "pubDate": iso(1) + "+00:00",
               "jobType": ["Full-Time"], "jobIndustry": ["Engineering"]}
        usa = {**job, "id": 2, "url": "https://jobicy.com/jobs/2", "jobGeo": "USA"}
        return httpx.Response(200, json={"jobs": [job, usa]})

    vagas = ColetorJobicy(cliente(handler), 24).coletar()
    assert len(chamadas) == 2 and chamadas[1].get("geo") == "latam"
    assert [v.url for v in vagas] == ["https://jobicy.com/jobs/1"]
    assert "Midweight" in vagas[0].labels and "Engineering" not in vagas[0].labels


def test_himalayas_para_na_vaga_velha_e_so_brasil_usa_so_o_pais():
    escopos = []

    def handler(req):
        escopos.append(dict(req.url.params))
        jobs = [{"title": f"Dev {i}", "companyName": "A", "applicationLink": f"https://himalayas.app/j/{req.url.params['q']}{i}",
                 "pubDate": epoch(1 if i < 2 else 80), "locationRestrictions": [], "description": "<p>x</p>",
                 "seniority": ["Mid-level"]} for i in range(20)]
        return httpx.Response(200, json={"jobs": jobs})

    col = ColetorHimalayas(cliente(handler), 24, max_paginas=3, so_brasil=True)
    vagas = col.coletar()
    assert all("country" in e and "worldwide" not in e for e in escopos) and not col.internacional
    assert len(vagas) == 4 and all(v.labels == ["Mid-level"] for v in vagas)  # 2 termos x 2 vagas novas; 1 página cada
    assert len(escopos) == 2

    escopos.clear()
    ColetorHimalayas(cliente(handler), 24).coletar()
    assert {tuple(sorted(set(e) & {"worldwide", "country"})) for e in escopos} == {("worldwide",), ("country",)}


def test_arbeitnow_so_remotas_sem_restricao():
    def handler(req):
        return httpx.Response(200, json={"data": [
            {"slug": "a", "url": "https://arbeitnow.com/a", "title": "Dev", "company_name": "A", "remote": True,
             "location": "", "created_at": epoch(2), "description": "&lt;p&gt;Hi&lt;/p&gt;", "tags": [], "job_types": []},
            {"slug": "b", "url": "https://arbeitnow.com/b", "title": "Dev", "company_name": "B", "remote": False,
             "location": "", "created_at": epoch(2), "description": ""},
            {"slug": "c", "url": "https://arbeitnow.com/c", "title": "Dev", "company_name": "C", "remote": True,
             "location": "Berlin", "created_at": epoch(2), "description": ""},
        ]})

    vagas = ColetorArbeitnow(cliente(handler), 24).coletar()
    assert [v.url for v in vagas] == ["https://arbeitnow.com/a"] and vagas[0].descricao == "Hi"


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Acme: Backend Engineer</title><region>Anywhere in the World</region><category>Programming</category>
<description>&lt;p&gt;Go and SQL&lt;/p&gt;</description><pubDate>{data}</pubDate>
<link>https://weworkremotely.com/remote-jobs/acme-backend</link></item>
<item><title>Foo: Dev</title><region>USA Only</region><description></description><pubDate>{data}</pubDate>
<link>https://weworkremotely.com/remote-jobs/foo-dev</link></item></channel></rss>"""


def test_wwr_rss_separa_empresa_e_cargo():
    data = (agora() - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S +0000")

    def handler(req):
        return httpx.Response(200, text=RSS.format(data=data))

    vagas = ColetorWWR(cliente(handler), 24).coletar()
    assert len(vagas) == 1  # 3 feeds com os mesmos itens: sem duplicar; "USA Only" fora
    v = vagas[0]
    assert v.empresa == "Acme" and v.titulo == "Backend Engineer" and v.descricao == "Go and SQL"
    assert abs((agora() - v.data_publicacao).total_seconds() - 7200) < 60


# ------------------------------------------------------------------ Gupy
def _gupy(i: int, horas: float, tipo="vacancy_type_effective") -> dict:
    return {"id": i, "name": f"Desenvolvedor {i}", "description": "Requisitos: Python", "careerPageName": "Acme",
            "publishedDate": iso(horas) + ".000Z", "workplaceType": "remote", "city": "", "state": "",
            "jobUrl": f"https://acme.gupy.io/job/{i}", "type": tipo}


def test_gupy_pagina_ordenada_e_para_na_primeira_velha():
    pedidos = []

    def handler(req):
        pedidos.append(dict(req.url.params))
        return httpx.Response(200, json={"data": [_gupy(1, 1), _gupy(2, 2, "vacancy_type_talent_pool"),
                                                  _gupy(3, 5), _gupy(4, 90), _gupy(5, 95)]})

    col = ColetorGupy(["desenvolvedor", " "], max_paginas=3, por_pagina=5, cliente=cliente(handler),
                      idade_maxima_horas=24, pausa_segundos=0)
    vagas = col.coletar()
    assert [v.url.rsplit("/", 1)[1] for v in vagas] == ["1", "3"]  # banco de talentos e velhas ficam de fora
    assert len(pedidos) == 1  # achou vaga velha na 1ª página: não pagina mais; termo em branco ignorado
    p = pedidos[0]
    assert p["workplaceType"] == "remote" and p["sortBy"] == "publishedDate" and p["sortOrder"] == "desc"
    assert vagas[0].modelo_trabalho == "remoto" and vagas[0].idioma == "pt" and vagas[0].local == "Remoto"
    assert col.stats["tipo_ignorado"] == 1 and col.stats["antigas"] == 2


def test_gupy_falha_com_mensagem_clara_se_o_formato_mudar():
    col = ColetorGupy(["x"], cliente=cliente(lambda r: httpx.Response(200, json={"oops": 1})), pausa_segundos=0)
    with pytest.raises(RuntimeError, match="Gupy"):
        col.coletar()


# ------------------------------------------------------------------ quadros
def test_greenhouse_local_decide_conforme_o_filtro_internacional():
    def handler(req):
        assert req.url.params["content"] == "true"
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Backend Developer", "absolute_url": "https://gh/1", "location": {"name": "São Paulo"},
             "first_published": iso(2) + "-03:00", "updated_at": iso(1) + "-03:00", "company_name": "Acme",
             "content": "&lt;p&gt;Java&lt;/p&gt;", "language": "pt", "departments": [{"name": "Tech"}]},
            {"id": 2, "title": "Backend Developer", "absolute_url": "https://gh/2", "location": {"name": "Remote"},
             "first_published": iso(2) + "-03:00", "content": ""},
            {"id": 3, "title": "Backend Developer", "absolute_url": "https://gh/3", "location": {"name": "New York"},
             "first_published": iso(2) + "-03:00", "content": ""},
        ]})

    so_br = ColetorGreenhouse("acme", internacional=False, cliente=cliente(handler), idade_maxima_horas=24).coletar()
    assert [v.url for v in so_br] == ["https://gh/1"]
    assert so_br[0].descricao == "Java" and so_br[0].empresa == "Acme" and so_br[0].labels == ["Tech"]
    com_inter = ColetorGreenhouse("acme", internacional=True, cliente=cliente(handler), idade_maxima_horas=24).coletar()
    assert [v.url for v in com_inter] == ["https://gh/1", "https://gh/2"]  # "New York" continua de fora


def test_quadro_inexistente_nao_derruba_a_coleta():
    col = ColetorGreenhouse("sumiu", cliente=cliente(lambda r: httpx.Response(404, json={})))
    assert col.coletar() == []


def test_lever_usa_workplace_type_e_monta_descricao():
    def handler(req):
        assert req.url.params["mode"] == "json"
        return httpx.Response(200, json=[{
            "id": "a", "text": "Developer", "hostedUrl": "https://jobs.lever.co/acme/a", "createdAt": epoch(1) * 1000,
            "categories": {"location": "Brazil", "team": "Eng", "commitment": "Full-time"}, "country": "BR",
            "workplaceType": "remote", "descriptionPlain": "Sobre a vaga",
            "lists": [{"text": "Requisitos", "content": "<li>Python</li>"}], "additionalPlain": "Benefícios"}])

    v, = ColetorLever("acme", cliente=cliente(handler), idade_maxima_horas=24).coletar()
    assert v.modelo_trabalho == "remoto" and v.empresa == "Acme"
    assert "Sobre a vaga" in v.descricao and "- Python" in v.descricao and "Benefícios" in v.descricao


def test_ashby_junta_locais_e_respeita_remoto():
    def handler(req):
        return httpx.Response(200, json={"jobs": [
            {"title": "Dev", "location": "Remote - EU", "secondaryLocations": [{"location": "Brazil"}],
             "publishedAt": iso(1) + "+00:00", "isListed": True, "isRemote": True, "workplaceType": "Remote",
             "jobUrl": "https://jobs.ashbyhq.com/acme/1", "descriptionPlain": "Texto",
             "compensation": {"compensationTierSummary": "$100K"}},
            {"title": "Dev oculta", "location": "Brazil", "publishedAt": iso(1) + "+00:00", "isListed": False,
             "jobUrl": "https://jobs.ashbyhq.com/acme/2"}]})

    v, = ColetorAshby("acme", cliente=cliente(handler), idade_maxima_horas=24).coletar()
    assert v.modelo_trabalho == "remoto" and "Brazil" in v.local and v.salario == "$100K"


# ------------------------------------------------------------------ HTML
HTML_PT = """
<div class="cell-list ">
  <a href="/jobs/101-dev-back-end-pleno"><div class="row"><div class="col-sm-9">
  <div class="cell-list-content"><h3 class="text-24">Desenvolvedor Back-End Pleno</h3>
  <div class='cell-list-content-icon'><span><i class='fa fa-briefcase'></i>Acme &amp; Cia</span>
  <span><i class='fas fa-map-marker-alt'></i>Remoto</span><span><i class='far fa-chart-bar'></i>Pleno</span>
  <span><i class='far fa-file-alt'></i>CLT</span></div>
  <div><span class='tag-list background-gray'>Python</span><span class='tag-list background-gray'>SQL</span></div>
  </div></div></div></a>
</div>
"""


def test_programathor_lista_sem_data_nem_descricao_completa():
    v, = parse_programathor(HTML_PT)
    assert v.url == "https://programathor.com.br/jobs/101-dev-back-end-pleno" and v.empresa == "Acme & Cia"
    assert v.titulo == "Desenvolvedor Back-End Pleno" and v.data_publicacao is None
    assert v.descricao_completa is False and "Python, SQL" in v.descricao and v.labels[0] == "Pleno"
    col = ColetorProgramaThor(cliente(lambda r: httpx.Response(200, text=HTML_PT)), 24, pausa_segundos=0)
    assert len(col.coletar()) == 1


def _item_vc(id_, titulo, data, local="100% Home Office"):
    return f"""<li class="vaga odd ">
 <div class="informacoes-header"><h2 class="cargo">
 <a class="link-detalhes-vaga" data-id-vaga="{id_}" title="{titulo}" id="v{id_}" onclick="ga('send')" href="/vagas/v{id_}/{id_}-dev">{titulo}</a></h2>
 <span class="emprVaga"> Talent Group </span><div class="nivelQtdVagas"><span class="nivelVaga"> Pleno </span></div></div>
 <div class="detalhes"><p>Descrição: trecho curto</p></div>
 <footer><span class="vaga-local"><i></i> {local}</span><span class="data-publicacao"><i></i>{data}</span></footer></li>"""


def _pagina_vc(*itens):
    return f"<ul>{''.join(itens)}</ul>"


def _detalhe_vc(desc="Descrição completa da vaga", tipo="TELECOMMUTE"):
    return ('<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "description": desc,
            "jobLocationType": tipo}) + "</script>")


def test_vagas_com_parse_da_lista_e_json_ld():
    base = datetime(2026, 10, 16, 12)
    v, = parse_vagas_com(_pagina_vc(_item_vc(1, "Desenvolvedor Java", "Há 3 horas")), base)
    assert v.url == "https://www.vagas.com.br/vagas/v1/1-dev" and v.empresa == "Talent Group"
    assert v.labels == ["Pleno"] and v.data_publicacao == base - timedelta(hours=3) and v.descricao_completa is False
    assert descricao_json_ld(_detalhe_vc("<p>Oi</p>")) == ("Oi", "TELECOMMUTE") and descricao_json_ld("<html>") == ("", "")


def test_vagas_com_busca_descricao_so_de_vaga_nova_e_dentro_da_idade():
    detalhes = []

    def handler(req):
        if "/vagas-de-" in req.url.path:
            if req.url.path.endswith("home-office"):
                return httpx.Response(200, text=_pagina_vc())
            return httpx.Response(200, text=_pagina_vc(
                _item_vc(1, "Dev novo", "Hoje"), _item_vc(2, "Dev conhecido", "Hoje"),
                _item_vc(3, "Dev velho", "Há 5 dias"), _item_vc(4, "Dev presencial", "Hoje", "São Paulo - SP")))
        detalhes.append(req.url.path)
        return httpx.Response(200, text=_detalhe_vc())

    col = ColetorVagasCom(["desenvolvedor"], cliente(handler), 24, pausa_segundos=0,
                          url_conhecida=lambda u: u.endswith("/vagas/v2/2-dev"))
    vagas = {v.url.rsplit("/", 2)[1]: v for v in col.coletar()}
    assert set(vagas) == {"v1", "v2", "v4"}  # a velha (5 dias) ficou de fora
    assert detalhes == ["/vagas/v1/1-dev"]  # sem detalhe para a conhecida e para a presencial
    assert vagas["v1"].descricao == "Descrição completa da vaga" and vagas["v1"].descricao_completa
    assert vagas["v2"].descricao_completa is False and vagas["v2"].modelo_trabalho == "remoto"
    assert vagas["v4"].modelo_trabalho == "desconhecido"


# ------------------------------------------------------------------ pipeline: internacional liga/desliga
def _nomes(sv):
    from app.pipeline import montar_coletores
    return [nome for nome, _intervalo, _col in montar_coletores(sv)]


def _habilitar_tudo(sv):
    for nome in ("gupy", "quadros", "programathor", "vagas_com", "remotive", "remoteok", "jobicy", "himalayas",
                 "arbeitnow", "weworkremotely"):
        sv.config.definir(f"fontes.{nome}.ativo", True)


def test_fontes_internacionais_so_rodam_com_a_opcao_ligada(fabrica_servicos):
    sv = fabrica_servicos(lambda r: httpx.Response(200, json={}))
    _habilitar_tudo(sv)
    internacionais = {"remotive", "remoteok", "jobicy", "arbeitnow", "weworkremotely"}

    desligado = _nomes(sv)
    assert not internacionais & set(desligado)
    assert {"gupy", "programathor", "vagas.com", "himalayas", "greenhouse:quintoandar", "lever:ciandt"} <= set(desligado)

    sv.config.definir("fontes.incluir_internacional", True)
    ligado = _nomes(sv)
    assert internacionais <= set(ligado) and "himalayas" in ligado
    assert ligado.count("himalayas") == 1 and len(ligado) == len(set(ligado))

    sv.config.definir("fontes.remotive.ativo", False)
    sv.config.definir("fontes.gupy.ativo", False)
    assert "remotive" not in _nomes(sv) and "gupy" not in _nomes(sv)


def test_internacional_chega_aos_quadros_de_empresa(fabrica_servicos):
    from app.pipeline import montar_coletores
    sv = fabrica_servicos(lambda r: httpx.Response(200, json={}))
    sv.config.definir("fontes.quadros.ativo", True)
    quadro = lambda: next(c for n, _i, c in montar_coletores(sv) if n == "greenhouse:gitlab")  # noqa: E731
    assert quadro().aceita_internacional is False
    sv.config.definir("fontes.incluir_internacional", True)
    assert quadro().aceita_internacional is True


def test_quadro_invalido_na_lista_e_ignorado_com_aviso(fabrica_servicos):
    sv = fabrica_servicos(lambda r: httpx.Response(200, json={}))
    sv.config.definir("fontes.quadros.ativo", True)
    sv.config.definir("fontes.quadros.lista", ["greenhouse:ok", "lixo", "workday:x"])
    assert [n for n in _nomes(sv) if ":" in n and not n.startswith("github")] == ["greenhouse:ok"]


def test_coleta_nao_grava_vaga_fora_da_area_em_fonte_ampla(fabrica_servicos, monkeypatch):
    from app.db.models import Vaga
    from sqlalchemy import select
    from app.pipeline import coletar_tudo

    def handler(req):
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=[])
        assert req.url.host == "portal.gupy.io"
        return httpx.Response(200, json={"data": [
            _gupy(1, 1) | {"name": "Desenvolvedor Backend Pleno"},
            _gupy(2, 1) | {"name": "Analista Financeiro"},
            _gupy(3, 1) | {"name": "Desenvolvedor Sênior"},
        ]})

    sv = fabrica_servicos(handler)
    sv.config.definir("fontes.gupy.ativo", True)
    sv.config.definir("fontes.gupy.termos", ["desenvolvedor"])
    monkeypatch.setattr("app.coletores.gupy.time.sleep", lambda _s: None)  # sem pausa de cortesia nos testes
    coletadas, novas, erros = coletar_tudo(sv)
    assert (coletadas, novas, erros) == (3, 1, [])
    with sv.fabrica() as s:
        assert [v.titulo for v in s.scalars(select(Vaga)).all()] == ["Desenvolvedor Backend Pleno"]
