from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from callgate import PolicyContext, claude_code


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


@pytest.fixture()
def ctx(tmp_path: Path) -> PolicyContext:
    work = tmp_path / "work"
    work.mkdir()
    own_code = work / "agent-code"
    own_code.mkdir()
    return PolicyContext(
        registry=claude_code.registry(),
        allowed_dirs=(work,),
        protected_paths=(own_code,),
        trusted_domains=("github.com", "example.org"),
        cwd=work,
    )
