"""Playbooks: swappable money-making workflows in playbooks/<name>/.

Each playbook folder holds:
- playbook.yaml   → title, description, default settings and limits
- instructions.md → what Jarvis should know and how to work this business (added to the system prompt)
- tools.py        → build(ctx) -> PlaybookTools: its MCP tools, their declared effects and optional hooks

Playbooks are code: the agent can never edit them (protected path). The owner enables them, and
overrides personal settings (name, NIF…), in jarvis.yaml under `playbooks:`.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from callgate import Decision, PolicyContext, Tier, ToolRegistry, ToolSpec, classify
from callgate.claude_code import registry as claude_code_registry

_log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class PlaybookError(ValueError):
    pass


MailHook = Callable[[dict[str, Any]], Awaitable[bool]]


@dataclass
class PlaybookTools:
    tools: list[Any]
    specs: tuple[ToolSpec, ...]
    summarizers: dict[str, Callable[[dict[str, Any]], str]] = field(default_factory=dict)
    on_email: MailHook | None = None
    # Checked on every tool call (any server); may only tighten, e.g. to enforce LSSI on generic Gmail sends.
    guard: Callable[[str, dict[str, Any], PolicyContext], Decision | None] | None = None


@dataclass
class PlaybookContext:
    """What a playbook may use. It never gets the API key or other secrets wholesale."""

    name: str
    settings: dict[str, Any]
    limits: dict[str, Any]
    db_path: Path
    data_dir: Path
    work_dir: Path
    notify: Callable[[str], Awaitable[None]]
    complete: Callable[..., Awaitable[str]]
    run_subagent: Callable[..., Awaitable[dict[str, Any]]]
    get_secret: Callable[[str], str | None]
    google_services: Callable[[], tuple[Any, Any]] | None = None


@dataclass
class Playbook:
    name: str
    path: Path
    title: str
    description: str
    instructions: str
    settings: dict[str, Any]
    limits: dict[str, Any]
    allowed_secrets: tuple[str, ...]

    @property
    def server_name(self) -> str:
        return f"pb_{self.name}"


def discover(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "playbook.yaml").is_file() and not p.name.startswith("_"))


def load_playbook(root: Path, name: str, overrides: dict[str, Any] | None = None) -> Playbook:
    if not _SLUG_RE.match(name):
        raise PlaybookError(f"Nombre de playbook no válido: '{name}' (minúsculas y guiones bajos).")
    folder = root / name
    meta_path = folder / "playbook.yaml"
    if not meta_path.is_file():
        raise PlaybookError(f"No existe el playbook '{name}' en {root}.")
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    if not isinstance(meta, dict):
        raise PlaybookError(f"{meta_path} debe ser un diccionario YAML.")
    instructions_path = folder / "instructions.md"
    if not instructions_path.is_file():
        raise PlaybookError(f"Falta {instructions_path}.")
    if not (folder / "tools.py").is_file():
        raise PlaybookError(f"Falta {folder / 'tools.py'}.")
    overrides = overrides or {}
    settings = {**(meta.get("settings") or {}), **(overrides.get("settings") or {})}
    limits = {**(meta.get("limits") or {}), **(overrides.get("limits") or {})}
    return Playbook(
        name=name,
        path=folder,
        title=str(meta.get("title", name)),
        description=str(meta.get("description", "")),
        instructions=instructions_path.read_text(encoding="utf-8").strip(),
        settings=settings,
        limits=limits,
        allowed_secrets=tuple(str(s) for s in meta.get("secrets", [])),
    )


def build_tools(playbook: Playbook, ctx: PlaybookContext) -> PlaybookTools:
    root = str(playbook.path.parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    module = importlib.import_module(f"{playbook.path.parent.name}.{playbook.name}.tools")
    built = module.build(ctx)
    if not isinstance(built, PlaybookTools):
        raise PlaybookError(f"{playbook.name}/tools.py: build() debe devolver PlaybookTools.")
    missing = (
        ToolRegistry()
        .add_server(playbook.server_name, built.specs)
        .missing(playbook.server_name, [t.name for t in built.tools])
    )
    if missing:
        raise PlaybookError(f"{playbook.name}: herramientas sin efecto declarado: {missing}")
    return built


def instructions_section(playbooks: list[Playbook]) -> str:
    if not playbooks:
        return ""
    parts = ["Playbooks activos (los negocios en los que ayudas). Sigue sus instrucciones al usarlos:"]
    for pb in playbooks:
        parts.append(f"\n## {pb.title}\n{pb.instructions}")
    return "\n".join(parts)


def subagent_policy(cwd: Path) -> PolicyContext:
    """A playbook subagent may only read and write inside its own work folder."""
    return PolicyContext(registry=claude_code_registry(), allowed_dirs=(cwd,), cwd=cwd)


async def subagent_hook(cwd: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    decision = classify(str(hook_input.get("tool_name", "")), hook_input.get("tool_input") or {}, subagent_policy(cwd))
    allowed = decision.tier is Tier.FREE
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allowed else "deny",
            "permissionDecisionReason": decision.reason,
        }
    }


def json_text(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    return {"content": [{"type": "text", "text": text}]}


def error_text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}
