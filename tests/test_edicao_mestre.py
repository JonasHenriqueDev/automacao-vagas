"""Currículo mestre editável: ativo/inativo, adicionar, editar, validar e fazer backup."""
import shutil
from pathlib import Path

import pytest
import yaml

from app.curriculo import edicao
from app.curriculo.mestre import carregar_mestre
from app.curriculo.personalizar import validar_saida
from app.match import score
from app.match.requisitos import RequisitosVaga
from tests.conftest import RAIZ


@pytest.fixture
def arquivo(tmp_path) -> Path:
    destino = tmp_path / "curriculo_mestre.yaml"
    shutil.copy(RAIZ / "data" / "curriculo_mestre.example.yaml", destino)
    return destino


def recarregar(arquivo: Path):
    return carregar_mestre(arquivo)


def test_exemplo_tem_so_a_certificacao_de_ingles_ativa(arquivo):
    m = recarregar(arquivo).somente_ativos()
    assert [c.pt for c in m.certificacoes] == ["EF SET English Certificate 56/100 (B2 Upper Intermediate)"]
    assert len(recarregar(arquivo).certificacoes) == 3  # as outras continuam salvas, só desativadas


def test_desativar_skill_tira_do_match_e_do_cv(arquivo):
    antes = score.Perfil(recarregar(arquivo).somente_ativos())
    assert antes.peso_do("Redis") == 1.0
    edicao.aplicar(arquivo, "alternar", {"tipo": "skill", "ref": "Redis"})
    m = recarregar(arquivo).somente_ativos()
    assert "Redis" not in [s.nome for s in m.skills]
    assert score.Perfil(m).peso_do("Redis") == 0.0
    conteudo = validar_saida({"skills": ["Redis", "Node.js"]}, m, "pt")
    assert "Redis" not in [i for g in conteudo.skills for i in g.itens]
    edicao.aplicar(arquivo, "alternar", {"tipo": "skill", "ref": "redis"})  # reativa (nome sem diferenciar caixa)
    assert score.Perfil(recarregar(arquivo).somente_ativos()).peso_do("Redis") == 1.0


def test_desativar_bullet_e_experiencia(arquivo):
    edicao.aplicar(arquivo, "alternar", {"tipo": "bullet", "ref": "emp_a", "ref2": "emp_a.pagamentos"})
    m = recarregar(arquivo).somente_ativos()
    assert "emp_a.pagamentos" not in [b.id for b in m.experiencia("emp_a").bullets]
    edicao.aplicar(arquivo, "alternar", {"tipo": "experiencia", "ref": "emp_b"})
    assert recarregar(arquivo).somente_ativos().experiencia("emp_b") is None
    ids = [e.id for e in validar_saida({}, recarregar(arquivo).somente_ativos(), "pt").experiencias]
    assert "emp_b" not in ids and "emp_a" in ids


def test_experiencia_sem_bullet_ativo_sai_do_cv(arquivo):
    for b in recarregar(arquivo).experiencia("freelance").bullets:
        edicao.aplicar(arquivo, "alternar", {"tipo": "bullet", "ref": "freelance", "ref2": b.id})
    assert recarregar(arquivo).somente_ativos().experiencia("freelance") is None


def test_alternar_item_de_lista_exige_chave_atual(arquivo):
    dados = edicao.ler(arquivo)
    item = dados["certificacoes"][0]
    with pytest.raises(ValueError, match="mudou"):
        edicao.aplicar(arquivo, "alternar", {"tipo": "certificacoes", "i": "0", "k": "deadbeef"})
    edicao.aplicar(arquivo, "alternar", {"tipo": "certificacoes", "i": "0", "k": edicao.chave(item)})
    assert edicao.ler(arquivo)["certificacoes"][0]["ativo"] is True  # estava desativada, agora ativa
    with pytest.raises(ValueError, match="não encontrado"):
        edicao.aplicar(arquivo, "alternar", {"tipo": "idiomas", "i": "99", "k": "x"})


