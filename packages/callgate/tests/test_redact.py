from __future__ import annotations

import logging

from callgate import RedactingFilter, Redactor
from callgate.redact import MASK


def test_masks_known_values_and_patterns() -> None:
    redactor = Redactor(["my-very-secret-value"])
    text = "a my-very-secret-value b ghp_" + "x" * 36 + " c sk-ant-" + "y" * 30
    out = redactor.redact(text)
    assert "my-very-secret-value" not in out and "ghp_" not in out and out.count(MASK) == 3
    assert redactor.find(text)[0] == "stored_secret"


def test_custom_mask() -> None:
    assert Redactor(["my-very-secret-value"], mask="***").redact("my-very-secret-value") == "***"


def test_ignores_short_values() -> None:
    assert Redactor(["abc"]).redact("abc abc") == "abc abc"


def test_logging_filter_masks_messages_and_tracebacks() -> None:
    redactor = Redactor(["token-1234567890"])
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "value %s", ("token-1234567890",), None)
    try:
        raise RuntimeError("boom token-1234567890")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    RedactingFilter(redactor).filter(record)
    assert "token-1234567890" not in record.getMessage()
    assert "token-1234567890" not in (record.exc_text or "")
