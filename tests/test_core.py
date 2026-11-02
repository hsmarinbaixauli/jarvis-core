from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jarvis.core.events import Event, EventBus
from jarvis.core.service import BusNotifier, Halted
from jarvis.scheduler import NOTHING_TO_REPORT, TaskSpec
from tests.conftest import RecordingChannel


def run_with_queue(core: Any, scenario: Callable[[], Awaitable[Any]]) -> Any:
    async def main() -> Any:
        dispatcher = asyncio.create_task(core.queue.run_forever())
        try:
            return await asyncio.wait_for(scenario(), 10)
        finally:
            dispatcher.cancel()

    return asyncio.run(main())


async def until(condition: Callable[[], bool]) -> None:
    while not condition():
        await asyncio.sleep(0.01)


# -- event bus ------------------------------------------------------------------------------------


def test_bus_targets_and_approval_channels() -> None:
    bus = EventBus()
    telegram, web = RecordingChannel("telegram"), RecordingChannel("web", can_approve=False)
    bus.register(telegram)
    bus.register(web)

    async def scenario() -> list[int]:
        return [
            await bus.publish(Event("notice", "a")),
            await bus.publish(Event("reply", "b", target="web")),
            await bus.publish(Event("approval")),
        ]

    assert asyncio.run(scenario()) == [2, 1, 1]
    assert telegram.texts() == ["a", ""] and web.texts() == ["a", "b"]


def test_approval_without_an_approving_channel_fails() -> None:
    bus = EventBus()
    bus.register(RecordingChannel("web", can_approve=False))
    with pytest.raises(RuntimeError):
        asyncio.run(BusNotifier(bus).ask_approval(object()))  # type: ignore[arg-type]


# -- chat through the queue -------------------------------------------------------------------


def test_chat_reply_goes_back_to_its_channel(core: Any, brain_script: Any) -> None:
    telegram, web = RecordingChannel("telegram"), RecordingChannel("web", can_approve=False)
    core.register(telegram)
    core.register(web)

    async def scenario() -> dict[str, Any]:
        task = await core.ask("telegram", "hola")
        return await core.wait(task["id"])

    done = run_with_queue(core, scenario)
    assert done["status"] == "done" and done["result"] == "eco: hola" and done["cost_usd"] == pytest.approx(0.01)
    assert "eco: hola" in telegram.texts("reply") and "eco: hola" not in web.texts("reply")
    assert [e.extra["change"] for e in web.events if e.kind == "task"] == ["queued", "running", "done"]


def test_same_conversation_runs_one_at_a_time(core: Any, brain_script: Any) -> None:
    brain_script.gate = asyncio.Event()
    channel = RecordingChannel()
    core.register(channel)

    async def scenario() -> tuple[int, list[dict[str, Any]]]:
        first = await core.ask("telegram", "uno")
        second = await core.ask("telegram", "dos")
        await until(lambda: len(brain_script.calls) == 1)
        await asyncio.sleep(0.05)
        concurrent = len(brain_script.calls)
        brain_script.gate.set()
        return concurrent, [await core.wait(first["id"]), await core.wait(second["id"])]

    concurrent, results = run_with_queue(core, scenario)
    assert concurrent == 1 and [r["status"] for r in results] == ["done", "done"]
    assert any("en cola" in t for t in channel.texts("reply"))


