from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from callgate import AuditEntry, Tier

from jarvis.config import ConfigError, load_settings


def test_audit_log_is_append_only(store: Any) -> None:
    store.audit(AuditEntry("t", "irreversible", "approval", "approved", "r", "{}", conversation_id="c"))
    with pytest.raises(sqlite3.DatabaseError):
        with sqlite3.connect(store.path) as conn:
            conn.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.DatabaseError):
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE audit_log SET decision = 'x'")
    entries = store.audit_entries()
    assert len(entries) == 1 and entries[0]["effect"] == "irreversible" and entries[0]["decision"] == "approved"


def test_old_audit_table_gets_effect_column(tmp_path: Any) -> None:
    from jarvis.store import Store

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, conversation_id TEXT,"
            " tool_name TEXT NOT NULL, tier TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,"
            " input_redacted TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO audit_log VALUES (1, 'ts', NULL, 'old', 'free', 'allowed', 'r', '{}')")
    store = Store(db)
    store.init()
    store.init()
    store.audit(AuditEntry("new", "read", "free", "allowed", "r", "{}"))
    assert [(e["tool_name"], e["effect"]) for e in store.audit_entries()] == [("new", "read"), ("old", None)]


def test_init_is_idempotent(store: Any) -> None:
    store.init()
    store.init()


def test_conversation_roundtrip(store: Any) -> None:
    cid = store.create_conversation("web")
    store.add_message(cid, "user", "hola")
    store.set_title_if_empty(cid, "hola")
    store.set_title_if_empty(cid, "otro")
    store.set_sdk_session(cid, "sess-1")
    conv = store.get_conversation(cid)
    assert conv["title"] == "hola" and conv["sdk_session_id"] == "sess-1"
    assert store.get_messages(cid)[0]["content"] == "hola"


def test_spend_tracking(store: Any) -> None:
    run_id = store.start_run("c")
    store.finish_run(run_id, "success", 0.25, 3)
    assert store.spent_since("2000-01-01T00:00:00+00:00") == pytest.approx(0.25)
    assert store.spent_since("2999-01-01T00:00:00+00:00") == 0


def test_defaults_without_file(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.data_dir == (tmp_path / "data").resolve()
    assert settings.web_host == "127.0.0.1"
    assert settings.config_path in settings.protected_paths


def test_full_config(tmp_path: Path) -> None:
    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text(
        """
data_dir: datos
allowed_dirs: [trabajo, /tmp/otra]
trusted_domains: [GitHub.com, .example.org]
budget: {task_usd: 0.5, daily_usd: 3}
approval_timeout_minutes: 2
telegram: {allowed_chat_id: 12345}
web: {port: 9000}
tool_rules:
  - {pattern: "mcp__github__create_branch", tier: notify}
""",
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.allowed_dirs[0] == (tmp_path / "trabajo").resolve()
    assert s.trusted_domains == ("github.com", "example.org")
    assert s.task_budget_usd == 0.5 and s.daily_budget_usd == 3
    assert s.approval_timeout_s == 120 and s.telegram_chat_id == 12345 and s.web_port == 9000
    assert s.tool_rules[0].tier is Tier.NOTIFY


@pytest.mark.parametrize(
    "body",
    [
        "web: {host: 0.0.0.0}",
        "web: {host: 192.168.1.10}",
        "telegram: {allowed_chat_id: 'abc'}",
        "tool_rules: [{pattern: x, tier: yolo}]",
        "budget: {daily_usd: -1}",
        "allowed_dirs: solo-una-cadena",
        "- no es un diccionario",
    ],
)
def test_invalid_config_rejected(tmp_path: Path, body: str) -> None:
    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg)


def test_example_config_loads() -> None:
    example = Path(__file__).resolve().parents[1] / "jarvis.example.yaml"
    settings = load_settings(example)
    assert settings.allowed_dirs


def test_tasks_and_memory_files_are_protected(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "jarvis.yaml")
    assert settings.tasks_file == (tmp_path / "tasks.yaml").resolve()
    assert settings.memory_file == (tmp_path / "memory" / "memoria.md").resolve()
    assert settings.tasks_file in settings.protected_paths and settings.memory_file in settings.protected_paths
    assert settings.watcher_interval_s == 300
