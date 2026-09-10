"""
Unit tests for the ConnectWise ticket dedupe/idempotency guard in
cron/trend_ticket.py (trend_fingerprint, ensure_trend_ticket, and the
ledger persisted alongside trend_alert_state.json).

CWClient is fully mocked throughout -- these tests never make a real
ConnectWise API call.
"""
import json
import logging
from unittest.mock import MagicMock

import pytest

from cron.trend_ticket import (
    ensure_trend_ticket,
    load_ledger,
    save_ledger,
    trend_fingerprint,
)


def _trend(entity_text: str = "Printer offline across finance floor", **overrides) -> dict:
    # entity_text stands in for each member ticket's own summary/evidence
    # text (finalize_trend()'s real "tickets": [{"summary":..., "evidence":
    # ...}] shape) -- the raw, non-LLM-authored signal trend_fingerprint()
    # now extracts its entity/symptom core from. Kept separate from the
    # top-level "summary" convenience key below, which stands in for
    # trend["title"] -- Stage B LLM prose that must NOT affect the
    # fingerprint (see test_reworded_llm_summary_does_not_change_fingerprint).
    base = {
        "trend_id": "trend-abc",
        "summary": "Printer offline across finance floor",
        "ticket_count": 3,
        "device_count": 2,
        "ticket_ids": [101, 102, 103],
    }
    base.update(overrides)
    if "tickets" not in base:
        base["tickets"] = [
            {"id": tid, "summary": entity_text, "evidence": ""}
            for tid in base.get("ticket_ids") or []
        ]
    return base


class TestTrendFingerprint:
    def test_stable_across_growing_count_and_shifted_ticket_ids(self):
        day1 = _trend(ticket_count=3, ticket_ids=[101, 102, 103])
        day2 = _trend(ticket_count=7, ticket_ids=[101, 102, 103, 104, 105, 106, 107])
        assert trend_fingerprint(day1) == trend_fingerprint(day2)

    def test_stable_across_grown_count_and_shifted_date_range(self):
        # Same founding tickets, but the trend has picked up more members
        # and its date range has widened -- both change day to day for an
        # ongoing trend and must not move the fingerprint.
        day1 = _trend(ticket_count=3, ticket_ids=[101, 102, 103], first_seen="2026-08-01", last_seen="2026-08-03")
        day2 = _trend(
            ticket_count=9, ticket_ids=[101, 102, 103, 104, 105, 106, 107, 108, 109],
            first_seen="2026-08-01", last_seen="2026-08-14",
        )
        assert trend_fingerprint(day1) == trend_fingerprint(day2)

    def test_reworded_llm_summary_does_not_change_fingerprint(self):
        # Regression test for the defect: Stage B (trend_cluster_semantic.py)
        # authors trend["summary"] with an LLM, which rewords the same
        # ongoing trend differently run to run. The fingerprint must be
        # keyed on the founding ticket ids, not the prose.
        day1 = _trend(ticket_ids=[201, 202, 203], summary="Accounting printer outage")
        day2 = _trend(ticket_ids=[201, 202, 203, 204], summary="Printer failures on the accounting floor")
        assert trend_fingerprint(day1) == trend_fingerprint(day2)

    def test_different_trend_produces_different_fingerprint(self):
        # Genuinely different trends never share entity/symptom cores, so
        # distinct entities/symptoms must produce distinct fingerprints
        # even with unrelated founding ticket ids.
        a = _trend(ticket_ids=[301, 302, 303], entity_text="Printer offline across finance floor")
        b = _trend(ticket_ids=[401, 402, 403], entity_text="VPN authentication failures spiking")
        assert trend_fingerprint(a) != trend_fingerprint(b)

    def test_reads_entity_signature_from_tickets_list_shape(self):
        # finalize_trend() (cron/trend_cluster_output.py) emits "tickets":
        # [{"id":..., "summary":..., "evidence":...}, ...] -- the real
        # production shape the entity/symptom signature is derived from.
        trend = {"tickets": [
            {"id": 501, "summary": "Printer offline across finance floor", "evidence": ""},
            {"id": 502, "summary": "Printer offline across finance floor", "evidence": ""},
        ]}
        assert trend_fingerprint(trend) == trend_fingerprint(_trend(ticket_ids=[501, 502]))

    def test_window_slide_founding_tickets_age_out_same_fingerprint(self):
        # THE KEY CASE: cron.trend.window_days (cron/trend_pass.py) is a
        # rolling 21-day window. A trend that outlives it has its founding
        # tickets eventually age out of the window entirely -- the cluster
        # re-forms around a new earliest ticket id even though the trend's
        # entity/symptom core (what it's actually ABOUT) hasn't changed.
        # Day 1: tickets 100-102. Day 30: same ongoing trend, but the
        # founding tickets are gone and it's now built from 140-142. The
        # entity/symptom core is identical both days, so the fingerprint
        # must be too -- this is exactly what a founding-ticket-id
        # fingerprint (the pre-fix behavior) gets wrong.
        day1 = _trend(ticket_ids=[100, 101, 102], entity_text="Printer offline across finance floor")
        day30 = _trend(ticket_ids=[140, 141, 142], entity_text="Printer offline across finance floor")
        assert trend_fingerprint(day1) == trend_fingerprint(day30)