def test_failed_attempt_without_side_effects_is_retried(core: Any, brain_script: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr("jarvis.core.queue.BACKOFF_S", (0, 0, 0))
    brain_script.script = [("API caída", "error"), ("por fin", "success")]
    channel = RecordingChannel()
    core.register(channel)

    async def scenario() -> dict[str, Any]:
        return await core.wait((await core.ask("telegram", "x"))["id"])

    done = run_with_queue(core, scenario)
    assert done["status"] == "done" and done["attempts"] == 2 and done["result"] == "por fin"
    assert any("reintento" in t for t in channel.texts())


def test_failed_attempt_that_changed_something_is_not_retried(core: Any, brain_script: Any) -> None:
    brain_script.script = [("se cortó a medias", "error")]
    brain_script.side_effect = "irreversible"
    channel = RecordingChannel()
    core.register(channel)

    async def scenario() -> dict[str, Any]:
        return await core.wait((await core.ask("telegram", "manda el correo"))["id"])

    done = run_with_queue(core, scenario)
    assert done["status"] == "failed" and done["attempts"] == 1 and "no la repito" in done["error"]
    assert len(brain_script.calls) == 1 and any("No he podido terminar" in t for t in channel.texts())


def test_budget_or_turn_limits_are_not_retried(core: Any, brain_script: Any) -> None:
    brain_script.script = [("⛔ límite", "error_max_budget_usd")]

    async def scenario() -> dict[str, Any]:
        return await core.wait((await core.ask("telegram", "x"))["id"])

    assert run_with_queue(core, scenario)["status"] == "done" and len(brain_script.calls) == 1


def test_recover_after_restart(core: Any, store: Any) -> None:
    from callgate import AuditEntry

    cid = store.create_conversation("telegram")
    clean = store.create_task(kind="chat", channel="telegram", conversation_id=cid, prompt="a", status="running")
    store.update_task(clean, started_at="2026-01-01T00:00:00+00:00")
    cid2 = store.create_conversation("telegram")
    dirty = store.create_task(kind="chat", channel="telegram", conversation_id=cid2, prompt="b", status="running")
    store.update_task(dirty, started_at="2026-01-01T00:00:00+00:00")
    store.audit(AuditEntry("mcp__x__send", "irreversible", "approval", "approved", "r", "{}", cid2))
    channel = RecordingChannel()
    core.register(channel)

    asyncio.run(core.startup([]))
    assert store.get_task(clean)["status"] == "queued"
    assert store.get_task(dirty)["status"] == "failed"
    assert any("Retomo" in t for t in channel.texts()) and any("no la repito" in t for t in channel.texts())


def test_cancel_running_and_queued(core: Any, brain_script: Any) -> None:
    brain_script.gate = asyncio.Event()
    channel = RecordingChannel()
    core.register(channel)

    async def scenario() -> list[str]:
        first = await core.ask("telegram", "largo")
        await until(lambda: len(brain_script.calls) == 1)
        other = core.new_conversation("web")
        queued = await core.ask("web", "otra", other)
        core.queue.max_parallel = 1
        assert "cancelada" in await core.command("cancelar", f"#{queued['id']}", "telegram")
        assert "cancelada" in await core.command("cancelar", str(first["id"]), "telegram")
        return [(await core.wait(t["id"]))["status"] for t in (first, queued)]

    assert run_with_queue(core, scenario) == ["cancelled", "cancelled"]
    assert "no está en marcha" in asyncio.run(core.command("cancelar", "999", "telegram"))


# -- emergency stop ---------------------------------------------------------------------------


def test_halt_stops_everything_and_survives_restart(core: Any, brain_script: Any, store: Any) -> None:
    from jarvis.security import StoreKillSwitch

    brain_script.gate = asyncio.Event()
    channel = RecordingChannel()
    core.register(channel)

    async def scenario() -> tuple[str, dict[str, Any], bool]:
        running = await core.ask("telegram", "algo largo")
        await until(lambda: len(brain_script.calls) == 1)

        async def sender(request: Any) -> None:
            pass

        approval = asyncio.create_task(core.broker.request("mcp__x__send", "{}", "r", sender))
        await until(lambda: len(core.broker.pending()) == 1)
        message = await core.command("parar", "", "telegram")
        return message, await core.wait(running["id"]), await approval

    message, task, approved = run_with_queue(core, scenario)
    assert "PARADA TOTAL" in message and "canceladas: 1" in message and "denegadas: 1" in message
    assert task["status"] == "cancelled" and approved is False
    assert StoreKillSwitch(store).engaged()
    with pytest.raises(Halted):
        asyncio.run(core.ask("telegram", "hola"))
    assert asyncio.run(core.submit_scheduled(TaskSpec("t", "0 8 * * *", "x"))) is None
    assert "PARADA TOTAL ACTIVADA" in core.status_text()
    asyncio.run(core.startup([]))
    assert any("Sigo con la PARADA TOTAL" in t for t in channel.texts())
    assert "Parada quitada" in asyncio.run(core.command("reanudar", "", "telegram"))
    assert not core.halted and "No había" in asyncio.run(core.command("reanudar", "", "telegram"))


def test_guarded_jobs_do_nothing_while_halted(core: Any) -> None:
    calls: list[int] = []

    async def job() -> int:
        calls.append(1)
        return 1

    guarded = core.guarded(job)
    assert asyncio.run(guarded()) == 1
    core.kill_switch.engage()
    assert asyncio.run(guarded()) is None and calls == [1]


# -- scheduled tasks --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("notify", "reply", "expected"),
    [("always", "Hoy 2 reuniones.", True), ("if_relevant", NOTHING_TO_REPORT + ".", False), ("never", "x", False)],
)
def test_scheduled_task_notify_modes(core: Any, brain_script: Any, notify: str, reply: str, expected: bool) -> None:
    brain_script.script = [(reply, "success")]
    channel = RecordingChannel()
    core.register(channel)
    spec = TaskSpec("resumen", "0 8 * * *", "Resume mi día.", notify=notify, budget_usd=0.2)

    async def scenario() -> dict[str, Any]:
        task = await core.submit_scheduled(spec)
        return await core.wait(task["id"])

    run_with_queue(core, scenario)
    call = brain_script.calls[0]
    assert call["fresh"] and call["budget"] == 0.2
    assert (NOTHING_TO_REPORT in call["text"]) == (notify == "if_relevant")
    assert any(t.startswith("⏱ resumen") for t in channel.texts("notice")) == expected


