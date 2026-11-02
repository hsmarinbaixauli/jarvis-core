"""Audit entries and where they go. Approval-tier actions do not run if their entry cannot be written."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AuditEntry:
    tool_name: str
    effect: str
    tier: str
    verdict: str  # allowed | approved | rejected | blocked | flagged (suspicious content in a result)
    reason: str
    input_redacted: str
    conversation_id: str | None = None
    ts: str = field(default_factory=_now)


class AuditSink(Protocol):
    def record(self, entry: AuditEntry) -> None:
        """Persist *entry*; raise on failure."""
        ...


class MemoryAuditSink:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)
