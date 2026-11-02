"""Pure classification of a tool call into a tier. No I/O.

Order: declared effect → input-aware classifier → config rule → effect floor → guards.
Config rules may relax a tier but never unblock a call or take an irreversible one below approval;
guards may only tighten.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from callgate.messages import Messages
from callgate.registry import MCP_PREFIX, Classifier, ToolRegistry
from callgate.tiers import MIN_TIER, Decision, Effect, Tier, rank

Observer = Callable[[str, dict[str, Any], "PolicyContext"], None]
ResultObserver = Callable[[str, Any, "PolicyContext"], None]


@dataclass(frozen=True)
class ToolRule:
    pattern: str
    tier: Tier


@dataclass(kw_only=True)
class PolicyContext:
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    allowed_dirs: tuple[Path, ...] = ()
    protected_paths: tuple[Path, ...] = ()
    trusted_domains: tuple[str, ...] = ()
    rules: tuple[ToolRule, ...] = ()
    cwd: Path | None = None
    # Set once the conversation has read third-party content; outbound calls get stricter after that.
    tainted: bool = False
    # Called after a call is allowed, to track state such as the browser's current site.
    observers: tuple[Observer, ...] = ()
    # Called with each tool's result, e.g. to learn which page the browser really ended up on.
    result_observers: tuple[ResultObserver, ...] = ()
    # Full tool name → text shown in an approval request instead of the raw input.
    summarizers: dict[str, Callable[[dict[str, Any]], str]] = field(default_factory=dict)
    # Cross-cutting checks (e.g. legal rules for outgoing email); applied only when stricter.
    guards: tuple[Classifier, ...] = ()
    messages: Messages = field(default_factory=Messages)
    extra: dict[str, Any] = field(default_factory=dict)


def classify(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    decision = _declared_or_default(tool_name, tool_input, ctx)
    effect = decision.effect or Effect.IRREVERSIBLE
    rule = _match_rules(tool_name, ctx.rules)
    if rule and decision.tier is not Tier.BLOCK:
        decision = Decision(rule.tier, ctx.messages("rule", pattern=rule.pattern))
    floor = MIN_TIER[effect]
    if rank(decision.tier) < rank(floor):
        decision = Decision(floor, decision.reason)
    decision = replace(decision, effect=effect)
    for guard in ctx.guards:
        verdict = guard(tool_name, tool_input, ctx)
        if verdict is not None and rank(verdict.tier) > rank(decision.tier):
            decision = replace(verdict, effect=effect)
    return decision


def _declared_or_default(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    resolved = ctx.registry.resolve(tool_name)
    spec = resolved.spec if resolved else None
    effect = spec.effect if spec else Effect.IRREVERSIBLE
    if resolved is not None and resolved.classifier is not None:
        specific = resolved.classifier(tool_name, tool_input, ctx)
        if specific is not None:
            return replace(specific, effect=specific.effect or effect)
    if spec is not None:
        return Decision(spec.default_tier, spec.reason or ctx.messages(f"effect_{spec.effect}"), spec.effect)
    if tool_name.startswith(MCP_PREFIX):
        return Decision(Tier.APPROVAL, ctx.messages("undeclared", tool=tool_name), Effect.IRREVERSIBLE)
    return Decision(Tier.BLOCK, ctx.messages("builtin_disabled", tool=tool_name), Effect.IRREVERSIBLE)


def _match_rules(tool_name: str, rules: Iterable[ToolRule]) -> ToolRule | None:
    lowered = tool_name.lower()
    return next((r for r in rules if fnmatch.fnmatchcase(lowered, r.pattern.lower())), None)


def is_external(tool_name: str, ctx: PolicyContext) -> bool:
    """True when the tool's output is third-party content (undeclared tools count as external)."""
    return ctx.registry.is_external(tool_name)


# -- filesystem scope ----------------------------------------------------------------------------


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def is_within(path: Path, roots: Iterable[Path]) -> bool:
    target = _norm(path)
    for root in roots:
        base = _norm(root)
        if target == base or target.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def check_path(raw: str | None, ctx: PolicyContext, verb: str) -> Decision:
    """*verb* is "read" or "write". Symlinks are resolved, so a link out of an allowed folder is caught."""
    default = ctx.cwd or (ctx.allowed_dirs[0] if ctx.allowed_dirs else Path.cwd())
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = default / path
    if is_within(path, ctx.protected_paths):
        return Decision(Tier.BLOCK, ctx.messages(f"{verb}_protected", path=path))
    if not is_within(path, ctx.allowed_dirs):
        return Decision(Tier.BLOCK, ctx.messages(f"{verb}_outside", path=path))
    return Decision(Tier.FREE, ctx.messages(f"{verb}_ok"))


# -- network scope -------------------------------------------------------------------------------

_MAX_PLAIN_QUERY = 64


def domain_matches(host: str, domains: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def host_is_trusted(host: str, ctx: PolicyContext) -> bool:
    return domain_matches(host, ctx.trusted_domains)


def classify_url(url: str, ctx: PolicyContext) -> Decision:
    """Visiting a URL. A long query string to an unknown host is how data gets smuggled out."""
    msg = ctx.messages
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return Decision(Tier.BLOCK, msg("url_invalid", url=url[:80]))
    if parts.username or parts.password:
        return Decision(Tier.BLOCK, msg("url_credentials"))
    host = parts.hostname
    if host_is_trusted(host, ctx):
        return Decision(Tier.FREE, msg("url_trusted", host=host))
    if len(parts.query) + len(parts.fragment) > _MAX_PLAIN_QUERY:
        return Decision(Tier.APPROVAL, msg("url_payload", host=host))
    if ctx.tainted:
        return Decision(Tier.APPROVAL, msg("url_tainted", host=host))
    return Decision(Tier.NOTIFY, msg("url_new", host=host))