def test_scheduled_tasks_keep_their_conversation(core: Any, brain_script: Any) -> None:
    spec = TaskSpec("resumen", "0 8 * * *", "x")

    async def scenario() -> None:
        for _ in range(2):
            await core.wait((await core.submit_scheduled(spec))["id"])

    run_with_queue(core, scenario)
    assert brain_script.calls[0]["cid"] == brain_script.calls[1]["cid"]


def test_missed_tasks_are_asked_about(core: Any, store: Any, brain_script: Any) -> None:
    channel = RecordingChannel()
    core.register(channel)
    daily = TaskSpec("resumen", "0 8 * * *", "x")
    new = TaskSpec("nueva", "0 8 * * *", "y")
    off = TaskSpec("apagada", "0 8 * * *", "z", enabled=False)
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    store.kv_set("task_last_run:resumen", (now - timedelta(days=2)).isoformat())
    store.kv_set("task_last_run:apagada", (now - timedelta(days=2)).isoformat())

    async def scenario() -> tuple[list[str], dict[str, Any]]:
        missed = await core.check_missed([daily, new, off], now=now)
        question = next(e.question for e in channel.events if e.kind == "question")
        assert question is not None and "resumen" in question.text
        assert await core.answer(question.id, "run")
        assert not await core.answer(question.id, "run")  # answered once only
        await until(lambda: bool(store.list_tasks(("done",))))
        return missed, store.list_tasks(("done",))[0]

    missed, task = run_with_queue(core, scenario)
    assert missed == ["resumen"] and task["label"] == "resumen"
    assert store.kv_get("task_last_run:nueva") is not None  # first sight: baseline, no question


def test_skipping_a_missed_task_moves_its_baseline(core: Any, store: Any) -> None:
    channel = RecordingChannel()
    core.register(channel)
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    store.kv_set("task_last_run:resumen", (now - timedelta(days=2)).isoformat())

    async def scenario() -> list[str]:
        await core.check_missed([TaskSpec("resumen", "0 8 * * *", "x")], now=now)
        question = channel.events[-1].question
        await core.answer(question.id, "skip")
        return await core.check_missed([TaskSpec("resumen", "0 8 * * *", "x")])

    assert asyncio.run(scenario()) == [] and store.list_tasks() == []


# -- commands ---------------------------------------------------------------------------------


def test_commands(core: Any) -> None:
    async def scenario() -> dict[str, str]:
        return {name: await core.command(name, "", "telegram") for name in ("ayuda", "estado", "cola", "ping", "x")}

    out = asyncio.run(scenario())
    assert "/parar" in out["ayuda"] and out["x"] == out["ayuda"]
    assert "1.50 $ de 10.00 $" in out["estado"] and "Aprobaciones pendientes: 0" in out["estado"]
    assert out["cola"] == "No hay nada en marcha ni en cola."
    assert out["ping"] == out["ayuda"]  # extension commands only exist when it is loaded
    first = core.conversation_for("telegram")
    assert asyncio.run(core.command("nuevo", "", "telegram")).startswith("Conversación nueva")
    assert core.conversation_for("telegram") != first
