#!/usr/bin/env python3
"""
Penny checking herself when no recovery ticket ever arrives.

Most outages close because a recovery ticket lands. Some never do: the vendor
stops posting, the alert source drops the thread, or the ticket expired out of
CW before the close was seen. Left alone, those runs sit "open" forever and go
on explaining user tickets they have nothing to do with.

So an outage that has gone quiet is re-checked, in this order, stopping at the
first source that actually answers:

  1. the vendor's status feed, where one of the confirmed feeds exists
  2. fresh activity on the same pairing key, which is direct evidence the fault
     is still live even with no feed to ask
  3. nothing - in which case the answer is "unknown"

The governing rule is worth stating plainly because it is easy to erode: a
failure to get an answer produces "unknown", never "cleared". A timeout, a
404, a service with no feed, a payload that changed shape - none of those are
evidence of recovery. Marking an outage cleared because we could not check
would silently stop it explaining the user tickets it really did cause, which
is the more expensive mistake. An "unknown" outage is dropped from the active
set and is never offered as an explanation, but it is also never recorded as
having recovered.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Optional

from cron.outage_feeds import check_service
from cron.outage_store import quiet_outages

__all__ = ["revalidate_all", "revalidate_one"]

OUTCOME_RECOVERED = "recovered"
OUTCOME_STILL_DOWN = "still_down"
OUTCOME_UNRESOLVABLE = "unresolvable"

# Cap matches cron/outage_runs so a long-lived run cannot grow without bound.
MAX_EVIDENCE_ENTRIES = 20


def revalidate_all(conn: sqlite3.Connection, now: datetime, fetch=None,
                   recent_activity_keys=None) -> dict:
    """Re-check every open run that has gone quiet. Returns {run_id: outcome}.

    One failing vendor must not stop the sweep, so each run is handled
    independently and a failure becomes that run's outcome rather than an
    exception that abandons the rest.
    """
    outcomes = {}
    for row in quiet_outages(conn, now=now):
        outcomes[int(row["id"])] = revalidate_one(
            conn, row, now=now, fetch=fetch, recent_activity_keys=recent_activity_keys
        )
    return outcomes


def revalidate_one(conn: sqlite3.Connection, row, now: datetime, fetch=None,
                   recent_activity_keys=None) -> str:
    """Re-check one run and write the result. Returns the outcome."""
    row = dict(row)

    # Signal 2 first: fresh activity on the same key is direct, local evidence
    # and costs nothing. Asking a vendor whether the whole service is up says
    # nothing about one device still alerting at one site.
    if row.get("pairing_key") in (recent_activity_keys or set()):
        _extend(conn, row, now, "still alerting since the last check")
        return OUTCOME_STILL_DOWN

    result = check_service(row.get("service_key") or "", fetch=fetch, now=now)

    if result.state == "up":
        _clear(conn, row, now, result)
        return OUTCOME_RECOVERED
    if result.state == "down":
        _extend(conn, row, now, f"vendor status feed reports: {result.detail}")
        return OUTCOME_STILL_DOWN

    _mark_unknown(conn, row, now, result)
    return OUTCOME_UNRESOLVABLE


def _clear(conn, row, now: datetime, result) -> None:
    stamp = now.isoformat()
    conn.execute(
        """
        UPDATE outages
           SET status = 'cleared', cleared_at = ?, last_signal_at = ?,
               clear_reason = ?, evidence = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            stamp, stamp,
            f"revalidated against the vendor status feed: {result.detail}",
            _append_evidence(row, f"revalidated up: {result.detail} ({result.source_url})"),
            stamp, row["id"],
        ),
    )


def _extend(conn, row, now: datetime, detail: str) -> None:
    stamp = now.isoformat()
    conn.execute(
        "UPDATE outages SET last_signal_at = ?, evidence = ?, updated_at = ? WHERE id = ?",
        (stamp, _append_evidence(row, f"revalidated down: {detail}"), stamp, row["id"]),
    )


def _mark_unknown(conn, row, now: datetime, result) -> None:
    """Could not get an answer. Explicitly NOT a clear: cleared_at stays null
    so nothing in the record claims this was observed to recover."""
    stamp = now.isoformat()
    conn.execute(
        """
        UPDATE outages
           SET status = 'unknown', cleared_at = NULL, clear_reason = ?,
               evidence = ?, updated_at = ?
         WHERE id = ?
        """,
        (
            f"revalidation could not reach an answer: {result.detail}",
            _append_evidence(row, f"revalidation inconclusive: {result.detail}"),
            stamp, row["id"],
        ),
    )


def _append_evidence(row, text: str) -> str:
    try:
        existing = json.loads(row.get("evidence") or "[]")
    except (TypeError, ValueError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    if text not in existing:
        existing.append(text)
    return json.dumps(existing[-MAX_EVIDENCE_ENTRIES:])
