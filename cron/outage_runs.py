#!/usr/bin/env python3
"""
The outage run state machine for cron/outage_store.py.

Split out to keep outage_store.py under the house 300-line cap. This module is
one cohesive concern: given a parsed signal and the run it belongs to, what
row changes and how. The store module keeps the public API and the read paths.

A "run" is one continuous period of a service being down, not the service
itself. The distinction carries real weight: correlation windows are computed
from opened_at and cleared_at, so a run that quietly stretches across weeks
would admit almost any ticket in that period. That is why a recurring fault
becomes a series of runs (see MAX_RUN_DAYS) and why a "- New Outage" signal
starts a fresh one rather than extending the old.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from cron.outage_windows import MAX_RUN_DAYS


def apply_open(conn, signal, observed: datetime) -> int:
    current = _current_run(conn, signal)

    # A "- New Outage" suffix means the feed itself is declaring a fresh
    # incident, which really does arrive while a prior run is still open.
    # A run that has already been open longer than MAX_RUN_DAYS is also split,
    # because a recurring fault is a series of runs rather than one endless
    # one, and an endless run gives correlation an unusably wide window.
    if current is not None and not getattr(signal, "is_new_run", False):
        if not _run_too_old(current, observed):
            _extend(conn, current, signal, observed)
            return int(current["id"])
        _retire_overlong_run(conn, current, observed)

    run_seq = _next_run_seq(conn, signal)
    stamp = _iso(observed)
    cursor = conn.execute(
        """
        INSERT INTO outages (stream, pairing_key, run_seq, service_key, service_name,
                             device, site, severity, status, opened_at, last_signal_at,
                             source_ticket_ids, evidence, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
        """,
        (
            signal.stream, signal.pairing_key, run_seq,
            signal.service_key or "", signal.service_name or "",
            getattr(signal, "device", "") or "", getattr(signal, "site", "") or "",
            getattr(signal, "severity", "") or "",
            stamp, stamp,
            json.dumps(_ticket_list(signal)), json.dumps(_evidence_list(signal)),
            stamp, stamp,
        ),
    )
    return int(cursor.lastrowid)


def apply_clear(conn, signal, observed: datetime) -> int:
    current = _current_run(conn, signal)
    stamp = _iso(observed)

    if current is None:
        # The opener fell outside the ingest window, or predates this store.
        # Recording the clear anyway preserves the only evidence we have that
        # the service is currently fine; discarding it would leave a silent
        # hole where a human would reasonably expect an answer.
        cursor = conn.execute(
            """
            INSERT INTO outages (stream, pairing_key, run_seq, service_key, service_name,
                                 device, site, status, opened_at, last_signal_at,
                                 cleared_at, clear_reason, source_ticket_ids, evidence,
                                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'cleared', NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.stream, signal.pairing_key, _next_run_seq(conn, signal),
                signal.service_key or "", signal.service_name or "",
                getattr(signal, "device", "") or "", getattr(signal, "site", "") or "",
                stamp, stamp,
                "recovery seen with no matching open run in the ingest window",
                json.dumps(_ticket_list(signal)), json.dumps(_evidence_list(signal)),
                stamp, stamp,
            ),
        )
        return int(cursor.lastrowid)

    # Duplicate recoveries happen in real traffic (two tickets recovered Apple
    # a minute apart). Fold them into the same run instead of opening a second.
    tickets = _merge_tickets(current, signal)
    conn.execute(
        """
        UPDATE outages
           SET status = 'cleared', cleared_at = ?, last_signal_at = ?,
               clear_reason = ?, source_ticket_ids = ?, evidence = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            stamp, stamp, "recovery signal observed", json.dumps(tickets),
            json.dumps(_merge_evidence(current, signal)), stamp, current["id"],
        ),
    )
    return int(current["id"])


def apply_retraction(conn, signal, observed: datetime) -> Optional[int]:
    current = _current_run(conn, signal)
    if current is None:
        return None
    stamp = _iso(observed)
    conn.execute(
        """
        UPDATE outages
           SET status = 'retracted', last_signal_at = ?, cleared_at = NULL,
               clear_reason = ?, source_ticket_ids = ?, evidence = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            stamp,
            "withdrawn by the source as a false positive; never a real outage",
            json.dumps(_merge_tickets(current, signal)),
            json.dumps(_merge_evidence(current, signal)), stamp, current["id"],
        ),
    )
    return int(current["id"])


def _run_too_old(row, observed: datetime) -> bool:
    opened_raw = row["opened_at"]
    if not opened_raw:
        return False
    try:
        opened = datetime.fromisoformat(opened_raw)
    except ValueError:
        return False
    return (observed - opened) > timedelta(days=MAX_RUN_DAYS)


def _retire_overlong_run(conn, row, observed: datetime) -> None:
    """Close out a run that outlived the maximum, as 'unknown' rather than
    'cleared'. Nothing here was observed to recover."""
    conn.execute(
        """
        UPDATE outages
           SET status = 'unknown', clear_reason = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            "run exceeded the maximum length; split into a new run, never observed to recover",
            _iso(observed), row["id"],
        ),
    )


def _extend(conn, row, signal, observed: datetime) -> None:
    stamp = _iso(observed)
    conn.execute(
        """
        UPDATE outages
           SET last_signal_at = ?, source_ticket_ids = ?, evidence = ?,
               severity = COALESCE(NULLIF(?, ''), severity), updated_at = ?
         WHERE id = ?
        """,
        (
            stamp, json.dumps(_merge_tickets(row, signal)),
            json.dumps(_merge_evidence(row, signal)),
            getattr(signal, "severity", "") or "", stamp, row["id"],
        ),
    )

def _current_run(conn, signal):
    row = conn.execute(
        """
        SELECT * FROM outages
         WHERE stream = ? AND pairing_key = ? AND status = 'open'
         ORDER BY run_seq DESC LIMIT 1
        """,
        (signal.stream, signal.pairing_key),
    ).fetchone()
    if row is not None:
        return row
    # A duplicate clear arrives after the run is already closed. Fold it into
    # the most recent run for this key rather than inserting a second record.
    return conn.execute(
        """
        SELECT * FROM outages
         WHERE stream = ? AND pairing_key = ? AND status = 'cleared'
         ORDER BY run_seq DESC LIMIT 1
        """,
        (signal.stream, signal.pairing_key),
    ).fetchone() if signal.state == "clear" else None


def _next_run_seq(conn, signal) -> int:
    row = conn.execute(
        "SELECT MAX(run_seq) AS top FROM outages WHERE stream = ? AND pairing_key = ?",
        (signal.stream, signal.pairing_key),
    ).fetchone()
    return int((row["top"] or 0) + 1)


def _ticket_list(signal) -> list:
    return [signal.ticket_id] if getattr(signal, "ticket_id", None) is not None else []


def _evidence_list(signal) -> list:
    text = getattr(signal, "evidence", "") or ""
    return [text] if text else []


def _merge_tickets(row, signal) -> list:
    existing = _load_json(row["source_ticket_ids"])
    for ticket_id in _ticket_list(signal):
        if ticket_id not in existing:
            existing.append(ticket_id)
    return existing


def _merge_evidence(row, signal) -> list:
    existing = _load_json(row["evidence"])
    for text in _evidence_list(signal):
        if text not in existing:
            existing.append(text)
    return existing[-20:]  # bounded: a long-running outage must not grow forever


def _load_json(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _iso(value: datetime) -> str:
    return value.isoformat()