class TestLedgerPersistence:
    def test_missing_ledger_starts_empty(self, tmp_path):
        assert load_ledger(tmp_path / "trend_ticket_ledger.json") == {}

    def test_corrupt_ledger_starts_empty_not_crash(self, tmp_path):
        p = tmp_path / "trend_ticket_ledger.json"
        p.write_text("{not valid json", encoding="utf-8")
        assert load_ledger(p) == {}

    def test_save_is_atomic_and_leaves_no_temp_file(self, tmp_path):
        p = tmp_path / "trend_ticket_ledger.json"
        save_ledger({"fp1": {"connectwise_ticket_id": 55, "created_at_iso": "x", "last_seen_iso": "y"}}, p)
        assert p.exists()
        assert list(tmp_path.glob("*.tmp")) == []
        reloaded = load_ledger(p)
        assert reloaded["fp1"]["connectwise_ticket_id"] == 55

    def test_legacy_scheme_entries_are_flagged_not_silently_ignored(self, tmp_path, caplog):
        # A ledger entry written before FINGERPRINT_SCHEME_VERSION=2 (keyed
        # on hashed summary tokens) can never be reached by a v2, ticket-id
        # based fingerprint. load_ledger() must not treat that key as
        # equivalent -- and must not silently drop or rewrite it -- it must
        # say so loudly so an operator knows a duplicate ticket is coming.
        p = tmp_path / "trend_ticket_ledger.json"
        legacy_entry = {"connectwise_ticket_id": 42, "created_at_iso": "x", "last_seen_iso": "y"}
        p.write_text(json.dumps({"deadbeefcafebabe": legacy_entry}), encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            ledger = load_ledger(p)

        assert ledger == {"deadbeefcafebabe": legacy_entry}  # left intact, not mutated or dropped
        assert any("fingerprint" in rec.message.lower() for rec in caplog.records)

    def test_v2_scheme_entries_are_flagged_not_silently_ignored(self, tmp_path, caplog):
        # v2 (founding-ticket-id) entries are just as unreachable under v3
        # (entity/symptom signature) as pre-v2 entries were under v2 -- the
        # same loud-warning contract must hold for the v2->v3 boundary.
        p = tmp_path / "trend_ticket_ledger.json"
        v2_entry = {
            "connectwise_ticket_id": 42, "created_at_iso": "x", "last_seen_iso": "y",
            "fingerprint_scheme_version": 2,
        }
        p.write_text(json.dumps({"deadbeefcafebabe": v2_entry}), encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            ledger = load_ledger(p)

        assert ledger == {"deadbeefcafebabe": v2_entry}
        assert any("fingerprint" in rec.message.lower() for rec in caplog.records)


class TestEnsureTrendTicket:
    def test_same_trend_two_consecutive_days_creates_once(self, tmp_path):
        ledger_path = tmp_path / "trend_ticket_ledger.json"
        client = MagicMock()
        client.create_ticket.return_value = {"id": 999}

        day1 = _trend(ticket_count=3, ticket_ids=[101, 102, 103])
        result1 = ensure_trend_ticket(day1, client, ledger_path=ledger_path)
        assert result1["created"] is True
        assert result1["ticket_id"] == 999
        assert client.create_ticket.call_count == 1

        day2 = _trend(ticket_count=5, ticket_ids=[101, 102, 103, 104, 105])
        result2 = ensure_trend_ticket(day2, client, ledger_path=ledger_path)
        assert result2["created"] is False
        assert result2["already_existed"] is True
        assert result2["ticket_id"] == 999
        assert client.create_ticket.call_count == 1

    def test_reworded_summary_still_creates_ticket_only_once(self, tmp_path):
        # End-to-end regression test for the defect: the same ongoing trend,
        # reworded by the LLM naming stage between two runs, must resolve to
        # the SAME fingerprint and therefore hit ConnectWise exactly once.
        ledger_path = tmp_path / "trend_ticket_ledger.json"
        client = MagicMock()
        client.create_ticket.return_value = {"id": 777}

        day1 = _trend(ticket_ids=[601, 602, 603], summary="Accounting printer outage")
        result1 = ensure_trend_ticket(day1, client, ledger_path=ledger_path)
        assert result1["created"] is True

        day2 = _trend(ticket_ids=[601, 602, 603, 604], summary="Printer failures on the accounting floor")
        result2 = ensure_trend_ticket(day2, client, ledger_path=ledger_path)
        assert result2["created"] is False
        assert result2["already_existed"] is True
        assert result2["ticket_id"] == 777
        assert client.create_ticket.call_count == 1

    def test_window_slide_creates_ticket_only_once(self, tmp_path):
        # End-to-end version of test_window_slide_founding_tickets_age_out_
        # same_fingerprint: a trend that outlives the 21-day window and
        # re-forms around new founding ticket ids must still resolve to the
        # existing ledger entry, not open a second CW ticket.
        ledger_path = tmp_path / "trend_ticket_ledger.json"
        client = MagicMock()
        client.create_ticket.return_value = {"id": 888}

        day1 = _trend(ticket_ids=[100, 101, 102], entity_text="Printer offline across finance floor")
        result1 = ensure_trend_ticket(day1, client, ledger_path=ledger_path)
        assert result1["created"] is True

        day30 = _trend(ticket_ids=[140, 141, 142], entity_text="Printer offline across finance floor")
        result2 = ensure_trend_ticket(day30, client, ledger_path=ledger_path)
        assert result2["created"] is False
        assert result2["already_existed"] is True
        assert result2["ticket_id"] == 888
        assert client.create_ticket.call_count == 1

    def test_dry_run_never_calls_create_ticket(self, tmp_path):
        ledger_path = tmp_path / "trend_ticket_ledger.json"
        client = MagicMock()

        result = ensure_trend_ticket(_trend(), client, ledger_path=ledger_path, dry_run=True)

        client.create_ticket.assert_not_called()
        assert result["created"] is False
        assert result["dry_run"] is True
        # dry run must not write a ledger entry either
        assert load_ledger(ledger_path) == {}

    def test_corrupt_ledger_does_not_crash_ensure_trend_ticket(self, tmp_path):
        ledger_path = tmp_path / "trend_ticket_ledger.json"
        ledger_path.write_text("{broken", encoding="utf-8")
        client = MagicMock()
        client.create_ticket.return_value = {"id": 1}

        result = ensure_trend_ticket(_trend(), client, ledger_path=ledger_path)

        assert result["created"] is True
        assert client.create_ticket.call_count == 1

    def test_api_error_surfaces_and_ledger_not_polluted(self, tmp_path):
        ledger_path = tmp_path / "trend_ticket_ledger.json"
        client = MagicMock()
        client.create_ticket.side_effect = RuntimeError("CW POST failed: 500")

        with pytest.raises(RuntimeError):
            ensure_trend_ticket(_trend(), client, ledger_path=ledger_path)

        assert load_ledger(ledger_path) == {}
