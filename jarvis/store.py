"""SQLite persistence: conversations, messages, runs (cost), append-only audit log, key/value."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from callgate import AuditEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    sdk_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    cost_usd REAL NOT NULL DEFAULT 0,
    num_turns INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    conversation_id TEXT,
    tool_name TEXT NOT NULL,
    tier TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    input_redacted TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email) WHERE email != '';
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    cost_usd REAL NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    next_attempt_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _task_row(row: sqlite3.Row) -> dict[str, Any]:
    task = dict(row)
    task["options"] = json.loads(task["options"] or "{}")
    return task


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)")}
            if "effect" not in columns:
                conn.execute("ALTER TABLE audit_log ADD COLUMN effect TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- conversations -------------------------------------------------------

    def create_conversation(self, channel: str, title: str = "") -> str:
        conversation_id = uuid.uuid4().hex
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, channel, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, channel, title, now, now),
            )
        return conversation_id

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return dict(row) if row else None

    def list_conversations(self, channel: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM conversations"
        params: tuple[Any, ...] = ()
        if channel:
            sql += " WHERE channel = ?"
            params = (channel,)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, (*params, limit)).fetchall()]

    def set_sdk_session(self, conversation_id: str, sdk_session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET sdk_session_id = ?, updated_at = ? WHERE id = ?",
                (sdk_session_id, utc_now(), conversation_id),
            )

    def set_title_if_empty(self, conversation_id: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET title = ? WHERE id = ? AND title = ''",
                (title[:80], conversation_id),
            )

    # -- messages ------------------------------------------------------------

    def add_message(self, conversation_id: str, role: str, content: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, now),
            )
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))

    def get_messages(self, conversation_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- runs / spend --------------------------------------------------------

    def start_run(self, conversation_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (conversation_id, started_at) VALUES (?, ?)", (conversation_id, utc_now())
            )
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, cost_usd: float, num_turns: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, cost_usd = ?, num_turns = ? WHERE id = ?",
                (utc_now(), status, cost_usd, num_turns, run_id),
            )

    def spent_since_for(self, conversation_id: str, since_utc_iso: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE conversation_id = ? AND started_at >= ?",
                (conversation_id, since_utc_iso),
            ).fetchone()
        return float(row[0])

    def spent_since(self, since_utc_iso: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE started_at >= ?", (since_utc_iso,)
            ).fetchone()
        return float(row[0])

    # -- audit ---------------------------------------------------------------

    def audit(self, entry: AuditEntry) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, conversation_id, tool_name, effect, tier, decision, reason, input_redacted)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.ts,
                    entry.conversation_id,
                    entry.tool_name,
                    entry.effect,
                    entry.tier,
                    entry.verdict,
                    entry.reason,
                    entry.input_redacted,
                ),
            )

    def audit_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def side_effects_since(self, conversation_id: str, since_utc_iso: str) -> int:
        """Calls that changed something (write or irreversible, actually run) in a conversation since a time."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE conversation_id = ? AND ts >= ?"
                " AND effect IN ('write', 'irreversible') AND decision IN ('allowed', 'approved')",
                (conversation_id, since_utc_iso),
            ).fetchone()
        return int(row[0])

    # -- tasks ---------------------------------------------------------------

    def create_task(
        self,
        *,
        kind: str,
        channel: str,
        conversation_id: str,
        prompt: str,
        label: str = "",
        options: dict[str, Any] | None = None,
        max_attempts: int = 3,
        status: str = "queued",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (kind, channel, conversation_id, label, prompt, options, max_attempts, status,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    channel,
                    conversation_id,
                    label,
                    prompt,
                    json.dumps(options or {}),
                    max_attempts,
                    status,
                    utc_now(),
                ),
            )
            return int(cur.lastrowid or 0)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task_row(row) if row else None

    def update_task(self, task_id: int, **fields: Any) -> None:
        allowed = {"status", "attempts", "cost_usd", "result", "error", "started_at", "finished_at", "next_attempt_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown task fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*fields.values(), task_id))

    def list_tasks(self, statuses: tuple[str, ...] = (), limit: int = 50) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM tasks", ()
        if statuses:
            query += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            params = statuses
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [_task_row(r) for r in rows]

    # -- key/value -----------------------------------------------------------

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- seen (watcher dedup) ------------------------------------------------

    def mark_seen(self, key: str) -> bool:
        """Record *key*; True only the first time it is seen."""
        with self._connect() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO seen (key, seen_at) VALUES (?, ?)", (key, utc_now()))
            return cur.rowcount == 1

    # -- contacts (CRM) ------------------------------------------------------

    def save_contact(self, *, name: str, email: str = "", phone: str = "", company: str = "", notes: str = "") -> int:
        email = email.strip().lower()
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute("SELECT id, notes FROM contacts WHERE email = ? AND email != ''", (email,)).fetchone()
            if row:
                merged = "\n".join(n for n in (row["notes"], notes.strip()) if n)
                conn.execute(
                    "UPDATE contacts SET name = ?, phone = COALESCE(NULLIF(?, ''), phone),"
                    " company = COALESCE(NULLIF(?, ''), company), notes = ?, updated_at = ? WHERE id = ?",
                    (name, phone, company, merged, now, row["id"]),
                )
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO contacts (name, email, phone, company, notes, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, email, phone, company, notes.strip(), now),
            )
            return int(cur.lastrowid or 0)

    def search_contacts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        like = f"%{query.strip()}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM contacts WHERE name LIKE ? OR email LIKE ? OR company LIKE ? OR notes LIKE ?"
                " ORDER BY updated_at DESC LIMIT ?",
                (like, like, like, like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- todos ---------------------------------------------------------------

    def add_todo(self, text: str, due: str = "") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO todos (text, due, created_at) VALUES (?, ?, ?)", (text.strip(), due.strip(), utc_now())
            )
            return int(cur.lastrowid or 0)

    def list_todos(self, include_done: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM todos" + ("" if include_done else " WHERE done = 0") + " ORDER BY due = '', due, id"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def complete_todo(self, todo_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE todos SET done = 1, done_at = ? WHERE id = ? AND done = 0", (utc_now(), todo_id))
            return cur.rowcount == 1
