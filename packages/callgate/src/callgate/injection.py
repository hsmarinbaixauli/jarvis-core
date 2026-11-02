"""Spotting instructions aimed at the agent inside third-party content (prompt injection).

Detection is a smoke alarm, not the wall: the gate still decides what runs. A finding lets the human
know someone tried, and lets the agent get a sharper reminder that the content is data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

_MAX_TEXT = 50_000
_EXCERPT = 160


@dataclass(frozen=True)
class Finding:
    """*reasons* are message codes (``inj_*``, see ``messages.ENGLISH``) or free text from a model."""

    source: str
    reasons: tuple[str, ...]
    excerpt: str
    score: int
    offset: int = 0  # where the first suspicious match starts in the inspected text


class ContentInspector(Protocol):
    async def inspect(self, source: str, text: str) -> Finding | None: ...


@dataclass(frozen=True)
class Rule:
    code: str
    pattern: re.Pattern[str]
    weight: int


def _rule(code: str, pattern: str, weight: int) -> Rule:
    return Rule(code, re.compile(pattern, re.IGNORECASE | re.DOTALL), weight)


RULES: tuple[Rule, ...] = (
    _rule(
        "inj_ignore",
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your|any)\b.{0,40}"
        r"\b(instructions?|rules|prompts?|directives|guidelines)\b"
        r"|\b(ignora|olvida|omite|desobedece)\b.{0,40}\b(instrucciones|reglas|indicaciones|órdenes|normas)\b",
        3,
    ),
    _rule(
        "inj_role",
        r"\byou are now\b|\bnew (system )?instructions\b|\bsystem prompt\b|\bdeveloper mode\b|\bjailbreak\b"
        r"|\bahora eres\b|\bnuevas instrucciones\b|\bmodo desarrollador\b|\bprompt del sistema\b",
        2,
    ),
    _rule(
        "inj_addressed",
        r"\b(dear|attention|hey|hello)\s+(ai|assistant|llm|agent|chatbot)\b|\bas an? (ai|language model|llm)\b"
        r"|\b(ai|llm) (agent|assistant)s? (reading|processing) this\b"
        r"|\b(querido|estimado|atención|hola)\s+(asistente|agente|ia|chatbot)\b|\bsi eres una (ia|inteligencia)\b",
        2,
    ),
    _rule(
        "inj_secrets",
        r"\b(send|forward|email|post|upload|share|reveal|print)\b.{0,60}"
        r"\b(passwords?|credentials?|api[ _-]?keys?|tokens?|secrets?|private keys?|ssh keys?)\b"
        r"|\b(envía|envia|reenvía|reenvia|manda|sube|comparte|revela|muestra)\b.{0,60}"
        r"\b(contraseñas?|credenciales|claves?|tokens?|secretos?|api ?keys?)\b",
        3,
    ),
    _rule(
        "inj_conceal",
        r"\b(do not|don't|never)\s+(tell|inform|mention|notify|alert)\b.{0,30}\b(the )?(user|owner|human)\b"
        r"|\bno (se lo )?(digas|cuentes|menciones|avises|informes)\b.{0,30}\b(al )?(usuario|propietario|humano)\b",
        3,
    ),
    _rule(
        "inj_markup",
        r"<\s*/?\s*(system|assistant|instructions?)\s*>|<\|im_(start|end)\|>|\[\s*/?\s*(system|inst)\s*\]",
        3,
    ),
    _rule("inj_hidden", r"[\u200b-\u200f\u2060-\u2064\ufeff\U000e0000-\U000e007f]{3,}", 2),
)


def response_text(value: Any, limit: int = _MAX_TEXT) -> str:
    """Flatten a tool result (string, MCP content blocks, JSON) into text for inspection."""
    parts: list[str] = []

    def walk(item: Any) -> None:
        if sum(len(p) for p in parts) >= limit:
            return
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, list | tuple):
            for child in item:
                walk(child)
        elif item is not None and not isinstance(item, bool | int | float):
            parts.append(json.dumps(item, default=str))

    walk(value)
    return "\n".join(parts)[:limit]


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - 40)
    snippet = text[left : max(end, start + _EXCERPT - 40)]
    return re.sub(r"\s+", " ", snippet).strip()[:_EXCERPT]


class HeuristicInspector:
    """Pattern rules in English and Spanish. Cheap enough to run on every piece of external content."""

    def __init__(self, rules: tuple[Rule, ...] = RULES, min_score: int = 3) -> None:
        self.rules = rules
        self.min_score = min_score

    async def inspect(self, source: str, text: str) -> Finding | None:
        return self.scan(source, text)

    def scan(self, source: str, text: str) -> Finding | None:
        reasons: list[str] = []
        score = 0
        first: re.Match[str] | None = None
        for rule in self.rules:
            match = rule.pattern.search(text)
            if match is None:
                continue
            reasons.append(rule.code)
            score += rule.weight
            if first is None or match.start() < first.start():
                first = match
        if score < self.min_score or first is None:
            return None
        return Finding(source, tuple(reasons), _excerpt(text, first.start(), first.end()), score, first.start())
