from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from jarvis.budget import BudgetGuard, start_of_local_day_utc


def _spend(store: Any, amount: float) -> None:
    run_id = store.start_run("c")
    store.finish_run(run_id, "success", amount, 1)


def test_refuses_when_daily_limit_reached(store: Any, notifier: Any) -> None:
    guard = BudgetGuard(store, notifier, daily_usd=1.0, task_usd=0.5)
    assert guard.refusal_reason() is None
    _spend(store, 1.0)
    assert "límite" in (guard.refusal_reason() or "")


def test_warns_once_at_80_percent_and_once_at_limit(store: Any, notifier: Any) -> None:
    guard = BudgetGuard(store, notifier, daily_usd=1.0, task_usd=0.5)
    _spend(store, 0.85)
    asyncio.run(guard.after_run(0.85))
    asyncio.run(guard.after_run(0.0))
    assert len(notifier.notices) == 1 and "85%" in notifier.notices[0]
    _spend(store, 0.2)
    asyncio.run(guard.after_run(0.2))
    assert len(notifier.notices) == 2 and "Límite" in notifier.notices[1]


def test_start_of_local_day_is_utc_iso() -> None:
    value = start_of_local_day_utc(datetime(2026, 10, 27, 12, 0, tzinfo=UTC))
    assert value.endswith("+00:00")
    assert datetime.fromisoformat(value) <= datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
