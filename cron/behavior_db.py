#!/usr/bin/env python3
"""
Connection and schema management for the behavior store
(cron/behavior_store.py). Split out purely to keep behavior_store.py under
the house 300-line file cap -- this is one cohesive concern (how a
connection is opened, what the schema looks like) with no reason to live
inline with the propose/approve/render logic.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

DB_PATH = get_hermes_home() / "memories" / "ops" / "behavior.db"
SCHEMA_VERSION = 1


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a short-lived connection with WAL + busy timeout set.

    The store is opened fresh per call and closed via contextlib.closing in
    every public function in behavior_store.py rather than cached -- this
    workload is a handful of calls per job run, not the gateway's
    continuous per-thread read traffic that _ThreadReadConn in
    hermes_state.py exists to bound. Open-per-call with a guaranteed close
    gives the same property (no leaked fd survives past the call) at this
    volume without that machinery.
    """
    p = db_path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # filesystem may not support WAL; DELETE mode still works
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema creation with a schema_version row for future
    migrations -- adding a column later checks this instead of guessing
    from PRAGMA table_info."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('knob', 'instruction')),
            scope TEXT NOT NULL,
            key TEXT,
            value TEXT,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'rejected', 'retired')),
            requested_by TEXT NOT NULL,
            approved_by TEXT,
            source_chat_id TEXT,
            source_message_id TEXT,
            created_at TEXT NOT NULL,
            superseded_by INTEGER,
            retired_at TEXT,
            active INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            by_user TEXT,
            reason TEXT,
            at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_active ON rules(active, kind, key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_key ON rules(key)")
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def authorized_approvers() -> set[str]:
    """Read the approver allowlist from TEAMS_ALLOWED_USERS.

    Fails closed: unset or empty means nobody is authorized. IDs are never
    hardcoded here -- this reads the same env var Teams auth already uses,
    so the allowlist has one home.
    """
    raw = os.getenv("TEAMS_ALLOWED_USERS", "").strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}
