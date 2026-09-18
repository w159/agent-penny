#!/usr/bin/env python3
"""
Local SQLite index of ConnectWise ticket contact/requester data, refreshed
on a schedule so ad hoc contact lookups and trend-by-requester/company
queries don't each have to re-pull the CW API live.

Root cause this fixes: no local store existed that captured a ticket's
contact/requester at all, so `tools/cw_contact_tool.py`'s trend-style
queries (by requester, by company, over a rolling window) had nothing to
query. Single-ticket and single-contact lookups still hit the live CW API
directly (see cw_contact_tool.py) - those are cheap, one-shot, and always
fresh, so there is no reason to route them through a store that can go
stale. This index exists for the many-tickets-at-once queries where a live
90-day pull on every chat turn would be slow and would trip CW's rate
limiter (see cron/cw_client.py's _MAX_ATTEMPTS_RATE_LIMIT comment for the
1000-ticket-request incident this is trying not to repeat).

Schema follows cron/outage_db.py's convention: WAL mode, a schema_meta
version row, one refresh_state row recording when the index last ran and
over what window, so a caller can tell "empty because never refreshed" and
"stale because the refresh job stopped" apart from "correctly empty".
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from cron.cw_client import CWClient
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DB_PATH = get_hermes_home() / "memories" / "ops" / "cw_ticket_index.db"
SCHEMA_VERSION = 1


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a short-lived connection with WAL and a busy timeout set (same
    shape as cron/outage_db.py's connect()) - a handful of statements per
    refresh, not continuous traffic."""
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
    conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY,
            date_entered TEXT NOT NULL DEFAULT '',
            board TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            priority TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            company_id INTEGER,
            company_name TEXT NOT NULL DEFAULT '',
            contact_id INTEGER,
            contact_name TEXT NOT NULL DEFAULT '',
            contact_email TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL
        )
        """
    )
    # Every query path below filters on one of these three, then orders by date.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_contact_name ON tickets (contact_name, date_entered)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_contact_email ON tickets (contact_email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_company ON tickets (company_name, date_entered)")
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
    )


def _ticket_row(ticket: dict) -> Optional[tuple]:
    tid = ticket.get("id")
    if tid is None:
        return None
    info = ticket.get("_info") or {}
    company = ticket.get("company") or {}
    contact = ticket.get("contact") or {}
    return (
        tid,
        info.get("dateEntered") or "",
        (ticket.get("board") or {}).get("name") or "",
        (ticket.get("status") or {}).get("name") or "",
        (ticket.get("priority") or {}).get("name") or "",
        ticket.get("summary") or "",
        company.get("id"),
        company.get("name") or "",
        contact.get("id"),
        ticket.get("contactName") or "",
        ticket.get("contactEmailAddress") or "",
        datetime.now(timezone.utc).isoformat(),
    )


def refresh_index(days: int = 90, *, client: Optional[CWClient] = None, db_path: Optional[Path] = None) -> dict:
    """Pull every ticket entered in the last `days` from ConnectWise (contact
    fields come back on the base ticket resource - no per-ticket notes/time-entry
    fetch needed, unlike cron/trend_corpus.py's richer digest) and upsert them
    into the local index.

    A CW failure propagates as CWError - never swallowed into "0 tickets
    refreshed today", which would be indistinguishable from a genuinely quiet
    day. Returns a summary dict suitable for an audit log line and for the
    scheduler's own logging.
    """
    cw = client or CWClient()
    since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tickets = cw.tickets_since(since_iso)

    rows = [row for row in (_ticket_row(t) for t in tickets) if row is not None]
    conn = connect(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO tickets (
                id, date_entered, board, status, priority, summary,
                company_id, company_name, contact_id, contact_name, contact_email, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                date_entered=excluded.date_entered, board=excluded.board, status=excluded.status,
                priority=excluded.priority, summary=excluded.summary, company_id=excluded.company_id,
                company_name=excluded.company_name, contact_id=excluded.contact_id,
                contact_name=excluded.contact_name, contact_email=excluded.contact_email,
                indexed_at=excluded.indexed_at
            """,
            rows,
        )
        refreshed_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('last_refreshed_at', ?)", (refreshed_at,)
        )
        conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('window_days', ?)", (str(days),))
        with_contact = sum(1 for row in rows if row[9] or row[10])
    finally:
        conn.close()

    summary = {
        "tickets_pulled": len(tickets),
        "rows_upserted": len(rows),
        "rows_with_contact": with_contact,
        "window_days": days,
        "refreshed_at": refreshed_at,
    }
    logger.info("cw_contact_index: refresh complete: %s", summary)
    return summary


