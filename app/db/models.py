"""Modelos SQLAlchemy (seção 8 do plano) + tabelas auxiliares de configuração e estado das fontes."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def agora_utc() -> datetime:
    """Datetime ingênuo em UTC (SQLite não guarda fuso)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Vaga(Base):
    __tablename__ = "vagas"

    id: Mapped[int] = mapped_column(primary_key=True)
    fonte: Mapped[str] = mapped_column(String(60), index=True)
    url: Mapped[str] = mapped_column(Text)
    empresa: Mapped[str] = mapped_column(String(200), default="")
    titulo: Mapped[str] = mapped_column(String(300))
    descricao: Mapped[str] = mapped_column(Text, default="")
    local: Mapped[str] = mapped_column(String(300), default="")
    modelo_trabalho: Mapped[str] = mapped_column(String(20), default="desconhecido")
    salario: Mapped[str] = mapped_column(String(100), default="")
    data_publicacao: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data_coleta: Mapped[datetime] = mapped_column(DateTime, default=agora_utc)
    hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    requisitos_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Campos extras além do plano:
    labels_json: Mapped[str] = mapped_column(Text, default="[]")  # labels/metadados da fonte
    descricao_completa: Mapped[bool] = mapped_column(Boolean, default=True)
    avisos_json: Mapped[str] = mapped_column(Text, default="[]")  # ex.: instruções escondidas
    eliminada_motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    idioma: Mapped[str] = mapped_column(String(5), default="pt")

    match: Mapped["Match | None"] = relationship(back_populates="vaga", uselist=False)
    curriculos: Mapped[list["CurriculoGerado"]] = relationship(
        back_populates="vaga", order_by="CurriculoGerado.versao"
    )
    aplicacoes: Mapped[list["Aplicacao"]] = relationship(back_populates="vaga")


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("vaga_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vaga_id: Mapped[int] = mapped_column(ForeignKey("vagas.id"), index=True)
    score: Mapped[float] = mapped_column(Float)
    detalhamento_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="novo", index=True)  # novo/aprovado/descartado
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora_utc)
    preliminar: Mapped[bool] = mapped_column(Boolean, default=False)  # só título/empresa/local
    modo: Mapped[str] = mapped_column(String(20), default="palavras")  # palavras | llm
    notificado: Mapped[bool] = mapped_column(Boolean, default=False)

    vaga: Mapped[Vaga] = relationship(back_populates="match")


class CurriculoGerado(Base):
    __tablename__ = "curriculos_gerados"

    id: Mapped[int] = mapped_column(primary_key=True)
    vaga_id: Mapped[int] = mapped_column(ForeignKey("vagas.id"), index=True)
    caminho_pdf: Mapped[str] = mapped_column(Text)
    conteudo_json: Mapped[str] = mapped_column(Text)
    provedor_llm: Mapped[str] = mapped_column(String(60), default="")
    modelo_llm: Mapped[str] = mapped_column(String(120), default="")
    versao: Mapped[int] = mapped_column(Integer, default=1)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora_utc)

    vaga: Mapped[Vaga] = relationship(back_populates="curriculos")


class Aplicacao(Base):
    __tablename__ = "aplicacoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    vaga_id: Mapped[int] = mapped_column(ForeignKey("vagas.id"), index=True)
    curriculo_id: Mapped[int | None] = mapped_column(ForeignKey("curriculos_gerados.id"), nullable=True)
    data: Mapped[datetime] = mapped_column(DateTime, default=agora_utc)
    canal: Mapped[str] = mapped_column(String(60), default="site")
    status: Mapped[str] = mapped_column(String(20), default="aplicado")  # aplicado/entrevista/recusado/oferta
    notas: Mapped[str] = mapped_column(Text, default="")

    vaga: Mapped[Vaga] = relationship(back_populates="aplicacoes")


class LlmChamada(Base):
    __tablename__ = "llm_chamadas"

    id: Mapped[int] = mapped_column(primary_key=True)
    provedor: Mapped[str] = mapped_column(String(60), index=True)
    modelo: Mapped[str] = mapped_column(String(120), default="")
    tarefa: Mapped[str] = mapped_column(String(60), default="")
    sucesso: Mapped[bool] = mapped_column(Boolean, default=False)
    erro: Mapped[str] = mapped_column(Text, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora_utc, index=True)
    # Log completo (migração 0002)
    execucao_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    vaga_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duracao_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tipo_erro: Mapped[str] = mapped_column(String(30), default="", server_default="")  # cota/auth/temporario/saida_invalida/outro
    tentativa: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class LlmProvedorEstado(Base):
    __tablename__ = "llm_provedores_estado"

    provedor: Mapped[str] = mapped_column(String(60), primary_key=True)
    cooldown_ate: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    desativado: Mapped[bool] = mapped_column(Boolean, default=False)
    motivo: Mapped[str] = mapped_column(Text, default="")


class Execucao(Base):
    __tablename__ = "execucoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    inicio: Mapped[datetime] = mapped_column(DateTime, default=agora_utc)
    fim: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    vagas_coletadas: Mapped[int] = mapped_column(Integer, default=0)
    vagas_novas: Mapped[int] = mapped_column(Integer, default=0)
    erros: Mapped[str] = mapped_column(Text, default="")
    origem: Mapped[str] = mapped_column(String(20), default="agendada")  # agendada/manual
    # Progresso ao vivo (migração 0002)
    status: Mapped[str] = mapped_column(String(20), default="rodando", server_default="concluida")  # rodando/concluida/erro
    etapa: Mapped[str] = mapped_column(String(200), default="", server_default="")
    progresso: Mapped[float] = mapped_column(Float, default=0.0, server_default="100")


class ExecucaoLog(Base):
    """Linha de log de uma execução (coleta, filtros, score, LLM, CV, notificação...)."""

    __tablename__ = "execucao_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    execucao_id: Mapped[int] = mapped_column(Integer, index=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora_utc)
    nivel: Mapped[str] = mapped_column(String(10), default="info")  # info/aviso/erro
    etapa: Mapped[str] = mapped_column(String(40), default="")
    mensagem: Mapped[str] = mapped_column(Text, default="")


class FonteEstado(Base):
    """Última execução de cada fonte (intervalo mínimo, seção 4.6) e cursor (ex.: `since` do GitHub)."""

    __tablename__ = "fontes_estado"

    fonte: Mapped[str] = mapped_column(String(120), primary_key=True)
    ultima_execucao: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cursor: Mapped[str] = mapped_column(Text, default="")


class Configuracao(Base):
    """Configurações salvas pelo painel. Segredos ficam criptografados (Fernet)."""

    __tablename__ = "configuracoes"

    chave: Mapped[str] = mapped_column(String(120), primary_key=True)
    valor: Mapped[str] = mapped_column(Text, default="")  # JSON (ou token Fernet se segredo)
    segredo: Mapped[bool] = mapped_column(Boolean, default=False)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=agora_utc, onupdate=agora_utc)
