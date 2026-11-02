from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis.scheduler import (
    NOTHING_TO_REPORT,
    TaskConfigError,
    TaskSpec,
    cron_trigger,
    has_nothing_to_report,
    if_relevant_prompt,
    load_tasks,
    missed_run,
)


def _next(expr: str, after: datetime) -> datetime:
    return cron_trigger(expr, UTC).get_next_fire_time(None, after)


def test_weekdays_use_standard_cron_numbering() -> None:
    tuesday_noon = datetime(2026, 10, 27, 12, tzinfo=UTC)
    assert _next("0 8 * * 1", tuesday_noon).strftime("%A") == "Monday"
    assert _next("0 8 * * 0", tuesday_noon).strftime("%A") == "Sunday"
    assert _next("0 8 * * 7", tuesday_noon).strftime("%A") == "Sunday"
    friday_noon = datetime(2026, 10, 30, 12, tzinfo=UTC)
    assert _next("0 8 * * 1-5", friday_noon).strftime("%A") == "Monday"  # skips the weekend


@pytest.mark.parametrize("expr", ["* * *", "61 * * * *", "0 8 * * 9", "x y z w v"])
def test_invalid_cron_rejected(expr: str) -> None:
    with pytest.raises(TaskConfigError):
        cron_trigger(expr, UTC)


def test_load_tasks(tmp_path: Path) -> None:
    path = tmp_path / "tasks.yaml"
    path.write_text(
        """
tasks:
  - id: resumen-matinal
    cron: "30 7 * * 1-5"
    prompt: Resume mi día.
    notify: if_relevant
    budget_usd: 0.3
  - id: apagada
    cron: "0 9 * * *"
    prompt: x
    enabled: false
""",
        encoding="utf-8",
    )
    tasks = load_tasks(path)
    assert [t.id for t in tasks] == ["resumen-matinal", "apagada"]
    assert tasks[0].budget_usd == 0.3 and tasks[0].notify == "if_relevant" and not tasks[1].enabled


def test_missing_file_means_no_tasks(tmp_path: Path) -> None:
    assert load_tasks(tmp_path / "nope.yaml") == []


@pytest.mark.parametrize(
    "body",
    [
        "tasks: {}",
        "tasks: [{id: 'Mal Id', cron: '0 8 * * *', prompt: x}]",
        "tasks: [{id: a, cron: '0 8 * * *', prompt: x}, {id: a, cron: '0 9 * * *', prompt: y}]",
        "tasks: [{id: a, cron: '0 8 * * *'}]",
        "tasks: [{id: a, cron: '0 8 * * *', prompt: x, notify: sometimes}]",
        "tasks: [{id: a, cron: '0 8 * *', prompt: x}]",
        "tasks: [{id: a, cron: '0 8 * * *', prompt: x, budget_usd: -1}]",
    ],
)
def test_invalid_tasks_rejected(tmp_path: Path, body: str) -> None:
    path = tmp_path / "tasks.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(TaskConfigError):
        load_tasks(path)


def test_example_tasks_file_is_valid() -> None:
    tasks = load_tasks(Path(__file__).resolve().parents[1] / "tasks.example.yaml")
    assert tasks


def test_missed_run() -> None:
    task = TaskSpec("t", "0 8 * * *", "x")
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    assert missed_run(task, "2026-10-28T09:00:00+00:00", now, UTC) == datetime(2026, 10, 29, 8, 0, tzinfo=UTC)
    assert missed_run(task, "2026-10-29T08:00:00+00:00", now, UTC) is None


def test_nothing_to_report() -> None:
    relevant = TaskSpec("t", "0 8 * * *", "Mira algo.", notify="if_relevant")
    assert NOTHING_TO_REPORT in if_relevant_prompt(relevant)
    assert has_nothing_to_report(relevant, " nada. ") and not has_nothing_to_report(relevant, "Hay 2 correos")
    assert has_nothing_to_report(TaskSpec("t", "0 8 * * *", "x", notify="never"), "algo")
    assert not has_nothing_to_report(TaskSpec("t", "0 8 * * *", "x"), NOTHING_TO_REPORT)


def test_scheduler_registers_enabled_jobs() -> None:
    from jarvis.scheduler import Scheduler

    async def scenario() -> list[str]:
        tasks = [TaskSpec("a", "0 8 * * 1-5", "x"), TaskSpec("b", "0 9 * * *", "y", enabled=False)]

        async def on_due(task: TaskSpec) -> None:
            pass

        scheduler = Scheduler(on_due, tasks, timezone=UTC)
        scheduler.start()
        try:
            return scheduler.describe()
        finally:
            scheduler.shutdown()

    lines = asyncio.run(scenario())
    assert "a (0 8 * * 1-5)" in lines[0] and "desactivada" in lines[1]
