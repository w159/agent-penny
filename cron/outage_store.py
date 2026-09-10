#!/usr/bin/env python3
"""
Penny's durable memory of what is down.

This store is authoritative, not a cache. Outage-signal tickets are retained
only about a week when they go without updates, and measured on the real
4-day ingest window only 4 of 14 runs had both their open and their close
inside the window. The other 10 close through here or never close at all. That
is why the read window stays short and this store carries the state forward:
widening the read to make pairing work would drag weeks-old evidence back into
correlation, which is the exact failure this system exists to prevent.

Three rules that are easy to get wrong and expensive to get wrong:

  A clear is only ever recorded from an observation. Silence produces
  "unknown", never "cleared". An outage nobody confirmed as fixed must not be
  quietly written off, and an "unknown" outage is never cited as an
  explanation for a user ticket.

  A false positive retracts. Microsoft withdraws incidents, 17 times in the
  validation corpus. Recording that as a clear would leave a phantom outage in
  history that had already explained user tickets.

  One row per run, not per service. A service can fail, recover, and fail
  again the same afternoon; the status feed says so with a "- New Outage"
  suffix while an earlier run is still open. Correlation windows come from
  opened_at and cleared_at, so smearing two runs into one row would sweep in
  tickets belonging to neither.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from cron.outage_runs import apply_clear, apply_open, apply_retraction
from cron.outage_windows import OUTAGE_MAX_AGE_DAYS, OUTAGE_QUIET_DAYS

__all__ = [
    "active_outages",
    "apply_signal",
    "expire_stale",
    "outages_for_service",
    "quiet_outages",
]

# States that describe a real service condition. Everything else the parsers
# emit (excluded, unclassified, informational, maintenance) is deliberately
# NOT an outage transition. Maintenance in particular must neither open a run
# nor close one: planned work is not a fault, and it is not a recovery either.
ACTIONABLE_STATES = ("open", "clear", "retracted")

ACTIVE_STATUS = "open"


def apply_signal(conn: sqlite3.Connection, signal, now: Optional[datetime] = None) -> Optional[int]:
    """Fold one parsed signal into the store. Returns the affected run id.

    Returns None, changing nothing, when the signal is not an outage
    transition or carries no usable timestamp. Refusing a signal with no time
    is deliberate: a defaulted timestamp once made every ticket look ancient,
    and here it would put a run at the wrong end of a correlation window.
    """
    if signal is None or getattr(signal, "state", None) not in ACTIONABLE_STATES:
        return None

    observed = getattr(signal, "observed_at", None) or now
    if observed is None:
        return None
    if not (signal.pairing_key or "").strip():
        return None

    if signal.state == "open":
        return apply_open(conn, signal, observed)
    if signal.state == "clear":
        return apply_clear(conn, signal, observed)
    return apply_retraction(conn, signal, observed)


def expire_stale(conn: sqlite3.Connection, now: datetime, max_age_days: int = OUTAGE_MAX_AGE_DAYS) -> int:
    """Move outages with no fresh evidence to 'unknown'. Returns the count.

    Deliberately NOT 'cleared'. Nothing here was observed to recover, and an
    unknown outage stops being offered as an explanation rather than becoming
    a silent success.
    """
    cutoff = _iso(now - timedelta(days=max_age_days))
    cursor = conn.execute(
        """
        UPDATE outages
           SET status = 'unknown', clear_reason = ?, updated_at = ?
         WHERE status = 'open' AND last_signal_at < ?
        """,
        ("no signal past the maximum age; never observed to recover", _iso(now), cutoff),
    )
    return cursor.rowcount or 0


def quiet_outages(conn: sqlite3.Connection, now: datetime, quiet_days: int = OUTAGE_QUIET_DAYS) -> list:
    """Open runs that have gone quiet and are due for self-revalidation."""
    cutoff = _iso(now - timedelta(days=quiet_days))
    rows = conn.execute(
        "SELECT * FROM outages WHERE status = 'open' AND last_signal_at < ? ORDER BY last_signal_at",
        (cutoff,),
    ).fetchall()
    return [dict(row) for row in rows]


def active_outages(conn: sqlite3.Connection, now: Optional[datetime] = None) -> list:
    """What is down right now, as far as Penny actually knows."""
    rows = conn.execute(
        "SELECT * FROM outages WHERE status = ? ORDER BY last_signal_at DESC",
        (ACTIVE_STATUS,),
    ).fetchall()
    return [dict(row) for row in rows]


def outages_for_service(conn: sqlite3.Connection, service_key: str) -> list:
    rows = conn.execute(
        "SELECT * FROM outages WHERE service_key = ? ORDER BY id",
        (service_key or "",),
    ).fetchall()
    return [dict(row) for row in rows]


def _iso(value: datetime) -> str:
    return value.isoformat()
