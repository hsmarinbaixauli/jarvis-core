from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jarvis.config import load_settings
from jarvis.secrets import SecretStore


def build_jarvis(tmp_path: Path, memory_keyring: Any, extra: str = "") -> Any:
    pytest.importorskip("claude_agent_sdk")
    from jarvis.app import build

    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text(
        "allowed_dirs: [w]\nbrowser: {enabled: false}\ngoogle: {enabled: false}\nqueue: {max_parallel: 3}\n" + extra,
        encoding="utf-8",
    )
    secrets = SecretStore(memory_keyring)
    secrets.set("ANTHROPIC_API_KEY", "sk-ant-test-key-0000000000")
    return build(load_settings(cfg), secrets)


def test_build_wires_channels_through_the_core(tmp_path: Path, memory_keyring: Any) -> None:
    jarvis = build_jarvis(tmp_path, memory_keyring)
    core = jarvis.core
    assert jarvis.bot is None and [c.name for c in core.bus.channels] == ["web"]
    assert core.queue.max_parallel == 3 and core.scheduler is jarvis.scheduler
    gate = jarvis.brain._gate_factory("c1")
    core.kill_switch.engage()
    outcome = asyncio.run(gate.evaluate("Read", {"file_path": "a.md"}))
    assert not outcome.allowed and "parada de emergencia" in outcome.message


def test_build_with_telegram_registers_it_as_a_channel(tmp_path: Path, memory_keyring: Any) -> None:
    memory_keyring.set_password("jarvis", "TELEGRAM_BOT_TOKEN", "123456789:" + "A" * 35)
    jarvis = build_jarvis(tmp_path, memory_keyring, "telegram: {allowed_chat_id: 42}\n")
    assert [c.name for c in jarvis.core.bus.channels] == ["telegram", "web"]
    assert jarvis.bot.core is jarvis.core
    asyncio.run(jarvis.bot.client.close())


def test_invalid_queue_setting(tmp_path: Path) -> None:
    from jarvis.config import ConfigError

    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text("queue: {max_parallel: 50}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg)