def index_freshness(*, db_path: Optional[Path] = None, max_age_hours: float = 26.0) -> dict:
    """Last refresh time/window and row count, plus whether it's stale
    relative to `max_age_hours` (default: a bit over the 6h refresh
    interval's 4x cadence, tolerant of one missed cycle before flagging)."""
    conn = connect(db_path)
    try:
        row_count = conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM schema_meta")}
    finally:
        conn.close()

    last_refreshed_at = meta.get("last_refreshed_at")
    stale = True
    age_hours: Optional[float] = None
    if last_refreshed_at:
        try:
            last_dt = datetime.fromisoformat(last_refreshed_at)
            age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
            stale = age_hours > max_age_hours
        except ValueError:
            pass

    return {
        "row_count": row_count,
        "last_refreshed_at": last_refreshed_at,
        "window_days": int(meta["window_days"]) if meta.get("window_days") else None,
        "age_hours": age_hours,
        "stale": stale,
    }


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "date": row["date_entered"],
        "board": row["board"],
        "status": row["status"],
        "priority": row["priority"],
        "summary": row["summary"],
        "company": row["company_name"],
        "contact_name": row["contact_name"],
        "contact_email": row["contact_email"],
    }


def contacts_matching(query: str, *, db_path: Optional[Path] = None, limit: int = 10) -> list[dict]:
    """Distinct (contact_name, contact_email) pairs whose name contains
    `query` (case-insensitive) or whose email exactly matches it. Powers
    ambiguity detection in cw_contact_tool.find_tickets_by_contact - a
    caller with more than one row back must disambiguate rather than
    silently mixing two different people's tickets."""
    conn = connect(db_path)
    try:
        if "@" in query:
            cursor = conn.execute(
                "SELECT DISTINCT contact_name, contact_email FROM tickets WHERE contact_email = ? COLLATE NOCASE LIMIT ?",
                (query.strip(), limit),
            )
        else:
            cursor = conn.execute(
                "SELECT DISTINCT contact_name, contact_email FROM tickets "
                "WHERE contact_name LIKE ? COLLATE NOCASE AND contact_name != '' LIMIT ?",
                (f"%{query.strip()}%", limit),
            )
        return [{"contact_name": r["contact_name"], "contact_email": r["contact_email"]} for r in cursor.fetchall()]
    finally:
        conn.close()


def tickets_for_contact(
    *, contact_name: Optional[str] = None, contact_email: Optional[str] = None,
    limit: int = 20, db_path: Optional[Path] = None,
) -> list[dict]:
    """Tickets for one already-resolved contact (exact name or email match), newest first."""
    if not contact_name and not contact_email:
        return []
    conn = connect(db_path)
    try:
        if contact_email:
            cursor = conn.execute(
                "SELECT * FROM tickets WHERE contact_email = ? COLLATE NOCASE ORDER BY date_entered DESC LIMIT ?",
                (contact_email.strip(), limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM tickets WHERE contact_name = ? COLLATE NOCASE ORDER BY date_entered DESC LIMIT ?",
                (contact_name.strip(), limit),
            )
        return [_row_to_dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def trend_by_requester(
    *, days: int = 30, min_tickets: int = 2, limit: int = 20, db_path: Optional[Path] = None,
) -> list[dict]:
    """Ticket counts per contact within the last `days`, descending - the
    aggregate query the old per-cluster trend pipeline had no way to answer
    (it clusters by symptom text, never by who's filing the tickets)."""
    since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT contact_name, contact_email, company_name, COUNT(*) AS ticket_count
            FROM tickets
            WHERE date_entered >= ? AND contact_name != ''
            GROUP BY contact_name, contact_email, company_name
            HAVING COUNT(*) >= ?
            ORDER BY ticket_count DESC
            LIMIT ?
            """,
            (since_iso, min_tickets, limit),
        )
        return [
            {
                "contact_name": r["contact_name"],
                "contact_email": r["contact_email"],
                "company": r["company_name"],
                "ticket_count": r["ticket_count"],
            }
            for r in cursor.fetchall()
        ]
    finally:
        conn.close()


def trend_by_company(*, days: int = 30, min_tickets: int = 2, limit: int = 20, db_path: Optional[Path] = None) -> list[dict]:
    """Ticket counts per company within the last `days`, descending."""
    since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT company_name, COUNT(*) AS ticket_count
            FROM tickets
            WHERE date_entered >= ? AND company_name != ''
            GROUP BY company_name
            HAVING COUNT(*) >= ?
            ORDER BY ticket_count DESC
            LIMIT ?
            """,
            (since_iso, min_tickets, limit),
        )
        return [{"company": r["company_name"], "ticket_count": r["ticket_count"]} for r in cursor.fetchall()]
    finally:
        conn.close()
