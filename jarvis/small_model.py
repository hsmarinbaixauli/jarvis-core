"""The small model: cheap, simple and sometimes sensitive jobs (email triage, checking content for injections).

Claude stays the main brain. Today these jobs also go to Claude (Haiku). The ``SmallModel`` protocol is
what a local model has to implement to take them over, so that content never leaves the PC:

    class OllamaSmallModel:
        name = "ollama:llama3.1:8b"
        local = True
        async def complete(self, prompt: str, *, system: str) -> str:
            # POST http://127.0.0.1:11434/api/chat with [system, user] messages, stream=False; return the text.

then accept ``small_model.provider: ollama`` in config.py and build it in ``build_small_model``.
"""

from __future__ import annotations

from typing import Any, Protocol

from jarvis.config import Settings


class SmallModel(Protocol):
    name: str
    # True when the model runs on this machine: nothing sent to it leaves the PC.
    local: bool

    async def complete(self, prompt: str, *, system: str) -> str: ...


class ClaudeSmallModel:
    local = False

    def __init__(self, brain: Any, model: str, budget_usd: float = 0.02) -> None:
        self._brain = brain
        self.model = model
        self.name = f"claude:{model}"
        self._budget_usd = budget_usd

    async def complete(self, prompt: str, *, system: str) -> str:
        return str(await self._brain.complete(prompt, system=system, model=self.model, budget_usd=self._budget_usd))


def build_small_model(settings: Settings, brain: Any) -> SmallModel:
    # config.py only accepts providers implemented here.
    return ClaudeSmallModel(brain, settings.small_model or settings.fast_model)