def test_adicionar_skill_em_categoria_existente_e_nova(arquivo):
    edicao.aplicar(arquivo, "skill_adicionar", {"nome": "Go", "categoria": "Linguagens", "nivel": "2", "sinonimos": "golang"})
    edicao.aplicar(arquivo, "skill_adicionar", {"nome": "Terraform", "categoria": "Linguagens",
                                                "categoria_nova": "Infraestrutura", "nivel": "3", "academico": "on"})
    skills = {s.nome: s for s in recarregar(arquivo).skills}
    assert skills["Go"].categoria == "Linguagens" and skills["Go"].sinonimos == ["golang"]
    assert skills["Terraform"].categoria == "Infraestrutura" and skills["Terraform"].academico
    perfil = score.Perfil(recarregar(arquivo).somente_ativos())
    assert perfil.peso_do("Golang") == 1.0 and perfil.peso_do("Terraform") == 0.5  # novas skills já contam no match
    with pytest.raises(ValueError, match="já existe"):
        edicao.aplicar(arquivo, "skill_adicionar", {"nome": "go", "categoria": "Linguagens"})


def test_editar_skill(arquivo):
    edicao.aplicar(arquivo, "skill_editar", {"ref": "Kafka", "categoria": "Mensageria", "nivel": "4", "sinonimos": "Apache Kafka"})
    k = next(s for s in recarregar(arquivo).skills if s.nome == "Kafka")
    assert k.nivel == 4 and not k.academico and k.sinonimos == ["Apache Kafka"]


def test_adicionar_bullet_vira_conteudo_valido_para_o_cv(arquivo):
    edicao.aplicar(arquivo, "bullet_adicionar", {"ref": "emp_a", "texto": "Integração com Twilio para SMS em larga escala.",
                                                 "tags": "Twilio, SMS"})
    m = recarregar(arquivo)
    novo = m.experiencia("emp_a").bullets[-1]
    assert novo.id.startswith("emp_a.") and novo.tags == ["Twilio", "SMS"]
    # a LLM pode reescrever usando o bullet novo (a tecnologia agora existe nesta experiência)
    saida = {"experiencias": [{"id": "emp_a", "bullets": [{"origem": novo.id, "texto": "SMS com Twilio em larga escala."}]}]}
    c = validar_saida(saida, m.somente_ativos(), "pt")
    assert c.correcoes == []


def test_adicionar_experiencia_so_entra_no_cv_com_bullet(arquivo):
    edicao.aplicar(arquivo, "experiencia_adicionar", {
        "empresa": "Nova Empresa", "cargo_pt": "Dev Backend", "cargo_en": "Backend Dev", "local": "Remoto",
        "inicio": "2024-01", "fim": "", "tipo_exp": "profissional", "tecnologias": "Python, FastAPI"})
    assert recarregar(arquivo).somente_ativos().experiencia("nova_empresa") is None
    edicao.aplicar(arquivo, "bullet_adicionar", {"ref": "nova_empresa", "texto": "APIs com FastAPI."})
    assert recarregar(arquivo).somente_ativos().experiencia("nova_empresa") is not None


def test_ids_gerados_nao_colidem(arquivo):
    for _ in range(3):
        edicao.aplicar(arquivo, "experiencia_adicionar", {"empresa": "Mesma Empresa", "cargo_pt": "Dev", "inicio": "2020-01",
                                                          "tipo_exp": "projeto"})
    ids = [e.id for e in recarregar(arquivo).experiencias if e.id.startswith("mesma_empresa")]
    assert ids == ["mesma_empresa", "mesma_empresa_2", "mesma_empresa_3"]


def test_adicionar_formacao_certificacao_e_idioma(arquivo):
    edicao.aplicar(arquivo, "formacao_adicionar", {"instituicao": "Curso X", "curso_pt": "Pós em Dados", "inicio": "2026-09",
                                                   "fim": "2027-12"})
    edicao.aplicar(arquivo, "item_adicionar", {"lista": "certificacoes", "pt": "AWS Cloud Practitioner", "en": "AWS Cloud Practitioner"})
    edicao.aplicar(arquivo, "item_adicionar", {"lista": "idiomas", "pt": "Espanhol: básico"})
    m = recarregar(arquivo)
    assert m.formacao[-1].instituicao == "Curso X" and m.certificacoes[-1].pt == "AWS Cloud Practitioner"
    assert m.idiomas[-1].pt == "Espanhol: básico" and m.idiomas[-1].ativo


