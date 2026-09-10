#!/usr/bin/env python3
"""Tests for cron/behavior_store.py -- the durable behavior store."""
import json

import pytest

from cron import behavior_store as store

JERRY = "6a04ad0e-d7dd-4892-9109-c2a1e8025517"
JARVIS = "6ff84f43-2fac-4366-926d-382cb712deae"
ERNESTO = "010a8823-1689-473a-8cde-3a26c406beae"
SCARLET = "207bf2b6-54e5-42a5-917c-e6f76eb2c0e4"
ALLOWED_CSV = f"{JERRY},{JARVIS},{ERNESTO},{SCARLET}"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "behavior.db"


@pytest.fixture(autouse=True)
def allow_all(monkeypatch):
    monkeypatch.setenv("TEAMS_ALLOWED_USERS", ALLOWED_CSV)


def test_pending_proposal_does_not_render(db_path):
    p = store.propose(
        "knob", "set trend alert min tickets to 7", scope="triage",
        key="trend.alert_min_tickets", value=7, requested_by="ticket-text",
        now="2026-08-01T00:00:00+00:00", db_path=db_path,
    )
    assert p["status"] == "pending"
    rendered = store.render_active_rules(db_path=db_path)
    assert "trend.alert_min_tickets" not in rendered
    assert store.count_active(db_path=db_path) == 0

    store.approve(p["id"], approved_by=ERNESTO, now="2026-08-01T00:01:00+00:00", db_path=db_path)
    rendered = store.render_active_rules(db_path=db_path)
    assert "trend.alert_min_tickets = 7" in rendered
    assert store.count_active(db_path=db_path) == 1


def test_unauthorized_approver_rejected(db_path):
    p = store.propose(
        "instruction", "never post cards after 6pm", scope="cards",
        requested_by="anyone", now="2026-08-01T00:00:00+00:00", db_path=db_path,
    )
    with pytest.raises(PermissionError):
        store.approve(p["id"], approved_by="random-user-id", db_path=db_path)
    row = store.history(limit=1, db_path=db_path)[0]
    assert row["status"] == "pending"


def test_fail_closed_when_env_unset(db_path, monkeypatch):
    monkeypatch.delenv("TEAMS_ALLOWED_USERS", raising=False)
    p = store.propose(
        "instruction", "escalate P1s immediately", scope="triage",
        requested_by="anyone", now="2026-08-01T00:00:00+00:00", db_path=db_path,
    )
    for approver in (JERRY, JARVIS, ERNESTO, SCARLET):
        with pytest.raises(PermissionError):
            store.approve(p["id"], approved_by=approver, db_path=db_path)


def test_knob_supersession_leaves_one_active(db_path):
    p1 = store.propose(
        "knob", "min tickets 5", scope="triage", key="trend.alert_min_tickets",
        value=5, requested_by=JARVIS, now="2026-08-01T00:00:00+00:00", db_path=db_path,
    )
    store.approve(p1["id"], approved_by=ERNESTO, now="2026-08-01T00:01:00+00:00", db_path=db_path)

    p2 = store.propose(
        "knob", "min tickets 9", scope="triage", key="trend.alert_min_tickets",
        value=9, requested_by=JARVIS, now="2026-08-02T00:00:00+00:00", db_path=db_path,
    )
    store.approve(p2["id"], approved_by=SCARLET, now="2026-08-02T00:01:00+00:00", db_path=db_path)

    active = [r for r in store.history(key="trend.alert_min_tickets", db_path=db_path) if r["active"]]
    assert len(active) == 1
    assert active[0]["value"] == 9

    all_history = store.history(key="trend.alert_min_tickets", db_path=db_path)
    assert len(all_history) == 2
    retired = [r for r in all_history if r["id"] == p1["id"]][0]
    assert retired["status"] == "retired"
    assert retired["superseded_by"] == p2["id"]


def test_unknown_knob_key_rejected(db_path):
    with pytest.raises(ValueError, match="unknown knob key"):
        store.propose(
            "knob", "bogus setting", scope="triage", key="not.a.real.knob",
            value=1, requested_by=JARVIS, db_path=db_path,
        )


def test_knob_value_out_of_range_rejected(db_path):
    with pytest.raises(ValueError, match="must be"):
        store.propose(
            "knob", "min tickets absurd", scope="triage", key="trend.alert_min_tickets",
            value=99999, requested_by=JARVIS, db_path=db_path,
        )


