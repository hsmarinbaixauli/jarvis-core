"""Emergency stop: while engaged, every tool call is refused before anything else is looked at."""

from __future__ import annotations

from typing import Protocol


class KillSwitch(Protocol):
    def engaged(self) -> bool: ...


class MemoryKillSwitch:
    def __init__(self, engaged: bool = False) -> None:
        self._engaged = engaged

    def engage(self) -> None:
        self._engaged = True

    def release(self) -> None:
        self._engaged = False

    def engaged(self) -> bool:
        return self._engaged
