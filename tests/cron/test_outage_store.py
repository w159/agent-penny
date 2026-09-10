#!/usr/bin/env python3
"""Tests for cron/outage_store.py - the durable memory of what is down.

This store is authoritative, not a cache. Signal tickets are retained only
about a week without updates, and on a 4-day ingest window only 4 of 14 runs
had both their open and their close inside the window. Everything else closes
through here or not at all.

The rules that matter most, all encoded below:
  - a clear NEVER arrives by assumption; silence produces "unknown"
  - a false positive retracts rather than clears, so no phantom outage stays
    in history having once explained user tickets
  - duplicate clears and "New Outage" re-opens both happen in real traffic
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cron.outage_signals import OutageSignal, STREAM_M365, STREAM_STATUS
from cron.outage_store import (
    active_outages,
    apply_signal,
    expire_stale,
    outages_for_service,
    quiet_outages,
)
from cron.outage_windows import MAX_RUN_DAYS, OUTAGE_MAX_AGE_DAYS, OUTAGE_QUIET_DAYS

T0 = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path):
    from cron.outage_db import connect
    conn = connect(tmp_path / "outage.db")
    yield conn
    conn.close()


def _sig(state, pairing_key="TM1423737", stream=STREAM_M365, service_key="microsoft_teams",
         service_name="Microsoft Teams", ticket_id=1, **kw):
    return OutageSignal(stream=stream, state=state, pairing_key=pairing_key,
                        service_key=service_key, service_name=service_name,
                        ticket_id=ticket_id, evidence="fixture", **kw)


# --------------------------------------------------------------------------
# opening and extending a run
# --------------------------------------------------------------------------

def test_open_signal_creates_one_run(db):
    apply_signal(db, _sig("open"), now=T0)
    runs = active_outages(db, now=T0)
    assert len(runs) == 1
    assert runs[0]["service_key"] == "microsoft_teams"
    assert runs[0]["status"] == "open"
    assert runs[0]["opened_at"] is not None


def test_repeat_open_extends_rather_than_duplicating(db):
    apply_signal(db, _sig("open", ticket_id=1), now=T0)
    apply_signal(db, _sig("open", ticket_id=2), now=T0 + timedelta(hours=3))
    runs = active_outages(db, now=T0 + timedelta(hours=3))
    assert len(runs) == 1
    assert runs[0]["last_signal_at"] > runs[0]["opened_at"]
    assert "2" in runs[0]["source_ticket_ids"]


def test_new_outage_suffix_opens_a_second_run_while_the_first_is_open(db):
    apply_signal(db, _sig("open", pairing_key="github", stream=STREAM_STATUS,
                          service_key="github", service_name="GitHub"), now=T0)
    apply_signal(db, _sig("open", pairing_key="github", stream=STREAM_STATUS,
                          service_key="github", service_name="GitHub",
                          is_new_run=True, ticket_id=2), now=T0 + timedelta(hours=1))
    assert len(active_outages(db, now=T0 + timedelta(hours=1))) == 2


def test_a_recurring_fault_becomes_a_series_of_runs_not_one_endless_one(db):
    """Live regression: Spanning Backup emitted 43 discrete errors over six
    weeks and each extended the same run, producing a single 45-day-wide
    outage. A window that wide would admit almost any ticket in the period."""
    apply_signal(db, _sig("open", pairing_key="spanning", service_key="spanning",
                          service_name="Spanning Backup"), now=T0)
    later = T0 + timedelta(days=MAX_RUN_DAYS + 1)
    apply_signal(db, _sig("open", pairing_key="spanning", service_key="spanning",
                          service_name="Spanning Backup", ticket_id=2), now=later)

    runs = outages_for_service(db, "spanning")
    assert len(runs) == 2, "the overlong run must split rather than stretch"
    assert runs[0]["status"] == "unknown"
    assert runs[0]["cleared_at"] is None, "a split run was never observed to recover"
    active = active_outages(db, now=later)
    assert len(active) == 1
    assert active[0]["opened_at"].startswith(later.date().isoformat())


def test_a_run_inside_the_maximum_still_extends(db):
    apply_signal(db, _sig("open"), now=T0)
    apply_signal(db, _sig("open", ticket_id=2), now=T0 + timedelta(days=MAX_RUN_DAYS - 1))
    assert len(outages_for_service(db, "microsoft_teams")) == 1


# --------------------------------------------------------------------------
# clearing
# --------------------------------------------------------------------------

def test_clear_closes_the_open_run(db):
    apply_signal(db, _sig("open"), now=T0)
    apply_signal(db, _sig("clear", ticket_id=2), now=T0 + timedelta(hours=5))
    assert active_outages(db, now=T0 + timedelta(hours=5)) == []
    stored = outages_for_service(db, "microsoft_teams")
    assert stored[0]["status"] == "cleared"
    assert stored[0]["cleared_at"] is not None


def test_duplicate_clear_is_idempotent(db):
    """Live case: tickets 93405 and 93406 both recovered Apple."""
    apply_signal(db, _sig("open", pairing_key="apple", stream=STREAM_STATUS,
                          service_key="apple", service_name="Apple"), now=T0)
    apply_signal(db, _sig("clear", pairing_key="apple", stream=STREAM_STATUS,
                          service_key="apple", service_name="Apple", ticket_id=93405),
                 now=T0 + timedelta(hours=2))
    apply_signal(db, _sig("clear", pairing_key="apple", stream=STREAM_STATUS,
                          service_key="apple", service_name="Apple", ticket_id=93406),
                 now=T0 + timedelta(hours=2, minutes=1))
    stored = outages_for_service(db, "apple")
    assert len(stored) == 1
    assert stored[0]["status"] == "cleared"


def test_clear_with_no_open_is_recorded_not_discarded(db):
    """The opener fell outside the ingest window. Discarding the clear would
    lose the only evidence that the service is currently fine."""
    apply_signal(db, _sig("clear"), now=T0)
    stored = outages_for_service(db, "microsoft_teams")
    assert len(stored) == 1
    assert stored[0]["status"] == "cleared"
    assert "no matching open" in stored[0]["clear_reason"]


def test_a_later_open_after_a_clear_starts_a_new_run(db):
    apply_signal(db, _sig("open"), now=T0)
    apply_signal(db, _sig("clear", ticket_id=2), now=T0 + timedelta(hours=2))
    apply_signal(db, _sig("open", ticket_id=3), now=T0 + timedelta(days=1))
    assert len(outages_for_service(db, "microsoft_teams")) == 2
    assert len(active_outages(db, now=T0 + timedelta(days=1))) == 1


# --------------------------------------------------------------------------
# retraction and non-events
# --------------------------------------------------------------------------

def test_false_positive_retracts_rather_than_clearing(db):
    apply_signal(db, _sig("open"), now=T0)
    apply_signal(db, _sig("retracted", ticket_id=2), now=T0 + timedelta(hours=1))
    stored = outages_for_service(db, "microsoft_teams")
    assert stored[0]["status"] == "retracted"
    assert active_outages(db, now=T0 + timedelta(hours=1)) == []


def test_maintenance_neither_opens_nor_clears(db):
    apply_signal(db, _sig("open", pairing_key="ninjaone", stream=STREAM_STATUS,
                          service_key="ninjaone", service_name="NinjaOne"), now=T0)
    apply_signal(db, _sig("maintenance", pairing_key="ninjaone", stream=STREAM_STATUS,
                          service_key="ninjaone", service_name="NinjaOne", ticket_id=2),
                 now=T0 + timedelta(hours=1))
    runs = active_outages(db, now=T0 + timedelta(hours=1))
    assert len(runs) == 1
    assert runs[0]["status"] == "open"


def test_non_events_are_not_stored_as_outages(db):
    for state in ("excluded", "unclassified", "informational"):
        apply_signal(db, _sig(state, ticket_id=7), now=T0)
    assert active_outages(db, now=T0) == []
    assert outages_for_service(db, "microsoft_teams") == []


# --------------------------------------------------------------------------
# the expiry backstop: unknown, never cleared
# --------------------------------------------------------------------------

def test_stale_outage_becomes_unknown_never_cleared(db):
    apply_signal(db, _sig("open"), now=T0)
    later = T0 + timedelta(days=OUTAGE_MAX_AGE_DAYS + 1)
    changed = expire_stale(db, now=later)
    assert changed == 1
    stored = outages_for_service(db, "microsoft_teams")
    assert stored[0]["status"] == "unknown"
    assert stored[0]["cleared_at"] is None, "an expired outage was never observed to recover"
    assert active_outages(db, now=later) == []


def test_a_fresh_outage_is_not_expired(db):
    apply_signal(db, _sig("open"), now=T0)
    assert expire_stale(db, now=T0 + timedelta(days=1)) == 0
    assert len(active_outages(db, now=T0 + timedelta(days=1))) == 1


def test_quiet_outages_surfaces_revalidation_candidates(db):
    apply_signal(db, _sig("open"), now=T0)
    quiet = quiet_outages(db, now=T0 + timedelta(days=OUTAGE_QUIET_DAYS + 1))
    assert len(quiet) == 1
    assert quiet_outages(db, now=T0 + timedelta(hours=1)) == []


# --------------------------------------------------------------------------
# durability: the whole reason this store exists
# --------------------------------------------------------------------------

def test_state_survives_reconnection(tmp_path):
    """The source ticket is deleted after about a week. The record is not."""
    from cron.outage_db import connect
    path = tmp_path / "outage.db"

    conn = connect(path)
    apply_signal(conn, _sig("open"), now=T0)
    conn.close()

    conn = connect(path)
    runs = active_outages(conn, now=T0 + timedelta(days=1))
    assert len(runs) == 1
    # And it still closes, days later, with the original ticket long gone.
    apply_signal(conn, _sig("clear", ticket_id=999), now=T0 + timedelta(days=9))
    assert active_outages(conn, now=T0 + timedelta(days=9)) == []
    conn.close()


def test_signal_without_a_timestamp_is_refused(db):
    signal = _sig("open")
    signal.observed_at = None
    apply_signal(db, signal, now=None)
    assert active_outages(db, now=T0) == []
