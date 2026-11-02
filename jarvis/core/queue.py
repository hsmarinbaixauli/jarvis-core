"""Persistent task queue: every request (a chat message, a scheduled task) is a row in SQLite.

Tasks run in the background with a small concurrency limit and one at a time per conversation.
A failed attempt is retried with backoff only when it changed nothing: if the audit log shows a
write or irreversible call in that attempt, repeating it could send an email twice, so it fails instead.
The same rule decides what happens to tasks cut short by a restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from jarvis.store import Store, utc_now

_log = logging.getLogger(__name__)

TaskChange = Literal["queued", "running", "slow", "retry", "done", "failed", "cancelled"]
TERMINAL = frozenset({"done", "failed", "cancelled"})
# Brain statuses worth another try; budget or turn limits are not.
RETRYABLE = frozenset({"error", "error_during_execution"})
BACKOFF_S = (30, 120, 600)


class BrainLike(Protocol):
    async def ask(
        self, conversation_id: str, text: str, *, fresh: bool = False, budget_usd: float | None = None
    ) -> Any: ...


class TaskEvents(Protocol):
    async def task_changed(self, task: dict[str, Any], change: TaskChange) -> None: ...


def _later(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class TaskQueue:
    def __init__(
        self,
        store: Store,
        brain: BrainLike,
        events: TaskEvents,
        *,
        is_halted: Callable[[], bool] = lambda: False,
        max_parallel: int = 2,
        slow_notice_s: float = 120,
        poll_s: float = 1.0,
    ) -> None:
        self._store = store
        self._brain = brain
        self._events = events
        self._is_halted = is_halted
        self.max_parallel = max(1, max_parallel)
        self._slow_notice_s = slow_notice_s
        self._poll_s = poll_s
        self._running: dict[int, asyncio.Task[None]] = {}
        self._busy_conversations: set[str] = set()
        self._waiters: dict[int, list[asyncio.Future[dict[str, Any]]]] = {}
        self._wake = asyncio.Event()

    # -- submitting and waiting ------------------------------------------------------------

    async def submit(
        self,
        *,
        kind: str,
        channel: str,
        conversation_id: str,
        prompt: str,
        label: str = "",
        options: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        task_id = self._store.create_task(
            kind=kind,
            channel=channel,
            conversation_id=conversation_id,
            prompt=prompt,
            label=label,
            options=options,
            max_attempts=max_attempts,
        )
        task = self._task(task_id)
        await self._changed(task, "queued")
        self._wake.set()
        return task

    async def wait(self, task_id: int, timeout: float | None = None) -> dict[str, Any]:
        task = self._task(task_id)
        if task["status"] in TERMINAL:
            return task
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(task_id, []).append(future)
        return await asyncio.wait_for(future, timeout)

    def position(self, task_id: int) -> int:
        """1-based place among queued tasks (0 if it is not queued)."""
        queued = sorted(t["id"] for t in self._store.list_tasks(("queued",), limit=500))
        return queued.index(task_id) + 1 if task_id in queued else 0

    @property
    def running_ids(self) -> tuple[int, ...]:
        return tuple(self._running)

    def would_wait(self, conversation_id: str) -> bool:
        """Whether a new task for this conversation would sit in the queue instead of starting now."""
        queued = self._store.list_tasks(("queued",), limit=500)
        if conversation_id in self._busy_conversations or any(t["conversation_id"] == conversation_id for t in queued):
            return True
        return len(self._running) + len(queued) >= self.max_parallel

    def wake(self) -> None:
        self._wake.set()

    # -- cancelling ---------------------------------------------------------------------------

    async def cancel(self, task_id: int) -> bool:
        running = self._running.get(task_id)
        if running is not None:
            running.cancel()
            return True
        task = self._store.get_task(task_id)
        if task is None or task["status"] != "queued":
            return False
        self._store.update_task(task_id, status="cancelled", finished_at=utc_now())
        await self._finish(self._task(task_id), "cancelled")
        return True

    async def cancel_all(self) -> int:
        count = 0
        for task in self._store.list_tasks(("queued",), limit=500):
            count += await self.cancel(task["id"])
        for task_id in list(self._running):
            count += await self.cancel(task_id)
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        return count

    # -- running ------------------------------------------------------------------------------

    async def run_forever(self) -> None:
        while True:
            try:
                if not self._is_halted():
                    self._dispatch_ready()
            except Exception:
                _log.exception("Task dispatcher failed; trying again")
            try:
                await asyncio.wait_for(self._wake.wait(), self._poll_s)
            except TimeoutError:
                pass
            self._wake.clear()

    def _dispatch_ready(self) -> None:
        now = utc_now()
        for task in reversed(self._store.list_tasks(("queued",), limit=200)):
            if len(self._running) >= self.max_parallel:
                return
            if task["conversation_id"] in self._busy_conversations:
                continue
            if task["next_attempt_at"] and task["next_attempt_at"] > now:
                continue
            self._start(task)

    def _start(self, task: dict[str, Any]) -> None:
        self._busy_conversations.add(task["conversation_id"])
        runner = asyncio.ensure_future(self.execute(task["id"]))
        self._running[task["id"]] = runner

        def done(_: asyncio.Task[None], task_id: int = task["id"], cid: str = task["conversation_id"]) -> None:
            self._running.pop(task_id, None)
            self._busy_conversations.discard(cid)
            self._wake.set()

        runner.add_done_callback(done)

    async def execute(self, task_id: int) -> None:
        """One attempt of a task, inline. The dispatcher uses it; so does the CLI to run something now."""
        task = self._task(task_id)
        started = utc_now()
        attempts = int(task["attempts"]) + 1
        self._store.update_task(task_id, status="running", attempts=attempts, started_at=started, next_attempt_at=None)
        await self._changed(self._task(task_id), "running")
        slow = asyncio.get_running_loop().call_later(
            self._slow_notice_s, lambda: asyncio.ensure_future(self._changed(self._task(task_id), "slow"))
        )
        options = task["options"]
        try:
            reply = await self._brain.ask(
                task["conversation_id"],
                task["prompt"],
                fresh=bool(options.get("fresh")),
                budget_usd=options.get("budget_usd"),
            )
            text, status = str(getattr(reply, "text", reply)), str(getattr(reply, "status", "success"))
            cost = float(getattr(reply, "cost_usd", 0.0) or 0.0)
        except asyncio.CancelledError:
            self._store.update_task(task_id, status="cancelled", finished_at=utc_now())
            await self._finish(self._task(task_id), "cancelled")
            raise
        except Exception as exc:
            _log.exception("Task %s failed", task_id)
            text, status, cost = f"Error interno: {type(exc).__name__}", "error", 0.0
        finally:
            slow.cancel()

        total_cost = float(task["cost_usd"]) + cost
        if status not in RETRYABLE:
            self._store.update_task(task_id, status="done", result=text, cost_usd=total_cost, finished_at=utc_now())
            await self._finish(self._task(task_id), "done")
            return
        changed_something = self._store.side_effects_since(task["conversation_id"], started) > 0
        if not changed_something and attempts < int(task["max_attempts"]):
            delay = BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)]
            self._store.update_task(
                task_id, status="queued", error=text, cost_usd=total_cost, next_attempt_at=_later(delay)
            )
            await self._changed(self._task(task_id) | {"retry_in_s": delay}, "retry")
            return
        error = text + (" (ya había hecho cambios, así que no la repito)" if changed_something else "")
        self._store.update_task(task_id, status="failed", error=error, cost_usd=total_cost, finished_at=utc_now())
        await self._finish(self._task(task_id), "failed")

    def recover(self) -> list[dict[str, Any]]:
        """At startup: tasks a restart cut short are queued again, unless they had already changed something."""
        recovered = []
        for task in self._store.list_tasks(("running",), limit=500):
            if self._store.side_effects_since(task["conversation_id"], task["started_at"] or task["created_at"]):
                self._store.update_task(
                    task["id"],
                    status="failed",
                    error="Jarvis se reinició en mitad de la tarea y ya había hecho cambios; no la repito.",
                    finished_at=utc_now(),
                )
            else:
                self._store.update_task(task["id"], status="queued", next_attempt_at=None)
            recovered.append(self._task(task["id"]))
        return recovered

    # -- helpers ------------------------------------------------------------------------------

    def _task(self, task_id: int) -> dict[str, Any]:
        task = self._store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    async def _changed(self, task: dict[str, Any], change: TaskChange) -> None:
        try:
            await self._events.task_changed(task, change)
        except Exception:
            _log.exception("Task event handler failed for task %s", task["id"])

    async def _finish(self, task: dict[str, Any], change: TaskChange) -> None:
        await self._changed(task, change)
        for future in self._waiters.pop(task["id"], []):
            if not future.done():
                future.set_result(task)
