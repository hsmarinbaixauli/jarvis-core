from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from callgate import PolicyContext, Tier, classify, is_external

from jarvis.memory import MemoryFile
from jarvis.security import core_registry
from jarvis.watcher import Watcher, parse_triage


def _gmail_with(ids: list[str]) -> MagicMock:
    gmail = MagicMock()
    gmail.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": i} for i in ids]
    }

    def meta(userId: str, id: str, **_: Any) -> MagicMock:
        call = MagicMock()
        call.execute.return_value = {
            "id": id,
            "threadId": id,
            "snippet": f"snippet {id}",
            "payload": {"headers": [{"name": "From", "value": f"{id}@cliente.es"}, {"name": "Subject", "value": id}]},
        }
        return call

    gmail.users.return_value.messages.return_value.get.side_effect = meta
    return gmail


class FakeCompleter:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, system: str, model: str | None = None, budget_usd: float = 0.05) -> str:
        self.prompts.append(prompt)
        return self.reply


def make_watcher(store: Any, notifier: Any, gmail: Any, calendar: Any = None, completer: Any = None) -> Watcher:
    return Watcher(
        store=store, notifier=notifier, services=lambda: (gmail, calendar or MagicMock()), completer=completer
    )


def test_first_run_ignores_backlog(store: Any, notifier: Any) -> None:
    watcher = make_watcher(store, notifier, _gmail_with(["a", "b"]))
    asyncio.run(watcher.check_inbox())
    assert len(notifier.notices) == 1 and "Ignoro los 2" in notifier.notices[0]
    asyncio.run(watcher.check_inbox())
    assert len(notifier.notices) == 1


def test_only_urgent_new_emails_are_reported(store: Any, notifier: Any) -> None:
    store.kv_set("watcher_initialized", "1")
    completer = FakeCompleter(
        '[{"id": "a", "urgent": true, "reason": "Cliente esperando"}, {"id": "b", "urgent": false}]'
    )
    watcher = make_watcher(store, notifier, _gmail_with(["a", "b"]), completer=completer)
    asyncio.run(watcher.check_inbox())
    assert (
        len(notifier.notices) == 1 and "Urgente" in notifier.notices[0] and "Cliente esperando" in notifier.notices[0]
    )
    assert completer.prompts[0].startswith('<contenido_externo origen="gmail">')
    asyncio.run(watcher.check_inbox())
    assert len(notifier.notices) == 1 and len(completer.prompts) == 1  # already seen: no second triage


def test_unparseable_triage_falls_back_to_plain_notice(store: Any, notifier: Any) -> None:
    store.kv_set("watcher_initialized", "1")
    watcher = make_watcher(store, notifier, _gmail_with(["a"]), completer=FakeCompleter("no sé"))
    asyncio.run(watcher.check_inbox())
    assert "Correo nuevo" in notifier.notices[0]


def test_parse_triage_tolerates_prose() -> None:
    parsed = parse_triage('Aquí tienes: [{"id": "x", "urgent": true}] gracias')
    assert parsed["x"]["urgent"] is True
    assert parse_triage("[not json") == {}


def test_calendar_alert_once(store: Any, notifier: Any) -> None:
    now = datetime.now().astimezone()
    calendar = MagicMock()
    calendar.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "e1", "summary": "Llamada", "start": {"dateTime": (now + timedelta(minutes=10)).isoformat()}},
            {"id": "e2", "summary": "Luego", "start": {"dateTime": (now + timedelta(hours=3)).isoformat()}},
            {"id": "e3", "summary": "Todo el día", "start": {"date": now.date().isoformat()}},
        ]
    }
    watcher = make_watcher(store, notifier, MagicMock(), calendar)
    asyncio.run(watcher.check_calendar(now))
    asyncio.run(watcher.check_calendar(now))
    assert len(notifier.notices) == 1 and "Llamada" in notifier.notices[0]


def test_memory_file_append(tmp_path: Path) -> None:
    memory = MemoryFile(tmp_path / "memory" / "owner.md")
    assert memory.read() == ""
    memory.append_note("Prefiere   reuniones\npor la mañana", when=datetime(2026, 10, 27))
    memory.append_note("Cliente clave: Bar Pepe", when=datetime(2026, 10, 28))
    text = memory.read()
    assert text.startswith("# Memoria\n")
    assert "- (2026-10-27) Prefiere reuniones por la mañana" in text
    assert text.count("## Notas añadidas por Jarvis") == 1


def test_memory_tiers() -> None:
    ctx = PolicyContext(registry=core_registry(), allowed_dirs=(Path("."),))
    assert classify("mcp__memory__memory_propose_note", {}, ctx).tier is Tier.APPROVAL
    assert classify("mcp__memory__contacts_save", {}, ctx).tier is Tier.NOTIFY
    assert classify("mcp__memory__todos_add", {}, ctx).tier is Tier.FREE
    assert not is_external("mcp__memory__todos_list", ctx)
    assert is_external("mcp__memory__contacts_search", ctx)


def test_memory_tools(store: Any, tmp_path: Path) -> None:
    pytest.importorskip("claude_agent_sdk")
    from jarvis.memory import build_tools

    memory = MemoryFile(tmp_path / "owner.md")
    tools = {t.name: t for t in build_tools(store, memory)}

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(tools[name].handler(args))

    assert (
        "Anotado"
        in call("memory_propose_note", {"note": "Le gusta el café solo", "reason": "lo dijo"})["content"][0]["text"]
    )
    assert "café solo" in memory.read()
    assert call("memory_propose_note", {"note": "x" * 600, "reason": ""}).get("is_error")

    call("contacts_save", {"name": "Pepe", "email": "Pepe@Bar.es", "notes": "Quiere web"})
    call("contacts_save", {"name": "Pepe García", "email": "pepe@bar.es", "notes": "Llamar el lunes"})
    found = store.search_contacts("pepe")
    assert len(found) == 1 and found[0]["name"] == "Pepe García" and "Quiere web" in found[0]["notes"]

    call("todos_add", {"text": "Enviar factura", "due": "2026-11-01"})
    todo_id = store.list_todos()[0]["id"]
    assert call("todos_complete", {"id": todo_id})["content"][0]["text"] == "Hecho."
    assert store.list_todos() == [] and len(store.list_todos(include_done=True)) == 1


def test_mark_seen(store: Any) -> None:
    assert store.mark_seen("k") is True
    assert store.mark_seen("k") is False
