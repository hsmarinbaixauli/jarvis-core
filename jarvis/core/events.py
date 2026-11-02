"""Events the core emits and the channels that receive them.

A channel is anything that talks to the owner: Telegram, the web dashboard, a future CLI or voice front-end.
It registers with the core and gets events; it calls the core's methods to act. Adding one never
touches the core.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from callgate import ApprovalRequest

_log = logging.getLogger(__name__)

EventKind = Literal["reply", "notice", "approval", "question", "task"]


@dataclass(frozen=True)
class Question:
    """Something only the owner can decide, answered with one of *options* (label, value)."""

    id: str
    text: str
    options: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    text: str = ""
    # Channel name for replies to a message; None means every channel.
    target: str | None = None
    conversation_id: str | None = None
    task: dict[str, Any] | None = None
    approval: ApprovalRequest | None = None
    question: Question | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Channel(Protocol):
    name: str
    # Channels that can show buttons and are tied to the owner's identity (Telegram) get approvals and questions.
    can_approve: bool

    async def deliver(self, event: Event) -> None: ...


class EventBus:
    def __init__(self) -> None:
        self._channels: list[Channel] = []

    def register(self, channel: Channel) -> None:
        self._channels.append(channel)

    @property
    def channels(self) -> tuple[Channel, ...]:
        return tuple(self._channels)

    async def publish(self, event: Event) -> int:
        """Deliver to every matching channel; returns how many took it."""
        delivered = 0
        for channel in self._channels:
            if event.target is not None and channel.name != event.target:
                continue
            if event.kind in ("approval", "question") and not channel.can_approve:
                continue
            try:
                await channel.deliver(event)
            except Exception:
                _log.exception("Channel %s failed to deliver a %s event", channel.name, event.kind)
                continue
            delivered += 1
        return delivered
