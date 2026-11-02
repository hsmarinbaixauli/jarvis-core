"""Declarations for Claude Code's built-in tools (the ones the Claude Agent SDK exposes by bare name).

Only the tools listed here can run; any other built-in (Bash, Task…) is undeclared and therefore blocked.
File tools are confined to ``PolicyContext.allowed_dirs``; web tools follow the network scope rules.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from callgate.policy import PolicyContext, check_path, classify_url
from callgate.registry import ToolRegistry, ToolSpec
from callgate.tiers import Decision, Effect, Tier


def _glob_base(pattern: str) -> str | None:
    """The fixed prefix of a Glob pattern that escapes the working folder, if any."""
    if not pattern:
        return None
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts or pattern.startswith("~"):
        head = pattern.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
        return head or os.sep
    return None


def _read(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    decision = check_path(tool_input.get("file_path") or tool_input.get("path"), ctx, "read")
    glob_base = _glob_base(str(tool_input.get("pattern", ""))) if tool_name == "Glob" else None
    if decision.tier is Tier.FREE and glob_base:
        decision = check_path(glob_base, ctx, "read")
    return decision


def _write(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        return Decision(Tier.BLOCK, ctx.messages("write_no_path"))
    return check_path(raw, ctx, "write")


def _web_search(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    # The query itself can carry data out once the conversation has read untrusted content.
    return Decision(Tier.NOTIFY if ctx.tainted else Tier.FREE, ctx.messages("web_search"))


def _web_fetch(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    return classify_url(str(tool_input.get("url", "")), ctx)


def _internal(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    return Decision(Tier.FREE, ctx.messages("internal"))


READ_TOOLS = ("Read", "Glob", "Grep", "LS")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

SPECS: tuple[ToolSpec, ...] = (
    *(ToolSpec(n, Effect.READ, external=False, classifier=_read) for n in READ_TOOLS),
    *(ToolSpec(n, Effect.WRITE, tier=Tier.FREE, external=False, classifier=_write) for n in WRITE_TOOLS),
    *(
        ToolSpec(n, Effect.WRITE, tier=Tier.FREE, external=False, classifier=_internal)
        for n in ("TodoWrite", "TodoRead")
    ),
    ToolSpec("WebSearch", Effect.READ, classifier=_web_search),
    ToolSpec("WebFetch", Effect.READ, classifier=_web_fetch),
)


def registry() -> ToolRegistry:
    return ToolRegistry().add(*SPECS)