def test_editar_e_remover_item_de_lista(arquivo):
    dados = edicao.ler(arquivo)
    it = dados["idiomas"][0]
    edicao.aplicar(arquivo, "item_editar", {"lista": "idiomas", "i": "0", "k": edicao.chave(it), "pt": "Português: nativo!", "en": "Portuguese"})
    assert recarregar(arquivo).idiomas[0].pt == "Português: nativo!"
    it = edicao.ler(arquivo)["idiomas"][0]
    n = len(recarregar(arquivo).idiomas)
    edicao.aplicar(arquivo, "remover", {"tipo": "idiomas", "i": "0", "k": edicao.chave(it)})
    assert len(recarregar(arquivo).idiomas) == n - 1


def test_remover_skill_bullet_e_experiencia(arquivo):
    edicao.aplicar(arquivo, "remover", {"tipo": "skill", "ref": "Jira"})
    edicao.aplicar(arquivo, "remover", {"tipo": "bullet", "ref": "emp_a", "ref2": "emp_a.sql"})
    edicao.aplicar(arquivo, "remover", {"tipo": "experiencia", "ref": "monitoria"})
    m = recarregar(arquivo)
    assert "Jira" not in [s.nome for s in m.skills] and m.experiencia("monitoria") is None
    assert "emp_a.sql" not in [b.id for b in m.experiencia("emp_a").bullets]


def test_geral_salvar_valida_formato_do_mes(arquivo):
    edicao.aplicar(arquivo, "geral_salvar", {"cargo_pt": "Dev Backend", "cargo_en": "Backend Dev", "inicio_carreira": "2022-06"})
    assert recarregar(arquivo).inicio_carreira == "2022-06"
    with pytest.raises(ValueError, match="AAAA-MM"):
        edicao.aplicar(arquivo, "geral_salvar", {"cargo_pt": "x", "inicio_carreira": "junho/2022"})


@pytest.mark.parametrize("acao,campos,erro", [
    ("skill_adicionar", {"nome": "X", "categoria": "A", "nivel": "9"}, "1 a 5"),
    ("skill_adicionar", {"nome": "", "categoria": "A"}, "Preencha"),
    ("skill_adicionar", {"nome": "X" * 80, "categoria": "A"}, "passa de"),
    ("experiencia_adicionar", {"empresa": "E", "cargo_pt": "C", "inicio": "2020-13", "tipo_exp": "profissional"}, "AAAA-MM"),
    ("experiencia_adicionar", {"empresa": "E", "cargo_pt": "C", "inicio": "2020-01", "tipo_exp": "hacker"}, "Tipo"),
    ("bullet_adicionar", {"ref": "nao_existe", "texto": "oi"}, "não encontrada"),
    ("nao_existe", {}, "desconhecida"),
])
def test_entradas_invalidas_nao_alteram_o_arquivo(arquivo, acao, campos, erro):
    antes = arquivo.read_bytes()
    with pytest.raises(ValueError, match=erro):
        edicao.aplicar(arquivo, acao, campos)
    assert arquivo.read_bytes() == antes


def test_backup_datado_a_cada_alteracao_e_limite(arquivo, monkeypatch):
    monkeypatch.setattr(edicao, "MAX_BACKUPS", 3)
    for i in range(5):
        edicao.aplicar(arquivo, "alternar", {"tipo": "skill", "ref": "Git"})
    backups = sorted((arquivo.parent / "backups").glob("curriculo_mestre-*.yaml"))
    assert len(backups) == 3  # só os 3 mais recentes
    assert all(yaml.safe_load(b.read_text(encoding="utf-8")) for b in backups)


def test_arquivo_ausente_da_erro_com_dica(tmp_path):
    with pytest.raises(FileNotFoundError, match="example"):
        edicao.ler(tmp_path / "curriculo_mestre.yaml")


def test_gravacao_atomica_nao_deixa_temporarios(arquivo):
    edicao.aplicar(arquivo, "alternar", {"tipo": "skill", "ref": "Git"})
    assert not list(arquivo.parent.glob(".mestre-*.tmp"))
    yaml.safe_load(arquivo.read_text(encoding="utf-8"))  # continua sendo YAML válido


def test_validacao_final_barra_dados_corrompidos(arquivo):
    antes = arquivo.read_bytes()
    dados = edicao.ler(arquivo)
    dados["skills"][0]["nivel"] = "alto"  # tipo errado
    with pytest.raises(ValueError, match="nada foi salvo"):
        edicao.salvar(arquivo, dados)
    assert arquivo.read_bytes() == antes
