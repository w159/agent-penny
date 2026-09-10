#!/usr/bin/env python3
"""
Turns one semantic_pass() group (see cron/trend_cluster_semantic.py) into
the final trend output dict cron/trend_cluster.py returns.

Split out of trend_cluster.py to keep both files under the house 300-line
cap - this is an implementation detail of detect_trends(), not a second
public surface.

Every count here is computed from the group's `digests` list, never taken
from the model - see trend_cluster.py's module docstring for why.
"""
from __future__ import annotations

import hashlib

# See trend_cluster.py for the reasoning behind this constant - kept here
# too since _trend_id() is the only thing that reads it.
STABLE_ID_CORE_SIZE = 6


def _date_only(raw: str) -> str:
    """Normalizes a CW dateEntered value down to the YYYY-MM-DD this
    module's output contract requires. TicketDigest.date carries the raw
    CW timestamp ("2026-08-04T11:28:14Z"), not a bare date - trend_corpus.py
    passes info.get("dateEntered") straight through. Falls back to the raw
    value if it isn't ISO-shaped (e.g. already a bare date in a test
    fixture, or empty)."""
    if isinstance(raw, str) and len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return raw or ""


def _diagnostic_score(note: str) -> int:
    """Cheap, keyword-free proxy for "this note is a raw diagnostic dump,
    not a narrative sentence." A line like
    "TpmPresent : False TpmReady : False ..." is dense with colons and
    digits; "Lynn came in and I enabled TPM" is prose. Counting colons and
    digits instead of matching specific words means this generalizes to
    any future diagnostic output (registry values, error codes, GUIDs)
    without this module having to know what any of it means."""
    return sum(c.isdigit() for c in note) + note.count(":") * 2


def best_evidence(d) -> str:
    """The single most informative line for a ticket. Among a ticket's own
    tech notes, prefers the most diagnostic one over merely the newest -
    a tech's raw tool output is worth more to a reviewer than "I fixed
    it." Falls back to the resolution note, then the raw issue/summary."""
    if d.tech_notes:
        return max(d.tech_notes, key=_diagnostic_score)
    return d.resolution or d.issue or d.summary or ""


def _trend_id(digests: list) -> str:
    """Hashes only the earliest N tickets (by id) in the final member set,
    not the whole set. Ticket ids only grow over time in this corpus, so
    the earliest N tickets of a trend are stable even as later tickets
    join it - hashing the full set would mint a new id every time the
    trend grew, which would both spam a fresh Teams alert and orphan
    trend_escalation.py's per-trend ack state."""
    core = sorted(digests, key=lambda d: d.id)[:STABLE_ID_CORE_SIZE]
    basis = ",".join(str(d.id) for d in core)
    date_part = min(_date_only(d.date) for d in core).replace("-", "")
    digest_hash = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:6]
    return f"TREND-{date_part}-{digest_hash}"


def finalize_trend(group: dict, *, min_tickets: int, min_distinct_subjects: int) -> dict | None:
    """Turns one semantic_pass() group into the final output dict, or None
    if it doesn't clear the threshold (defense in depth: a merge of
    already-promoted candidates can only grow, never shrink below
    threshold, but re-checking here means a future change to
    semantic_pass() can't silently defeat Stage 2)."""
    digests = sorted(group["digests"], key=lambda d: d.id)
    contacts = {d.contact for d in digests if d.contact}
    devices = sorted({dev for d in digests for dev in d.devices})
    techs = sorted({t for d in digests for t in d.techs})
    subjects = contacts | set(devices)

    if len(digests) < min_tickets or len(subjects) < min_distinct_subjects:
        return None

    spans_both = {d.is_automated for d in digests} == {True, False}
    ticket_count = len(digests)
    user_count = len(contacts)
    device_count = len(devices)
    confidence = (
        "high"
        if spans_both or (ticket_count >= 5 and len(subjects) >= 3)
        else "medium"
    )

    member_tickets = sorted(digests, key=lambda d: (d.date, d.id), reverse=True)
    tickets_out = [
        {"id": d.id, "date": _date_only(d.date), "summary": d.summary, "evidence": best_evidence(d)}
        for d in member_tickets
    ]

    return {
        "trend_id": _trend_id(digests),
        "title": group["title"],
        "confidence": confidence,
        "first_seen": min(_date_only(d.date) for d in digests),
        "last_seen": max(_date_only(d.date) for d in digests),
        "ticket_count": ticket_count,
        "device_count": device_count,
        "user_count": user_count,
        "techs": techs,
        "devices": devices,
        "why_related": group["why_related"],
        "recommended_action": group["recommended_action"],
        "tickets": tickets_out,
        "escalation_level": 0,
    }
