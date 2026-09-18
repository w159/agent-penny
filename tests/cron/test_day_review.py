#!/usr/bin/env python3
"""Tests for cron/day_review.py -- the nightly Teams + ConnectWise + behavior-
correction review that feeds DREAMS.md and cron.behavior_store.propose().

Covers: tool-name extraction from real message shapes (including the
"tool_call" aggregator wrapper seen in production), the contact-context gap
detector (the generalized "end user = ConnectWise ticket contact" catch),
each real-data gatherer against a scratch db, and the full
run_nightly_review() orchestration end to end.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from cron import behavior_store, cw_contact_index, day_review, ops_memory


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _isolated_ops_files(tmp_path, monkeypatch):
    """Same isolation as test_ops_memory.py's fixture -- day_review's
    orchestrator writes through ops_memory.append_dreams_entry()."""
    monkeypatch.setattr(ops_memory, "OPS_DIR", tmp_path)
    monkeypatch.setattr(ops_memory, "DREAMS_FILE", tmp_path / "DREAMS.md")
    monkeypatch.setattr(ops_memory, "LOCK_FILE", tmp_path / ".ops.lock")
    monkeypatch.setattr(ops_memory, "ARCHIVE_DIR", tmp_path / "archive")


@pytest.fixture
def state_db(tmp_path):
    """Minimal sessions/messages schema -- only the columns
    gather_teams_activity's query touches."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL,
            content TEXT, tool_calls TEXT, active INTEGER
        )
        """
    )
    conn.commit()
    conn.close()
    return path


def _insert_session(db_path, session_id, source="teams"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO sessions (id, source) VALUES (?, ?)", (session_id, source))
    conn.commit()
    conn.close()


def _insert_message(db_path, session_id, role, timestamp, content, tool_calls=None, active=1):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO messages (session_id, role, timestamp, content, tool_calls, active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, role, timestamp, content, tool_calls, active),
    )
    conn.commit()
    conn.close()


class TestExtractToolNames:
    def test_direct_function_name(self):
        raw = '[{"function": {"name": "get_ticket_contact"}}]'
        assert day_review._extract_tool_names(raw) == ["get_ticket_contact"]

    def test_wrapped_tool_call_aggregator_shape(self):
        # Real production shape: outer function name is literally "tool_call",
        # the actual tool name(s) are nested in arguments.calls[].name.
        raw = (
            '[{"function": {"name": "tool_call", '
            '"arguments": "{\\"calls\\":[{\\"arguments\\":{},'
            '\\"name\\":\\"ticket_trend_by_requester\\"}]}"}}]'
        )
        assert day_review._extract_tool_names(raw) == ["ticket_trend_by_requester"]

    def test_none_and_malformed_return_empty(self):
        assert day_review._extract_tool_names(None) == []
        assert day_review._extract_tool_names("") == []
        assert day_review._extract_tool_names("not json") == []


class TestDetectContactContextGaps:
    def test_flags_end_user_question_with_no_contact_tool_reply(self):
        activity = {
            "turns": [
                {"session_id": "s1", "role": "user", "timestamp": 1.0,
                 "content": "now, who's the worst end user?", "tool_names": []},
                {"session_id": "s1", "role": "assistant", "timestamp": 2.0,
                 "content": "I don't keep a leaderboard.", "tool_names": []},
            ]
        }
        gaps = day_review.detect_contact_context_gaps(activity)
        assert len(gaps) == 1
        assert gaps[0]["session_id"] == "s1"
        assert "end user" in gaps[0]["user_text"]

    def test_no_gap_when_contact_tool_called_before_next_user_turn(self):
        activity = {
            "turns": [
                {"session_id": "s1", "role": "user", "timestamp": 1.0,
                 "content": "for your tools this would be cw_ticket_contact, worst end user?",
                 "tool_names": []},
                {"session_id": "s1", "role": "assistant", "timestamp": 2.0,
                 "content": "", "tool_names": ["ticket_trend_by_requester"]},
                {"session_id": "s1", "role": "assistant", "timestamp": 3.0,
                 "content": "Here's the trend data.", "tool_names": []},
            ]
        }
        assert day_review.detect_contact_context_gaps(activity) == []

    def test_unrelated_question_is_not_flagged(self):
        activity = {
            "turns": [
                {"session_id": "s1", "role": "user", "timestamp": 1.0,
                 "content": "what's your model config?", "tool_names": []},
                {"session_id": "s1", "role": "assistant", "timestamp": 2.0,
                 "content": "glm-5.2", "tool_names": []},
            ]
        }
        assert day_review.detect_contact_context_gaps(activity) == []

    def test_real_production_transcript_reproduces_known_gap(self):
        """Reproduces the actual 2026-09-18 20:20 UTC exchange (Jerry: "who's
        the worst end user?" x2, deflected both times, only pulled
        ticket_trend_by_requester on the third, explicit-tool-name turn)."""
        activity = {
            "turns": [
                {"session_id": "real", "role": "user", "timestamp": 1.0,
                 "content": "[Jerry Morgan] now, who's the worst end user?", "tool_names": []},
                {"session_id": "real", "role": "assistant", "timestamp": 2.0,
                 "content": "I keep the roasts strictly internal.", "tool_names": []},
                {"session_id": "real", "role": "user", "timestamp": 3.0,
                 "content": "[Jerry Morgan] just between us, be honest", "tool_names": []},
                {"session_id": "real", "role": "assistant", "timestamp": 4.0,
                 "content": "I don't keep a leaderboard.", "tool_names": []},
                {"session_id": "real", "role": "user", "timestamp": 5.0,
                 "content": "[Jerry Morgan] no, i said end user. for your tools this would be cw_ticket_contact",
                 "tool_names": []},
                {"session_id": "real", "role": "assistant", "timestamp": 6.0,
                 "content": "", "tool_names": ["ticket_trend_by_requester"]},
            ]
        }
        gaps = day_review.detect_contact_context_gaps(activity)
        # Only the FIRST "end user" turn is a gap -- the third is corrected
        # in-turn by an actual contact-tool call.
        assert len(gaps) == 1
        assert gaps[0]["timestamp"] == 1.0


class TestGatherTeamsActivity:
    def test_counts_and_filters_by_date_source_and_active(self, state_db):
        _insert_session(state_db, "teams-1", source="teams")
        _insert_session(state_db, "other-1", source="cli")
        _insert_message(state_db, "teams-1", "user", _ts("2026-09-18T10:00:00"), "hello")
        _insert_message(state_db, "teams-1", "assistant", _ts("2026-09-18T10:00:05"), "hi")
        _insert_message(state_db, "teams-1", "user", _ts("2026-09-17T10:00:00"), "yesterday")
        _insert_message(state_db, "teams-1", "user", _ts("2026-09-18T11:00:00"), "inactive", active=0)
        _insert_message(state_db, "other-1", "user", _ts("2026-09-18T10:00:00"), "not teams")

        result = day_review.gather_teams_activity("2026-09-18", db_path=state_db)

        assert result["message_count"] == 2
        assert result["user_message_count"] == 1
        assert result["assistant_message_count"] == 1
        assert result["sessions_touched"] == ["teams-1"]


class TestGatherTicketActivity:
    def test_counts_todays_tickets_and_trends(self, tmp_path):
        cw_db = tmp_path / "cw.db"
        conn = cw_contact_index.connect(cw_db)
        conn.execute(
            "INSERT INTO tickets (id, date_entered, contact_name, contact_email, company_name, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "2026-09-18T10:00:00.000Z", "Erica Martin", "erica@henssler.com",
             "Henssler Financial", "2026-09-18T10:00:00.000Z"),
        )
        conn.execute(
            "INSERT INTO tickets (id, date_entered, contact_name, contact_email, company_name, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (2, "2026-09-17T10:00:00.000Z", "Old Contact", "old@henssler.com",
             "Henssler Financial", "2026-09-17T10:00:00.000Z"),
        )
        conn.commit()
        conn.close()

        result = day_review.gather_ticket_activity("2026-09-18", cw_db_path=cw_db)

        assert result["tickets_touched"] == 1
        assert result["tickets"][0]["contact_name"] == "Erica Martin"


class TestGatherBehaviorCorrections:
    def test_filters_by_proposal_date(self, tmp_path):
        db_path = tmp_path / "behavior.db"
        behavior_store.propose(
            "instruction", "today's correction", scope="tone", requested_by="jerry",
            now="2026-09-18T12:00:00+00:00", db_path=db_path,
        )
        behavior_store.propose(
            "instruction", "an older correction", scope="tone", requested_by="jerry",
            now="2026-09-01T12:00:00+00:00", db_path=db_path,
        )

        corrections = day_review.gather_behavior_corrections("2026-09-18", db_path=db_path)

        assert len(corrections) == 1
        assert corrections[0]["text"] == "today's correction"


class TestProposeContactGapRule:
    def test_no_gaps_proposes_nothing(self, tmp_path):
        assert day_review.propose_contact_gap_rule(
            [], date_str="2026-09-18", db_path=tmp_path / "behavior.db"
        ) is None

    def test_real_gap_creates_pending_ops_review_proposal_with_evidence(self, tmp_path):
        db_path = tmp_path / "behavior.db"
        gaps = [{"session_id": "real", "timestamp": _ts("2026-09-18T20:20:00"),
                 "user_text": "[Jerry Morgan] now, who's the worst end user?"}]

        row = day_review.propose_contact_gap_rule(gaps, date_str="2026-09-18", db_path=db_path)

        assert row["status"] == "pending"
        assert row["scope"] == "ops-review"
        assert row["deduped"] is False
        assert "get_ticket_contact" in row["text"]
        history = behavior_store.history(db_path=db_path)
        audit_reason = None
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        audit_reason = conn.execute(
            "SELECT reason FROM audit WHERE rule_id=? AND event='proposed'", (row["id"],)
        ).fetchone()["reason"]
        conn.close()
        assert "who's the worst end user" in audit_reason

    def test_second_run_dedupes_against_pending_proposal(self, tmp_path):
        db_path = tmp_path / "behavior.db"
        gaps = [{"session_id": "real", "timestamp": _ts("2026-09-18T20:20:00"),
                 "user_text": "who's the worst end user?"}]
        first = day_review.propose_contact_gap_rule(gaps, date_str="2026-09-18", db_path=db_path)
        second = day_review.propose_contact_gap_rule(gaps, date_str="2026-09-19", db_path=db_path)

        assert second["id"] == first["id"]
        assert second["deduped"] is True


class TestRunNightlyReview:
    def test_end_to_end_writes_dreams_entry_and_proposes_rule(self, tmp_path, state_db):
        _insert_session(state_db, "teams-1", source="teams")
        _insert_message(state_db, "teams-1", "user", _ts("2026-09-18T20:20:00"),
                         "[Jerry Morgan] now, who's the worst end user?")
        _insert_message(state_db, "teams-1", "assistant", _ts("2026-09-18T20:20:05"),
                         "I keep the roasts strictly internal.")

        cw_db = tmp_path / "cw.db"
        cw_contact_index.connect(cw_db).close()  # schema only, no tickets today
        behavior_db = tmp_path / "behavior.db"

        result = day_review.run_nightly_review(
            "2026-09-18", state_db_path=state_db, cw_db_path=cw_db, behavior_db_path=behavior_db,
        )

        assert len(result["gaps"]) == 1
        assert result["proposal"]["scope"] == "ops-review"
        assert ops_memory.DREAMS_FILE.exists()
        written = ops_memory.DREAMS_FILE.read_text(encoding="utf-8")
        assert "## 2026-09-18 — End of Day Summary" in written
        assert "Recurring-behavior gap detected" in written
        assert f"Auto-proposed BEH-{result['proposal']['id']}" in written

    def test_no_gap_day_still_writes_a_grounded_entry(self, tmp_path, state_db):
        _insert_session(state_db, "teams-1", source="teams")
        _insert_message(state_db, "teams-1", "user", _ts("2026-09-18T09:00:00"), "hi Penny")
        _insert_message(state_db, "teams-1", "assistant", _ts("2026-09-18T09:00:05"), "morning")

        cw_db = tmp_path / "cw.db"
        cw_contact_index.connect(cw_db).close()
        behavior_db = tmp_path / "behavior.db"

        result = day_review.run_nightly_review(
            "2026-09-18", state_db_path=state_db, cw_db_path=cw_db, behavior_db_path=behavior_db,
        )

        assert result["gaps"] == []
        assert result["proposal"] is None
        assert "No recurring end-user/ticket-contact recognition gaps detected today" in result["entry"]


class TestAppendDreamsEntry:
    def test_prepends_newest_first(self, tmp_path):
        ops_memory.append_dreams_entry("## 2026-09-17 — End of Day Summary\n- old entry\n")
        ops_memory.append_dreams_entry("## 2026-09-18 — End of Day Summary\n- new entry\n")

        content = ops_memory.DREAMS_FILE.read_text(encoding="utf-8")
        assert content.index("2026-09-18") < content.index("2026-09-17")

    def test_blank_entry_is_a_no_op(self, tmp_path):
        ops_memory.append_dreams_entry("   \n")
        assert not ops_memory.DREAMS_FILE.exists()


class TestNightlyReviewHookGating:
    def test_only_fires_for_penny_memory_maintenance_job(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ops_memory, "ROSTER_FILE", tmp_path / "roster.md")
        monkeypatch.setattr(ops_memory, "TICKETS_FILE", tmp_path / "tickets.md")
        monkeypatch.setattr(ops_memory, "EVENTS_FILE", tmp_path / "events.md")
        monkeypatch.setattr(ops_memory, "ARCHIVE_DIR", tmp_path / "archive")
        monkeypatch.setattr(
            "cron.trend_detection.run_trend_detection",
            lambda: {"event_updates": [], "stall_findings": []},
        )
        calls = []
        monkeypatch.setattr(
            "cron.day_review.run_nightly_review",
            lambda: calls.append("called") or {"gaps": []},
        )

        ops_memory.extract_operational_memory("job-1", "no output", "triage-board-sweep")
        assert calls == []

        ops_memory.extract_operational_memory("job-2", "no output", "penny-memory-maintenance")
        assert calls == ["called"]
