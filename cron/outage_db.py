#!/usr/bin/env python3
"""
Connection and schema management for the outage store (cron/outage_store.py).

Split out to keep outage_store.py under the house 300-line cap, following the
same shape as cron/behavior_db.py.

One row per RUN, not per service. A service can go down, recover, and go down
again the same afternoon, and the third-party status feed says so explicitly
with a "- New Outage" suffix while an earlier run is still open. Collapsing
those into one row would make an outage look continuous when it was not, and
correlation windows are computed from opened_at and cleared_at, so a smeared
run would sweep in tickets that belong to neither.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

DB_PATH = get_hermes_home() / "memories" / "ops" / "outages.db"
SCHEMA_VERSION = 1


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a short-lived connection with WAL and a busy timeout set.

    Opened per call and closed by the caller rather than cached: this is a
    handful of statements per cron tick, not continuous traffic.
    """
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # filesystem may not support WAL; DELETE mode still works
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema creation with a version row for future migrations."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stream TEXT NOT NULL,
            pairing_key TEXT NOT NULL,
            run_seq INTEGER NOT NULL DEFAULT 1,
            service_key TEXT NOT NULL DEFAULT '',
            service_name TEXT NOT NULL DEFAULT '',
            device TEXT NOT NULL DEFAULT '',
            site TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT '',
            -- 'unknown' exists so a stale outage is never silently promoted to
            -- 'cleared'. Nothing is declared fixed without an observation.
            status TEXT NOT NULL CHECK (
                status IN ('open', 'cleared', 'retracted', 'unknown')
            ),
            opened_at TEXT,
            last_signal_at TEXT NOT NULL,
            cleared_at TEXT,
            clear_reason TEXT NOT NULL DEFAULT '',
            source_ticket_ids TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (stream, pairing_key, run_seq)
        )
        """
    )
    # active_outages and quiet_outages both filter on status then order by
    # recency; every read path this store has goes through one of them.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outages_status ON outages (status, last_signal_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outages_service ON outages (service_key, opened_at)")
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
