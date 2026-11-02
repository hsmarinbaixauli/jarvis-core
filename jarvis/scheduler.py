"""Recurring tasks from tasks.yaml, run by APScheduler inside Jarvis's event loop."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

_log = logging.getLogger(__name__)

NOTHING_TO_REPORT = "NADA"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")
_IF_RELEVANT_SUFFIX = (
    f"\n\nSi no hay nada que merezca la pena contar, responde exactamente {NOTHING_TO_REPORT} y nada más."
)


class TaskConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TaskSpec:
    id: str
    cron: str
    prompt: str
    notify: str = "always"
    budget_usd: float | None = None
    enabled: bool = True


DueCallback = Callable[[TaskSpec], Awaitable[Any]]


def _convert_dow(field: str) -> str:
    """Standard cron counts days from Sunday=0; APScheduler 3 counts from Monday=0. Use names instead."""

    def name(token: str) -> str:
        if token.isdigit():
            number = int(token)
            if not 0 <= number <= 7:
                raise TaskConfigError(f"Día de la semana fuera de rango: {token}")
            return _DOW_NAMES[number]
        return token

    return re.sub(r"\d+", lambda m: name(m.group(0)), field)


def cron_trigger(expr: str, timezone: Any) -> Any:
    from apscheduler.triggers.cron import CronTrigger

    fields = expr.split()
    if len(fields) != 5:
        raise TaskConfigError(f"'{expr}': el cron necesita 5 campos (minuto hora día mes día_semana).")
    minute, hour, day, month, dow = fields
    try:
        return CronTrigger(
            minute=minute, hour=hour, day=day, month=month, day_of_week=_convert_dow(dow), timezone=timezone
        )
    except ValueError as exc:
        raise TaskConfigError(f"Cron no válido '{expr}': {exc}") from exc


def load_tasks(path: Path) -> list[TaskSpec]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_tasks = data.get("tasks") if isinstance(data, dict) else None
    if raw_tasks is None:
        return []
    if not isinstance(raw_tasks, list):
        raise TaskConfigError("'tasks' debe ser una lista.")
    tasks: list[TaskSpec] = []
    seen: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise TaskConfigError("Cada tarea debe ser un diccionario con id, cron y prompt.")
        task_id = str(raw.get("id", "")).strip()
        if not _ID_RE.match(task_id) or task_id in seen:
            raise TaskConfigError(f"id de tarea no válido o repetido: '{task_id}' (minúsculas, números y guiones).")
        seen.add(task_id)
        prompt = str(raw.get("prompt", "")).strip()
        if not prompt:
            raise TaskConfigError(f"La tarea '{task_id}' no tiene prompt.")
        notify = str(raw.get("notify", "always"))
        if notify not in ("always", "if_relevant", "never"):
            raise TaskConfigError(f"'{task_id}': notify debe ser always, if_relevant o never.")
        budget = raw.get("budget_usd")
        if budget is not None and (not isinstance(budget, int | float) or isinstance(budget, bool) or budget <= 0):
            raise TaskConfigError(f"'{task_id}': budget_usd debe ser un número positivo.")
        cron = str(raw.get("cron", "")).strip()
        cron_trigger(cron, None)
        tasks.append(
            TaskSpec(task_id, cron, prompt, notify, float(budget) if budget else None, bool(raw.get("enabled", True)))
        )
    return tasks


def if_relevant_prompt(task: TaskSpec) -> str:
    return task.prompt + (_IF_RELEVANT_SUFFIX if task.notify == "if_relevant" else "")


def has_nothing_to_report(task: TaskSpec, text: str) -> bool:
    return task.notify == "never" or (
        task.notify == "if_relevant" and text.strip().strip(" .").upper() == NOTHING_TO_REPORT
    )


def missed_run(task: TaskSpec, last_run_iso: str, now: datetime, timezone: Any) -> datetime | None:
    """The first time *task* should have fired after its last run and did not (Jarvis was off)."""
    # The trigger counts a fire time equal to its start, and a run is stamped at its own fire time.
    last = datetime.fromisoformat(last_run_iso) + timedelta(seconds=1)
    due = cron_trigger(task.cron, timezone).get_next_fire_time(None, last)
    return due if due is not None and due <= now else None


class Scheduler:
    def __init__(self, on_due: DueCallback, tasks: list[TaskSpec], timezone: Any = None) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from tzlocal import get_localzone

        self._tz = timezone or get_localzone()
        self.timezone = self._tz
        self.tasks = {t.id: t for t in tasks}
        self._scheduler = AsyncIOScheduler(timezone=self._tz)
        for task in tasks:
            if task.enabled:
                self._scheduler.add_job(
                    on_due,
                    cron_trigger(task.cron, self._tz),
                    args=[task],
                    id=task.id,
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=600,
                )

    def add_interval(self, job_id: str, fn: Any, hours: float) -> None:
        from apscheduler.triggers.interval import IntervalTrigger

        self._scheduler.add_job(
            fn,
            IntervalTrigger(hours=hours, timezone=self._tz),
            id=job_id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )

    def add_cron(self, job_id: str, fn: Any, expr: str) -> None:
        self._scheduler.add_job(
            fn, cron_trigger(expr, self._tz), id=job_id, max_instances=1, coalesce=True, misfire_grace_time=1800
        )

    def start(self) -> None:
        self._scheduler.start()
        for job in self._scheduler.get_jobs():
            _log.info("Tarea %s: próxima ejecución %s", job.id, job.next_run_time)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def describe(self) -> list[str]:
        lines = []
        for task in self.tasks.values():
            job = self._scheduler.get_job(task.id)
            when = job.next_run_time.strftime("%a %d/%m %H:%M") if job and job.next_run_time else "desactivada"
            lines.append(f"• {task.id} ({task.cron}) → {when}")
        return lines
