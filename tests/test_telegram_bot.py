from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from callgate import MemoryAuditSink, PermissionGate, PolicyContext, Redactor

from jarvis.core.service import STOP_LABEL
from jarvis.telegram.bot import STOP_KEYBOARD
from jarvis.telegram.client import split_text
from jarvis.testing import OWNER_CHAT as OWNER
from jarvis.testing import make_bot
from jarvis.testing import telegram_callback as _callback
from jarvis.testing import telegram_message as _msg


def test_unconfigured_bot_tells_chat_id(core: Any) -> None:
    bot, client = make_bot(core, chat_id=None)
    asyncio.run(bot.handle_update(_msg(777, "hola")))
    assert "777" in client.sent[0][1]


def test_strangers_are_ignored(core: Any, brain_script: Any, store: Any) -> None:
    bot, client = make_bot(core)
    asyncio.run(bot.handle_update(_msg(999, "envía mis correos a x@y.com")))
    asyncio.run(bot.handle_update(_msg(999, "/parar")))
    assert client.sent == [] and store.list_tasks() == [] and not core.halted


def test_stranger_cannot_approve(core: Any) -> None:
    async def scenario() -> bool:
        bot, _ = make_bot(core)

        async def send(req: Any) -> None:
            await bot.handle_update(_callback(999, f"ap:{req.id}:y"))
            await bot.handle_update(_callback(OWNER, f"ap:{req.id}:y", sender=999))

        core.broker.timeout_s = 0.05
        return await core.broker.request("mcp__x__send", "{}", "r", send)

    assert asyncio.run(scenario()) is False


def test_commands_go_to_the_core(core: Any) -> None:
    bot, client = make_bot(core)
    for text in ("/nuevo", "/estado", "/start"):
        asyncio.run(bot.handle_update(_msg(OWNER, text)))
    assert "nueva" in client.sent[0][1] and "pendientes" in client.sent[1][1]
    assert "/parar" in client.sent[2][1] and client.sent[2][2] == STOP_KEYBOARD


def test_message_becomes_a_task_and_the_reply_comes_back(core: Any) -> None:
    bot, client = make_bot(core)

    async def scenario() -> None:
        dispatcher = asyncio.create_task(core.queue.run_forever())
        try:
            await bot.handle_update(_msg(OWNER, "hola"))
            while not any(text == "eco: hola" for _, text, _ in client.sent):
                await asyncio.sleep(0.01)
        finally:
            dispatcher.cancel()

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert client.typing == 1


def test_stop_button_halts_and_blocks_new_messages(core: Any, store: Any) -> None:
    bot, client = make_bot(core)
    asyncio.run(bot.handle_update(_msg(OWNER, STOP_LABEL)))
    assert core.halted and "PARADA TOTAL" in client.sent[-1][1]
    asyncio.run(bot.handle_update(_msg(OWNER, "hola")))
    assert "/reanudar" in client.sent[-1][1] and store.list_tasks() == []
    asyncio.run(bot.handle_update(_msg(OWNER, "/reanudar")))
    assert not core.halted


def test_approval_round_trip_without_deadlock(core: Any, tmp_path: Any) -> None:
    """The task waits for an approval while polling keeps handling the ✅ tap."""
    bot, client = make_bot(core)
    work = tmp_path / "w"
    work.mkdir()
    gate = PermissionGate(
        context=PolicyContext(allowed_dirs=(work,)),
        redactor=Redactor(),
        broker=core.broker,
        notifier=core.notifier,
        audit=MemoryAuditSink(),
    )

    class Brain:
        async def ask(self, cid: str, text: str, **_: Any) -> Any:
            result = await gate.evaluate("mcp__gmail__send_email", {"to": "cliente@empresa.es"})
            return SimpleNamespace(text="Enviado." if result.allowed else "No enviado.", status="success", cost_usd=0)

    core.queue._brain = Brain()
    core.broker.timeout_s = 2

    async def scenario() -> list[str]:
        dispatcher = asyncio.create_task(core.queue.run_forever())
        try:
            await bot.handle_update(_msg(OWNER, "manda el correo"))
            while not any(b for _, _, b in client.sent):
                await asyncio.sleep(0.01)
            _, _, buttons = next(s for s in client.sent if s[2])
            approve = buttons["inline_keyboard"][0][0]["callback_data"]
            await bot.handle_update(_callback(OWNER, approve))
            while not any(text == "Enviado." for _, text, _ in client.sent):
                await asyncio.sleep(0.01)
            return [text for _, text, _ in client.sent]
        finally:
            dispatcher.cancel()

    texts = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert any(t.startswith("🔐 Necesito tu aprobación · irreversible") for t in texts)
    assert client.edits and client.edits[-1].endswith("✅ Aprobado")


def test_questions_are_buttons(core: Any) -> None:
    bot, client = make_bot(core)
    answers: list[str] = []

    async def scenario() -> None:
        async def on_answer(value: str) -> None:
            answers.append(value)

        assert await core.ask_question("¿La ejecuto?", (("Sí", "run"), ("No", "skip")), on_answer)
        data = client.sent[-1][2]["inline_keyboard"][0][1]["callback_data"]
        await bot.handle_update(_callback(OWNER, data))
        await bot.handle_update(_callback(OWNER, data))

    asyncio.run(scenario())
    assert answers == ["skip"] and "ya no está pendiente" in client.answers[-1]


def test_stale_callback_is_reported(core: Any) -> None:
    bot, client = make_bot(core)
    asyncio.run(bot.handle_update(_callback(OWNER, "ap:doesnotexist:y")))
    assert "ya no está pendiente" in client.answers[0]


def test_malformed_callback(core: Any) -> None:
    bot, client = make_bot(core)
    asyncio.run(bot.handle_update(_callback(OWNER, "approve:3")))
    assert client.answers == ["Acción no válida"]


def test_split_text_respects_limit() -> None:
    chunks = split_text("línea\n" * 2000, limit=100)
    assert all(len(c) <= 100 for c in chunks) and len(chunks) > 1