def test_near_duplicate_instruction_returns_existing(db_path):
    p1 = store.propose(
        "instruction", "Always escalate security tickets within 1 hour.",
        scope="triage", requested_by=JARVIS, now="2026-08-01T00:00:00+00:00", db_path=db_path,
    )
    store.approve(p1["id"], approved_by=ERNESTO, now="2026-08-01T00:01:00+00:00", db_path=db_path)

    p2 = store.propose(
        "instruction", "  always ESCALATE security tickets   within 1 hour.  ",
        scope="triage", requested_by=SCARLET, db_path=db_path,
    )
    assert p2["id"] == p1["id"]
    assert store.count_active(db_path=db_path) == 1


def test_render_active_rules_respects_cap_and_names_omitted(db_path):
    for i in range(20):
        p = store.propose(
            "instruction", f"lesson number {i} about ticket triage behavior",
            scope="triage", requested_by=JARVIS, now="2026-08-01T00:00:00+00:00", db_path=db_path,
        )
        store.approve(p["id"], approved_by=ERNESTO, now="2026-08-01T00:01:00+00:00", db_path=db_path)

    rendered = store.render_active_rules(max_chars=300, db_path=db_path)
    assert len(rendered) <= 300
    last_line = rendered.splitlines()[-1]
    assert "more rule(s) omitted" in last_line


def test_five_hundred_active_rules_stay_bounded(db_path):
    for i in range(500):
        p = store.propose(
            "instruction", f"distinct learned fact number {i} from an IT ticket",
            scope="global", requested_by=JARVIS, now="2026-08-01T00:00:00+00:00", db_path=db_path,
        )
        store.approve(p["id"], approved_by=ERNESTO, now="2026-08-01T00:01:00+00:00", db_path=db_path)

    assert store.count_active(db_path=db_path) == 500
    rendered = store.render_active_rules(max_chars=2000, db_path=db_path)
    assert len(rendered) <= 2000
    assert "more rule(s) omitted" in rendered.splitlines()[-1]


def test_history_survives_supersession(db_path):
    p1 = store.propose(
        "knob", "quiet hours start 20", scope="global", key="quiet_hours.start",
        value=20, requested_by=JARVIS, now="2026-08-01T00:00:00+00:00", db_path=db_path,
    )
    store.approve(p1["id"], approved_by=ERNESTO, now="2026-08-01T00:01:00+00:00", db_path=db_path)
    p2 = store.propose(
        "knob", "quiet hours start 21", scope="global", key="quiet_hours.start",
        value=21, requested_by=JARVIS, now="2026-08-02T00:00:00+00:00", db_path=db_path,
    )
    store.approve(p2["id"], approved_by=SCARLET, now="2026-08-02T00:01:00+00:00", db_path=db_path)

    hist = store.history(key="quiet_hours.start", db_path=db_path)
    assert len(hist) == 2


def test_retire_requires_authorization(db_path):
    p = store.propose(
        "instruction", "post closed-ticket cards silently", scope="cards",
        requested_by=JARVIS, now="2026-08-01T00:00:00+00:00", db_path=db_path,
    )
    store.approve(p["id"], approved_by=ERNESTO, now="2026-08-01T00:01:00+00:00", db_path=db_path)

    with pytest.raises(PermissionError):
        store.retire(p["id"], by="random-user", reason="no longer wanted", db_path=db_path)

    store.retire(p["id"], by=SCARLET, reason="no longer wanted", now="2026-08-03T00:00:00+00:00", db_path=db_path)
    assert store.count_active(db_path=db_path) == 0
    row = store.history(limit=1, db_path=db_path)[0]
    assert row["status"] == "retired"
    assert row["retired_at"] == "2026-08-03T00:00:00+00:00"


def test_schema_creation_idempotent(db_path):
    store.count_active(db_path=db_path)  # opens once, creates schema
    store.count_active(db_path=db_path)  # opens again, must not error/duplicate
    p = store.propose(
        "instruction", "idempotency smoke test", scope="global",
        requested_by=JARVIS, db_path=db_path,
    )
    assert p["id"] == 1  # AUTOINCREMENT unaffected by the double-open


def test_validate_knob_reports_closest_keys():
    ok, msg = store.validate_knob("trend.alert_min_ticket", 5)
    assert ok is False
    assert "trend.alert_min_tickets" in msg


def test_reject_closes_out_pending_without_rendering(db_path):
    p = store.propose(
        "instruction", "reject me", scope="global", requested_by=JARVIS, db_path=db_path,
    )
    store.reject(p["id"], rejected_by=ERNESTO, reason="not useful", db_path=db_path)
    assert store.count_active(db_path=db_path) == 0
    row = store.history(limit=1, db_path=db_path)[0]
    assert row["status"] == "rejected"
    with pytest.raises(ValueError, match="not pending"):
        store.approve(p["id"], approved_by=ERNESTO, db_path=db_path)
