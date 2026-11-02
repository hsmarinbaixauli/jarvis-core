from __future__ import annotations

from typing import Any

import pytest

from jarvis.secrets import SecretError, SecretStore


def test_set_get_list_delete(memory_keyring: Any) -> None:
    store = SecretStore(memory_keyring)
    store.set("TELEGRAM_BOT_TOKEN", "  123456789:abc  ")
    assert store.get("TELEGRAM_BOT_TOKEN") == "123456789:abc"
    assert store.names() == ["TELEGRAM_BOT_TOKEN"]
    assert store.values() == ["123456789:abc"]
    assert store.delete("TELEGRAM_BOT_TOKEN") is True
    assert store.get("TELEGRAM_BOT_TOKEN") is None
    assert store.names() == []
    assert store.delete("TELEGRAM_BOT_TOKEN") is False


@pytest.mark.parametrize("bad", ["lower", "WITH SPACE", "A", "../X", "__index__"])
def test_invalid_names_rejected(memory_keyring: Any, bad: str) -> None:
    with pytest.raises(SecretError):
        SecretStore(memory_keyring).set(bad, "value123")


def test_empty_value_rejected(memory_keyring: Any) -> None:
    with pytest.raises(SecretError):
        SecretStore(memory_keyring).set("ANTHROPIC_API_KEY", "   ")
