"""Agendador interno (APScheduler) com cron configurável, sem sobreposição de execuções."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .db.models import Execucao
from .pipeline import Servicos, rodar

log = logging.getLogger(__name__)

JOB_ID = "rodada"
MISFIRE_GRACE = 6 * 3600


def validar_cron(expressao: str, fuso: str) -> CronTrigger:
    """Valida uma expressão cron de 5 campos. Levanta ValueError com mensagem legível."""
    partes = expressao.split()
    if len(partes) != 5:
        raise ValueError("Use 5 campos: minuto hora dia mês dia-da-semana (ex.: 0 */3 * * *).")
    try:
        return CronTrigger.from_crontab(expressao, timezone=ZoneInfo(fuso))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Expressão cron inválida: {e}") from e


class Agendador:
    def __init__(self, sv: Servicos):
        self.sv = sv
        self.fuso = sv.config.obter("agendamento.fuso", "America/Sao_Paulo")
        self.sched = BackgroundScheduler(timezone=ZoneInfo(self.fuso))

    @property
    def cron(self) -> str:
        return self.sv.config.obter("agendamento.cron", "0 */3 * * *")

    def _perdeu_execucao(self, trigger: CronTrigger) -> bool:
        """Houve um horário previsto depois da última execução que não rodou (ex.: VM desligada)?"""
        with self.sv.fabrica() as s:
            ultimo = s.scalar(select(Execucao.inicio).order_by(Execucao.inicio.desc()).limit(1))
        if ultimo is None:
            return True
        ref = ultimo.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(self.fuso))
        previsto = trigger.get_next_fire_time(None, ref)
        return previsto is not None and previsto < datetime.now(ZoneInfo(self.fuso))

    def iniciar(self) -> None:
        trigger = validar_cron(self.cron, self.fuso)
        self.sched.add_job(
            rodar, trigger, args=[self.sv], id=JOB_ID, name="Rodada de coleta e match",
            max_instances=1, coalesce=True, misfire_grace_time=MISFIRE_GRACE, replace_existing=True,
        )
        self.sched.start()
        if self._perdeu_execucao(trigger):  # roda uma vez só ao voltar
            log.info("Execução perdida detectada: rodando agora.")
            self.sched.modify_job(JOB_ID, next_run_time=datetime.now(ZoneInfo(self.fuso)))

    def parar(self) -> None:
        if self.sched.running:
            self.sched.shutdown(wait=False)

    def reagendar(self, expressao: str) -> datetime | None:
        """Valida, salva no banco, reagenda na hora e devolve a próxima execução prevista."""
        trigger = validar_cron(expressao, self.fuso)
        self.sv.config.definir("agendamento.cron", expressao)
        self.sched.reschedule_job(JOB_ID, trigger=trigger)
        return self.proxima_execucao()

    def proxima_execucao(self) -> datetime | None:
        job = self.sched.get_job(JOB_ID)
        return getattr(job, "next_run_time", None) if job else None
