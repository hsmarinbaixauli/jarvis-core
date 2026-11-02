"""Test kit for Jarvis and its extensions: in-memory keyring, scripted brain, a ready core, a fake Telegram.

Core tests load it through tests/conftest.py; an extension's tests do the same with
``from jarvis.testing import *`` in their own conftest.py. Needs pytest (a dev dependency).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from jarvis.telegram.bot import TelegramBot

OWNER_CHAT = 4242

__all__ = [
    "OWNER_CHAT",
    "FakeTelegram",
    "MemoryKeyring",
    "RecordingChannel",
    "RecordingNotifier",
    "ScriptedBrain",
    "brain_script",
    "core",
    "make_bot",
    "memory_keyring",
    "mock_calendar_service",
    "mock_gmail_service",
    "notifier",
    "store",
    "telegram_callback",
    "telegram_message",
]


@pytest.fixture()
def mock_calendar_service() -> MagicMock:
    """Return a MagicMock that mimics the Google Calendar API service object.

    The fixture pre-wires the chain ``service.events().list().execute()`` so
    individual tests can override ``.execute.return_value`` without repeating
    the chain setup.
    """
    service: MagicMock = MagicMock()
    # Default: empty events list so tests that don't care about data still work.
    service.events.return_value.list.return_value.execute.return_value = {"items": []}
    service.events.return_value.insert.return_value.execute.return_value = {
        "id": "evt001",
        "summary": "Test Event",
    }
    return service


@pytest.fixture()
def mock_gmail_service() -> MagicMock:
    """Return a MagicMock that mimics the Gmail API service object.

    Pre-wires common method chains so tests can override ``.execute.return_value``
    without boilerplate.
    """
    service: MagicMock = MagicMock()
    # Default: empty message list.
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {"messages": []}
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {}
    service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "msg001"}
    service.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
    # Profile lookup used by _get_sender_email.
    service.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "test@example.com"}
    return service


class MemoryKeyring:
    """In-memory stand-in for the OS keyring."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.data.pop((service, username), None)


@pytest.fixture()
def memory_keyring() -> MemoryKeyring:
    return MemoryKeyring()


@pytest.fixture()
def store(tmp_path: Any) -> Any:
    from jarvis.store import Store

    s = Store(tmp_path / "data" / "jarvis.db")
    s.init()
    return s


class RecordingNotifier:
    def __init__(self) -> None:
        self.notices: list[str] = []
        self.approval_requests: list[Any] = []
        self.auto_answer: bool | None = None
        self.broker: Any = None

    async def notify(self, text: str) -> None:
        self.notices.append(text)

    async def ask_approval(self, request: Any) -> None:
        self.approval_requests.append(request)
        if self.auto_answer is not None and self.broker is not None:
            self.broker.resolve(request.id, self.auto_answer)


@pytest.fixture()
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


class ScriptedBrain:
    """Stands in for Brain.ask: replies from a script, records calls, can simulate side effects or hang."""

    def __init__(self, store: Any = None) -> None:
        self.store = store
        self.calls: list[dict[str, Any]] = []
        self.script: list[tuple[str, str]] = []  # (text, status); empty → echo
        self.side_effect: str | None = None  # "write" or "irreversible" call recorded during the attempt
        self.gate: Any = None  # awaited before replying (an asyncio.Event) to keep a task running

    async def ask(
        self, conversation_id: str, text: str, *, fresh: bool = False, budget_usd: float | None = None
    ) -> Any:
        from types import SimpleNamespace

        self.calls.append({"cid": conversation_id, "text": text, "fresh": fresh, "budget": budget_usd})
        if self.side_effect and self.store is not None:
            from callgate import AuditEntry

            self.store.audit(
                AuditEntry("mcp__x__do", self.side_effect, "approval", "approved", "r", "{}", conversation_id)
            )
        if self.gate is not None:
            await self.gate.wait()
        reply, status = self.script.pop(0) if self.script else (f"eco: {text}", "success")
        return SimpleNamespace(text=reply, status=status, cost_usd=0.01)


class RecordingChannel:
    def __init__(self, name: str = "telegram", can_approve: bool = True) -> None:
        self.name = name
        self.can_approve = can_approve
        self.events: list[Any] = []

    async def deliver(self, event: Any) -> None:
        self.events.append(event)

    def texts(self, *kinds: str) -> list[str]:
        return [e.text for e in self.events if not kinds or e.kind in kinds]


@pytest.fixture()
def brain_script(store: Any) -> ScriptedBrain:
    return ScriptedBrain(store)


@pytest.fixture()
def core(store: Any, brain_script: ScriptedBrain) -> Any:
    from callgate import ApprovalBroker

    from jarvis.core.events import EventBus
    from jarvis.core.service import BusNotifier, JarvisCore
    from jarvis.security import StoreKillSwitch

    bus = EventBus()

    class Budget:
        daily_usd = 10.0

        def spent_today(self) -> float:
            return 1.5

    c = JarvisCore(
        store=store,
        brain=brain_script,
        broker=ApprovalBroker(1),
        kill_switch=StoreKillSwitch(store),
        bus=bus,
        notifier=BusNotifier(bus),
        budget=Budget(),
    )
    c.queue._poll_s = 0.01
    return c


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Any]] = []
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.typing = 0

    async def send_message(self, chat_id: int, text: str, buttons: Any = None) -> None:
        self.sent.append((chat_id, text, buttons))

    async def send_typing(self, chat_id: int) -> None:
        self.typing += 1

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append(text)

    async def clear_buttons(self, chat_id: int, message_id: int) -> None:
        pass

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append(text)


def telegram_message(chat: int, text: str) -> dict[str, Any]:
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def telegram_callback(chat: int, data: str, sender: int | None = None) -> dict[str, Any]:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb",
            "data": data,
            "from": {"id": sender if sender is not None else chat},
            "message": {"message_id": 9, "chat": {"id": chat}, "text": "🔐"},
        },
    }


def make_bot(core: Any, chat_id: int | None = OWNER_CHAT) -> tuple[TelegramBot, FakeTelegram]:
    client = FakeTelegram()
    bot = TelegramBot(client, core, chat_id)  # type: ignore[arg-type]
    core.register(bot)
    return bot, client
