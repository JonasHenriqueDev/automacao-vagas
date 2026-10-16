import httpx

from app.coletores.email_imap import ColetorEmail, parser_linkedin
from app.coletores.github_issues import ColetorGithubIssues
from app.pipeline import salvar_se_nova
from app.coletores.base import VagaBruta


def _issue(numero, titulo, labels, corpo="", pr=False):
    d = {"number": numero, "title": titulo, "body": corpo,
         "html_url": f"https://github.com/backend-br/vagas/issues/{numero}",
         "created_at": "2026-10-15T12:00:00Z", "labels": [{"name": l} for l in labels]}
    if pr:
        d["pull_request"] = {}
    return d


def test_github_coleta_filtra_prs_e_labels_excluidas():
    pedidos = []

    def handler(req: httpx.Request) -> httpx.Response:
        pedidos.append(req)
        return httpx.Response(200, json=[
            _issue(1, "[Remoto] Dev Backend Node.js na Acme", ["Remoto", "Pleno"],
                   "**Empresa:** Acme Ltda\nRequisitos: Node.js"),
            _issue(2, "Dev Sênior", ["Remoto", "Sênior"]),
            _issue(3, "PR de README", ["Remoto"], pr=True),
        ])

    col = ColetorGithubIssues("backend-br/vagas", ["Remoto"], ["Sênior", "Stale"], token="tok",
                              cliente=httpx.Client(transport=httpx.MockTransport(handler)))
    vagas = col.coletar()
    assert [v.url.rsplit("/", 1)[1] for v in vagas] == ["1"]
    v = vagas[0]
    assert v.titulo == "Dev Backend Node.js na Acme" and v.empresa == "Acme Ltda"
    assert v.modelo_trabalho == "remoto" and "Pleno" in v.labels
    req = pedidos[0]
    assert req.url.params["labels"] == "Remoto" and req.url.params["sort"] == "created"
    assert "since" not in req.url.params  # `since` filtra por ATUALIZAÇÃO e traria vagas antigas comentadas hoje
    assert req.headers["authorization"] == "Bearer tok" and "automacao-vagas" in req.headers["user-agent"]


def test_github_para_no_limite_de_requisicoes():
    def handler(_):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "rate limit"})

    col = ColetorGithubIssues("a/b", [], [], cliente=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        col.coletar()
        assert False, "deveria falhar"
    except RuntimeError as e:
        assert "Limite" in str(e)


HTML_LINKEDIN = """
<table><tr><td>
<a href="https://www.linkedin.com/comm/jobs/view/4012345678?trackingId=abc&amp;refId=x">Desenvolvedor Backend Pleno</a>
</td></tr><tr><td>Acme Tecnologia · Brasil (Remoto)</td></tr><tr><td>Candidatura simplificada</td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4099999999?trackingId=zzz">Full Stack Developer</a></td></tr>
<tr><td>Globex</td></tr><tr><td>São Paulo, Brasil</td></tr><tr><td>há 2 dias</td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4012345678?dup=1">Desenvolvedor Backend Pleno</a></td></tr>
<tr><td><a href="https://www.linkedin.com/help">Ajuda</a></td></tr>
</table>
"""


def test_parser_linkedin_extrai_cartoes_sem_duplicar():
    vagas = parser_linkedin("Novas vagas: backend", HTML_LINKEDIN)
    assert len(vagas) == 2
    a, b = vagas
    assert a.url == "https://www.linkedin.com/jobs/view/4012345678/"
    assert (a.titulo, a.empresa, a.local) == ("Desenvolvedor Backend Pleno", "Acme Tecnologia", "Brasil (Remoto)")
    assert (b.titulo, b.empresa, b.local) == ("Full Stack Developer", "Globex", "São Paulo, Brasil")
    assert not a.descricao_completa and a.fonte == "email:linkedin"


class FakeIMAP:
    def __init__(self, mensagens):
        self.mensagens, self.vistos = mensagens, []

    def login(self, u, p): self.logado = (u, p)
    def select(self, pasta, readonly=False): return "OK", [b"1"]
    def search(self, _charset, *criterio): return "OK", [b" ".join(str(i).encode() for i in self.mensagens)]
    def fetch(self, num, _): return "OK", [(b"1 (BODY[] {10}", self.mensagens[int(num)]), b")"]
    def store(self, num, *_): self.vistos.append(int(num))
    def logout(self): pass


def _email_bruto(remetente: str, html: str) -> bytes:
    from email.message import EmailMessage
    m = EmailMessage()
    from email.utils import formatdate
    m["From"], m["Subject"], m["Date"] = remetente, "Alerta de vagas", formatdate()
    m.set_content("texto")
    m.add_alternative(html, subtype="html")
    return m.as_bytes()


def test_coletor_email_usa_parser_do_remetente_e_marca_lido():
    imap = FakeIMAP({1: _email_bruto("LinkedIn <jobalerts-noreply@linkedin.com>", HTML_LINKEDIN),
                     2: _email_bruto("Outro <promo@loja.com>", "<a href='x'>y</a>")})
    col = ColetorEmail("imap.x", 993, "u", "s", conexao_factory=lambda: imap)
    vagas = col.coletar()
    assert len(vagas) == 2 and imap.vistos == [1]


def test_deduplicacao_por_hash(fabrica_servicos):
    sv = fabrica_servicos()
    v = VagaBruta("t", "https://x.com/a?utm=1", "Dev Backend", "Acme", "desc")
    with sv.fabrica() as s:
        assert salvar_se_nova(s, v) is not None
        assert salvar_se_nova(s, VagaBruta("t", "https://x.com/a", "Dev Backend", "ACME", "desc")) is None
        s.commit()


def test_empresa_extraida_de_titulos_reais():
    from app.coletores.github_issues import _empresa
    casos = {
        "Desenvolvedor(a) Full Stack Pleno (Python / TypeScript / IA) na Ártemis": "Ártemis",
        "Dev java junior - DB1": "DB1",
        "Back-end Software Engineer  - USD (Micro1)": "Micro1",
        "[Remoto] Dev Backend Node.js na Acme": "Acme",
        "Engenheiro de Software- Micro 1": "",
    }
    for titulo, esperado in casos.items():
        assert _empresa(titulo, "") == esperado, titulo
    assert _empresa("Dev", "**Empresa:** Foo Ltda") == "Foo Ltda"
