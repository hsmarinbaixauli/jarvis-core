"""Pending approvals: a tool call waits here until a human answers, or it times out (a timeout is a no)."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class ApprovalRequest:
    id: str
    tool_name: str
    summary: str
    reason: str
    effect: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    origin: str | None = None
    future: asyncio.Future[bool] | None = None


SendPrompt = Callable[[ApprovalRequest], Awaitable[None]]


class ApprovalBroker:
    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        self._pending: dict[str, ApprovalRequest] = {}

    async def request(
        self,
        tool_name: str,
        summary: str,
        reason: str,
        send: SendPrompt,
        origin: str | None = None,
        effect: str | None = None,
    ) -> bool:
        """Send the prompt and wait for a decision. Anything but an explicit yes is a no."""
        loop = asyncio.get_running_loop()
        req = ApprovalRequest(
            id=secrets.token_urlsafe(9),
            tool_name=tool_name,
            summary=summary,
            reason=reason,
            effect=effect,
            future=loop.create_future(),
            origin=origin,
        )
        self._pending[req.id] = req
        try:
            await send(req)
            assert req.future is not None
            return await asyncio.wait_for(req.future, timeout=self.timeout_s)
        except TimeoutError:
            return False
        finally:
            self._pending.pop(req.id, None)

    def resolve(self, request_id: str, approved: bool) -> ApprovalRequest | None:
        req = self._pending.get(request_id)
        if req is None or req.future is None or req.future.done():
            return None
        req.future.set_result(approved)
        return req

    def deny_all(self) -> int:
        """Answer no to everything pending (used by the emergency stop)."""
        denied = 0
        for req in list(self._pending.values()):
            if req.future is not None and not req.future.done():
                req.future.set_result(False)
                denied += 1
        return denied

    def pending(self) -> list[ApprovalRequest]:
        return list(self._pending.values())
