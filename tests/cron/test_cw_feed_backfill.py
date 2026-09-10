"""
Unit tests for cron/scripts/cw_feed_backfill.py - the poll-based fallback
that closes gaps in logs/cw_callback_payloads.jsonl during a callback
outage (see the module docstring for the 2026-08-26..2026-09-01 outage
this was built for).

No live network calls. CWClient is mocked throughout.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from cron.scripts.cw_feed_backfill import (
    build_backfill_record,
    compute_default_since,
    load_latest_updated_by_id,
    select_new_or_updated,
    run,
)
from cron.trend_detection import load_tickets_from_cw_log


def _rec(ts: str, entity: dict) -> dict:
    return {
        "ts": ts,
        "payload": {
            "MessageId": "m-1",
            "FromUrl": "na.myconnectwise.net",
            "CompanyId": "henssler",
            "MemberId": "AgentPenny",
            "Action": "added",
            "Type": "ticket",
            "ID": entity["id"],
            "ProductInstanceId": "prod-1",
            "PartnerId": "partner-1",
            "Entity": json.dumps(entity),
            "Metadata": {},
        },
    }


def _entity(ticket_id: int, last_updated: str, board: str = "Triage") -> dict:
    return {
        "id": ticket_id,
        "summary": f"ticket {ticket_id}",
        "contactName": "Jane Doe",
        "status": {"name": "New"},
        "closedFlag": False,
        "priority": {"name": "Priority 3 - Medium"},
        "board": {"name": board},
        "_info": {
            "dateEntered": "2026-08-20T00:00:00Z",
            "lastUpdated": last_updated,
        },
    }


def _write_jsonl(path, records):
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class TestComputeDefaultSince:
    def test_default_since_is_newest_ts_minus_five_minutes(self, tmp_path):
        log_path = tmp_path / "cw_callback_payloads.jsonl"
        _write_jsonl(
            log_path,
            [
                _rec("2026-08-25T00:00:00Z", _entity(1, "2026-08-25T00:00:00Z")),
                _rec("2026-08-26T03:02:43Z", _entity(2, "2026-08-26T03:02:40Z")),
            ],
        )
        since = compute_default_since(log_path)
        assert since == datetime(2026, 8, 26, 2, 57, 43, tzinfo=timezone.utc)

    def test_unparseable_lines_are_skipped_not_fatal(self, tmp_path):
        log_path = tmp_path / "cw_callback_payloads.jsonl"
        log_path.write_text(
            "not json at all\n"
            + json.dumps(_rec("2026-08-26T03:02:43Z", _entity(2, "2026-08-26T03:02:40Z"))) + "\n"
        )
        since = compute_default_since(log_path)
        assert since == datetime(2026, 8, 26, 2, 57, 43, tzinfo=timezone.utc)

    def test_missing_log_falls_back_to_one_hour_ago(self, tmp_path):
        log_path = tmp_path / "does_not_exist.jsonl"
        before = datetime.now(timezone.utc) - timedelta(hours=1, minutes=6)
        since = compute_default_since(log_path)
        after = datetime.now(timezone.utc) - timedelta(hours=1, minutes=4)
        assert before <= since <= after


class TestIdempotentSkip:
    def test_ticket_already_at_or_past_known_lastupdated_is_skipped(self, tmp_path):
        log_path = tmp_path / "cw_callback_payloads.jsonl"
        _write_jsonl(log_path, [_rec("2026-08-27T00:00:00Z", _entity(96695, "2026-08-27T00:00:00Z"))])
        latest_by_id = load_latest_updated_by_id(log_path)

        same_ticket = _entity(96695, "2026-08-27T00:00:00Z")
        older_fetch = _entity(96695, "2026-08-20T00:00:00Z")
        newer_fetch = _entity(96695, "2026-08-28T00:00:00Z")
        unseen_ticket = _entity(96697, "2026-08-28T00:00:00Z")

        kept = select_new_or_updated(
            [same_ticket, older_fetch, newer_fetch, unseen_ticket], latest_by_id
        )
        kept_ids_with_updated = {(t["id"], t["_info"]["lastUpdated"]) for t in kept}
        assert kept_ids_with_updated == {
            (96695, "2026-08-28T00:00:00Z"),
            (96697, "2026-08-28T00:00:00Z"),
        }

    def test_unknown_ticket_id_is_never_skipped(self, tmp_path):
        log_path = tmp_path / "cw_callback_payloads.jsonl"
        _write_jsonl(log_path, [])
        latest_by_id = load_latest_updated_by_id(log_path)
        fresh = _entity(1, "2026-08-28T00:00:00Z")
        assert select_new_or_updated([fresh], latest_by_id) == [fresh]


class TestRecordShapeRoundTrips:
    def test_built_record_round_trips_through_the_real_loader(self, tmp_path):
        ticket = _entity(96695, "2026-08-27T12:00:00Z")
        record = build_backfill_record(ticket)

        log_path = tmp_path / "cw_callback_payloads.jsonl"
        log_path.write_text(json.dumps(record) + "\n")

        loaded = load_tickets_from_cw_log(log_path)
        assert len(loaded) == 1
        assert loaded[0].id == 96695
        assert loaded[0].board == "Triage"
        assert loaded[0].priority == "Priority 3 - Medium"

    def test_record_is_marked_as_a_backfill(self):
        record = build_backfill_record(_entity(1, "2026-08-27T12:00:00Z"))
        assert record["payload"]["Action"] == "backfill"
        assert record["payload"]["Metadata"]["source"] == "cw_feed_backfill"
        assert record["ts"] == "2026-08-27T12:00:00Z"


class TestDryRun:
    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        log_path = tmp_path / "cw_callback_payloads.jsonl"
        _write_jsonl(log_path, [_rec("2026-08-25T00:00:00Z", _entity(1, "2026-08-25T00:00:00Z"))])
        original_contents = log_path.read_text()

        fake_client = MagicMock()
        fake_client.paged.return_value = [_entity(2, "2026-08-28T00:00:00Z")]

        import cron.scripts.cw_feed_backfill as backfill_module
        backfill_module.CWClient = MagicMock(return_value=fake_client)

        since = datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc)
        exit_code = run("Triage", since, apply=False, path=log_path)

        assert exit_code == 0
        assert log_path.read_text() == original_contents
        out = capsys.readouterr().out
        assert "dry run" in out
        assert "would append 1 record" in out

    def test_apply_appends_and_backs_up(self, tmp_path, capsys):
        log_path = tmp_path / "cw_callback_payloads.jsonl"
        _write_jsonl(log_path, [_rec("2026-08-25T00:00:00Z", _entity(1, "2026-08-25T00:00:00Z"))])

        fake_client = MagicMock()
        fake_client.paged.return_value = [_entity(2, "2026-08-28T00:00:00Z")]

        import cron.scripts.cw_feed_backfill as backfill_module
        backfill_module.CWClient = MagicMock(return_value=fake_client)

        since = datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc)
        exit_code = run("Triage", since, apply=True, path=log_path)

        assert exit_code == 0
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        appended = json.loads(lines[1])
        assert appended["payload"]["ID"] == 2

        backups = list(tmp_path.glob("cw_callback_payloads.jsonl.bak.*"))
        assert len(backups) == 1

        out = capsys.readouterr().out
        assert "appended 1 record(s)" in out
