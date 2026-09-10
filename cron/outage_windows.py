#!/usr/bin/env python3
"""
Time windows for Penny's NOC outage tracking, in one place so they cannot
drift back into scattered literals.

These are the single most abused numbers in this whole system. A window that
creeps wider is how correlation quietly turns back into keyword matching: on
2026-08-25 a set of Outlook tickets three weeks apart were presented as one
cluster purely because nothing enforced a bound. Every window below is a hard
gate, not a tuning knob, and CORRELATION_* in particular is applied BEFORE any
similarity scoring so a strong text match can never buy its way past a bad
timestamp.

Why ingestion is short and the store is durable
-----------------------------------------------
Measured against live data on 2026-08-25: over a 4-day window only 3 of 11
Microsoft 365 incident ids had both their opener and their "Service Restored"
inside the window. That is not a reason to widen ingestion. It is the reason
the outage store must be authoritative. Penny reads a few days of tickets and
carries state forward in the store, so an incident opened on a Monday and
restored the following Tuesday still pairs without ever re-reading old
tickets. Widening the read window to "fix" pairing would paper over a missing
store and drag stale evidence back into correlation.

Signal tickets are retained only about a week when they go without updates, so
INGEST_WINDOW_DAYS also has to stay comfortably inside retention.
"""
from __future__ import annotations

# How far back a sweep reads CW tickets. Runtime code must never query a wider
# window than this; see HISTORICAL_CORPUS_IS_OFFLINE_ONLY below.
INGEST_WINDOW_DAYS = 4

# Hard gate on correlation. A user ticket is a candidate only if it falls
# inside the outage's own window, extended by the margin at each end. Outside
# that, the candidate is rejected before scoring, whatever the text says.
CORRELATION_WINDOW_DAYS = 3
CORRELATION_MARGIN_DAYS = 1

# The longest a single run may be extended by repeated open signals. Past
# this, a further open starts a NEW run and the old one expires to unknown.
#
# Found by replaying real traffic: Spanning Backup emits a discrete error
# every few days, and each one extended the same run until it was a single
# 45-day-wide "outage". Correlation windows are computed from opened_at, so
# one smeared run would admit almost any ticket in that period - the exact
# cross-week false pairing this system exists to prevent. A recurring fault is
# a series of runs, not one endless one.
MAX_RUN_DAYS = 7

# An open outage with no signal for this long triggers cron/outage_revalidate:
# Penny checks herself rather than assuming it is still down.
OUTAGE_QUIET_DAYS = 2

# An outage with no fresh evidence past this age becomes "unknown", never
# "cleared". Nothing haunts the store forever and nothing is silently declared
# fixed.
OUTAGE_MAX_AGE_DAYS = 14

# A correlation stops being reported once it is older than this, so a stale
# cluster is never resurfaced as though it were current.
CORRELATION_REPORT_MAX_AGE_DAYS = 7

# Historical pulls wider than INGEST_WINDOW_DAYS are legitimate for exactly
# two offline purposes: validating a parser against real traffic, and
# bootstrapping the symptom map from closed tickets. They are never an
# operating parameter, and no scheduled job may use one. Any module that needs
# a wide pull must be a script under scripts/, not a cron module.
HISTORICAL_CORPUS_IS_OFFLINE_ONLY = True


def is_within_correlation_window(ticket_at, outage_started_at, outage_ended_at=None) -> bool:
    """The hard gate. True only if the ticket lands inside the outage's window
    plus the margin at each end.

    `outage_ended_at` of None means still open, in which case the window runs
    from the start to the start plus CORRELATION_WINDOW_DAYS. An open outage
    does not grant an unbounded future: an outage that has been open for three
    weeks stops collecting new correlations and gets re-validated instead.
    """
    if ticket_at is None or outage_started_at is None:
        return False

    from datetime import timedelta

    margin = timedelta(days=CORRELATION_MARGIN_DAYS)
    end = outage_ended_at or (outage_started_at + timedelta(days=CORRELATION_WINDOW_DAYS))
    return (outage_started_at - margin) <= ticket_at <= (end + margin)
