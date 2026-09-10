#!/usr/bin/env python3
"""Tests for cron/outage_dry_run.py - the accuracy gate that also gives
cron/outage_store.active_outages() its first production caller.

Before this module, active_outages() had zero callers outside tests: outage
signals were written to the store and never read back, so a tracked outage
never informed anything. These tests exercise that wiring (a ticket whose
routed service_key matches an already-active run gets annotated as a
continuation) without touching the network - CWClient and the outage db
connection are both faked.
"""
from __future__ import annotations

import pytest

from cron.outage_dry_run import run


class FakeCW:
    def __init__(self, tickets_by_query):
        self.tickets_by_query = tickets_by_query

    def paged(self, path, conditions, page_size=1000):
        if path == "/company/configurations":
            return []
        if "closedFlag=true" in conditions:
            return self.tickets_by_query.get("history", [])
        return self.tickets_by_query.get("open", [])

    def send(self, method, path, payload):
        raise AssertionError("outage_dry_run must never write to ConnectWise")


def _ticket(ticket_id, summary, contact=None):
    t = {"id": ticket_id, "summary": summary, "board": {"name": "Triage"},
         "_info": {"dateEntered": "2026-08-28T09:00:00Z"}}
    if contact:
        t["contact"] = {"name": contact}
    return t


def test_dry_run_never_calls_cw_send(monkeypatch):
    """The whole point of the accuracy gate: it decides, it never writes."""
    fake = FakeCW({"open": [_ticket(1, "Anything")], "history": []})
    monkeypatch.setattr("cron.outage_dry_run.CWClient", lambda: fake)
    monkeypatch.setattr("cron.outage_dry_run.connect_outage_db", lambda: _FakeDB([]))
    table = run()
    assert len(table) == 1
    # FakeCW.send would raise if apply_plan ever tried to write; run()
    # completing without raising is itself the assertion.


def test_apply_true_is_refused_at_the_call_site():
    with pytest.raises(AssertionError):
        run(apply=True)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows
        return _Cursor(self._rows)

    def close(self):
        pass


def test_a_ticket_matching_an_active_tracked_outage_is_flagged_as_a_continuation(monkeypatch):
    """This is the wiring itself: active_outages() feeding route_ticket's
    output rather than sitting unread. sharepoint_online is already tracked
    (run id 42); a fresh SharePoint incident on Triage should surface that
    run id instead of looking like an unrelated first sighting."""
    ticket = _ticket(
        96184,
        "SP1461217: SharePoint Online Service Health Incident (Service Degradation)",
    )
    fake_cw = FakeCW({"open": [ticket], "history": []})
    monkeypatch.setattr("cron.outage_dry_run.CWClient", lambda: fake_cw)

    active_row = {"id": 42, "service_key": "sharepoint_online", "status": "open"}
    monkeypatch.setattr("cron.outage_dry_run.connect_outage_db", lambda: _FakeDB([active_row]))
    monkeypatch.setattr("cron.outage_dry_run.active_outages", lambda conn: [active_row] if conn else [])

    # build_registry needs SharePoint Online resolvable; give it a minimal
    # configuration row that outage_inventory recognizes as a service type.
    monkeypatch.setattr(
        "cron.outage_dry_run.build_registry",
        lambda configurations: {
            "sharepoint_online": type(
                "Entry", (), {"key": "sharepoint_online", "name": "SharePoint Online",
                              "aliases": frozenset({"sharepoint online", "sharepoint"}),
                              "config_ids": (), "tracked": True, "suppressed_reason": "",
                              "parent_key": ""},
            )(),
        },
    )

    table = run()
    assert len(table) == 1
    assert table[0]["already_tracked_run_id"] == 42
