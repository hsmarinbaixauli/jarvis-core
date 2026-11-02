"""The tool registry: every tool the agent can call is declared here with its effect.

A tool with no declaration is treated as irreversible, so forgetting to declare one costs a tap on
"approve", never a silent side effect. MCP tools are addressed as ``mcp__<server>__<tool>``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from callgate.tiers import DEFAULT_TIER, MIN_TIER, Decision, Effect, Tier, rank

if TYPE_CHECKING:
    from callgate.policy import PolicyContext

Classifier = Callable[[str, dict[str, Any], "PolicyContext"], Decision | None]

MCP_PREFIX = "mcp__"


class UndeclaredToolError(ValueError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    """*name* is a tool name or an fnmatch pattern (``get_*``). *external*: its output is third-party content."""

    name: str
    effect: Effect
    reason: str = ""
    tier: Tier | None = None
    external: bool = True
    classifier: Classifier | None = None

    def __post_init__(self) -> None:
        if self.tier is not None and self.tier is not Tier.BLOCK and rank(self.tier) < rank(MIN_TIER[self.effect]):
            raise ValueError(f"{self.name}: an {self.effect} tool cannot be {self.tier}.")

    @property
    def default_tier(self) -> Tier:
        return self.tier or DEFAULT_TIER[self.effect]

    @property
    def is_pattern(self) -> bool:
        return any(c in self.name for c in "*?[")


@dataclass
class _Group:
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    patterns: list[ToolSpec] = field(default_factory=list)
    classifier: Classifier | None = None

    def add(self, spec: ToolSpec) -> None:
        if spec.is_pattern:
            self.patterns.append(spec)
        else:
            self.specs[spec.name] = spec

    def find(self, name: str) -> ToolSpec | None:
        exact = self.specs.get(name)
        if exact is not None:
            return exact
        return next((s for s in self.patterns if fnmatch.fnmatchcase(name, s.name)), None)


@dataclass(frozen=True)
class ResolvedTool:
    spec: ToolSpec | None
    classifier: Classifier | None


def mcp_parts(tool_name: str) -> tuple[str, str] | None:
    """``mcp__server__tool`` → (server, tool)."""
    if not tool_name.startswith(MCP_PREFIX):
        return None
    parts = tool_name[len(MCP_PREFIX) :].split("__", 1)
    return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else None


class ToolRegistry:
    def __init__(self) -> None:
        self._top = _Group()
        self._servers: dict[str, _Group] = {}

    def add(self, *specs: ToolSpec) -> ToolRegistry:
        """Declare tools addressed by their bare name (an agent framework's built-ins)."""
        for spec in specs:
            self._top.add(spec)
        return self

    def add_server(
        self, server: str, specs: Iterable[ToolSpec] = (), classifier: Classifier | None = None
    ) -> ToolRegistry:
        """Declare an MCP server's tools. *classifier* sees every call to the server, declared or not."""
        group = self._servers.setdefault(server, _Group())
        for spec in specs:
            group.add(spec)
        if classifier is not None:
            group.classifier = classifier
        return self

    def has_server(self, server: str) -> bool:
        return server in self._servers

    def resolve(self, tool_name: str) -> ResolvedTool | None:
        parts = mcp_parts(tool_name)
        if parts is None:
            spec = self._top.find(tool_name)
            return ResolvedTool(spec, spec.classifier) if spec else None
        group = self._servers.get(parts[0])
        if group is None:
            return None
        spec = group.find(parts[1])
        if spec is None and group.classifier is None:
            return None
        return ResolvedTool(spec, group.classifier)

    def is_external(self, tool_name: str) -> bool:
        resolved = self.resolve(tool_name)
        return True if resolved is None or resolved.spec is None else resolved.spec.external

    def missing(self, server: str, tool_names: Iterable[str]) -> list[str]:
        group = self._servers.get(server, _Group())
        return sorted(n for n in tool_names if group.find(n) is None)

    def require(self, server: str, tool_names: Iterable[str]) -> None:
        """Raise unless every tool is declared: call it when a server you own starts."""
        missing = self.missing(server, tool_names)
        if missing:
            raise UndeclaredToolError(f"{server}: tools without a declared effect: {', '.join(missing)}")

    def rows(self) -> list[tuple[str, ToolSpec]]:
        """(full name, spec) for every declaration, for documentation."""
        out = [(s.name, s) for s in [*self._top.specs.values(), *self._top.patterns]]
        for server, group in sorted(self._servers.items()):
            out += [(f"{MCP_PREFIX}{server}__{s.name}", s) for s in [*group.specs.values(), *group.patterns]]
        return out

    def to_markdown(self) -> str:
        lines = ["| Tool | Effect | Default tier | External content |", "|---|---|---|---|"]
        for name, spec in self.rows():
            lines.append(f"| `{name}` | {spec.effect} | {spec.default_tier} | {'yes' if spec.external else 'no'} |")
        return "\n".join(lines)
