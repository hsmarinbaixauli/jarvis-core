"""Spend limits: per task (enforced by the Agent SDK) and per local calendar day (enforced here)."""

from __future__ import annotations

from datetime import UTC, datetime

from callgate import Notifier

from jarvis.store import Store

WARN_FRACTION = 0.8


def start_of_local_day_utc(now: datetime | None = None) -> str:
    local = (now or datetime.now(UTC)).astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).isoformat(timespec="seconds")


class BudgetGuard:
    def __init__(self, store: Store, notifier: Notifier, daily_usd: float, task_usd: float) -> None:
        self._store = store
        self._notifier = notifier
        self.daily_usd = daily_usd
        self.task_usd = task_usd

    def spent_today(self) -> float:
        return self._store.spent_since(start_of_local_day_utc())

    def refusal_reason(self) -> str | None:
        spent = self.spent_today()
        if spent >= self.daily_usd:
            return (
                f"He llegado al límite de gasto de hoy ({spent:.2f} $ de {self.daily_usd:.2f} $). "
                "Súbelo en jarvis.yaml (budget.daily_usd) si lo necesitas."
            )
        return None

    async def after_run(self, cost_usd: float) -> None:
        spent = self.spent_today()
        before = spent - cost_usd
        day = start_of_local_day_utc()
        if before < self.daily_usd <= spent:
            await self._notify_once(f"budget_stop:{day}", f"⛔ Límite diario alcanzado: {spent:.2f} $.")
        elif before < self.daily_usd * WARN_FRACTION <= spent:
            await self._notify_once(
                f"budget_warn:{day}",
                f"⚠️ Llevo {spent:.2f} $ hoy, el {spent / self.daily_usd:.0%} del límite diario.",
            )

    async def _notify_once(self, key: str, text: str) -> None:
        if self._store.kv_get(key):
            return
        self._store.kv_set(key, "1")
        try:
            await self._notifier.notify(text)
        except Exception:
            pass
