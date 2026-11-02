"""Effects (what a tool does) and tiers (what the gate does about it)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Effect(StrEnum):
    """Declared by whoever registers a tool. Irreversible tools always need a human's approval."""

    READ = "read"
    WRITE = "write"
    IRREVERSIBLE = "irreversible"


class Tier(StrEnum):
    FREE = "free"
    NOTIFY = "notify"
    APPROVAL = "approval"
    BLOCK = "block"


_ORDER = {Tier.FREE: 0, Tier.NOTIFY: 1, Tier.APPROVAL: 2, Tier.BLOCK: 3}

DEFAULT_TIER = {Effect.READ: Tier.FREE, Effect.WRITE: Tier.NOTIFY, Effect.IRREVERSIBLE: Tier.APPROVAL}
# Nothing (classifier, config rule) may take a call below this.
MIN_TIER = {Effect.READ: Tier.FREE, Effect.WRITE: Tier.FREE, Effect.IRREVERSIBLE: Tier.APPROVAL}


def rank(tier: Tier) -> int:
    return _ORDER[tier]


def stricter(a: Tier, b: Tier) -> Tier:
    return a if _ORDER[a] >= _ORDER[b] else b


@dataclass(frozen=True)
class Decision:
    tier: Tier
    reason: str
    effect: Effect | None = None
