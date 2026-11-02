"""Detect and mask secrets in text: known secret values plus common credential formats."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

_MIN_SECRET_LEN = 8
MASK = "[REDACTED]"

_PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "telegram_token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "slack_token": re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b"),
}


class Redactor:
    def __init__(self, known_values: Iterable[str] = (), mask: str = MASK) -> None:
        self.mask = mask
        self._known: set[str] = set()
        for value in known_values:
            self.add(value)

    def add(self, value: str | None) -> None:
        if value and len(value) >= _MIN_SECRET_LEN:
            self._known.add(value)

    def find(self, text: str) -> list[str]:
        """Return labels of the secrets present in *text* (never the values)."""
        if not text:
            return []
        found = ["stored_secret" for value in self._known if value in text][:1]
        found += [label for label, pattern in _PATTERNS.items() if pattern.search(text)]
        return found

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value in sorted(self._known, key=len, reverse=True):
            text = text.replace(value, self.mask)
        for pattern in _PATTERNS.values():
            text = pattern.sub(self.mask, text)
        return text


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._redactor.redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = self._redactor.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True
