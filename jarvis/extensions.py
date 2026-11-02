"""Extensions: optional modules (private business tools, experiments) that plug into the core without changing it.

An extension is a Python module listed under ``extensions:`` in jarvis.yaml that exposes ``create()``
returning an object with:

- ``name``: its config section in jarvis.yaml (``ctx.config``) and its label in logs;
- ``declare(registry)``: declare its MCP tools with their effects (also used by ``python -m jarvis tools``);
- ``setup(ctx)``: build its servers, scheduled jobs, commands and labels.

Extensions get the same gate as everything else: their tools must be declared, irreversible ones always
need approval, and scheduled jobs registered through the context stop with the emergency stop.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from callgate import ToolRegistry

from jarvis.config import Settings
from jarvis.store import Store

_log = logging.getLogger(__name__)


class ExtensionError(RuntimeError):
    pass


@dataclass
class ExtensionContext:
    settings: Settings
    config: dict[str, Any]
    store: Store
    brain: Any
    core: Any
    notifier: Any
    integrations: Any
    scheduler: Any

    @property
    def registry(self) -> ToolRegistry:
        return self.integrations.registry

    def every(self, job_id: str, fn: Callable[[], Awaitable[Any]], hours: float) -> None:
        self.scheduler.add_interval(job_id, self.core.guarded(fn), hours)

    def cron(self, job_id: str, fn: Callable[[], Awaitable[Any]], expr: str) -> None:
        self.scheduler.add_cron(job_id, self.core.guarded(fn), expr)


class Extension(Protocol):
    name: str

    def declare(self, registry: ToolRegistry) -> None: ...
    def setup(self, ctx: ExtensionContext) -> None: ...


def load(path: str) -> Extension:
    try:
        module = importlib.import_module(path)
    except ImportError as exc:
        raise ExtensionError(f"No encuentro la extensión '{path}': {exc}") from exc
    create = getattr(module, "create", None)
    if create is None:
        raise ExtensionError(f"La extensión '{path}' no tiene create().")
    extension = create()
    for attribute in ("name", "declare", "setup"):
        if not hasattr(extension, attribute):
            raise ExtensionError(f"La extensión '{path}' no define {attribute}.")
    return extension


def load_all(paths: tuple[str, ...]) -> list[Extension]:
    return [load(p) for p in paths]
