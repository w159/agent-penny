"""
Unit tests for cron/trend_escalation.py (and its split-out siblings
cron/trend_state.py, cron/trend_ack.py, cron/trend_ticket.py).

THE HEADLINE TEST is TestAcknowledgmentTrap.test_synthetic_cron_row_is_not_an_ack
-- a synthetic cron/webhook prompt sharing the same chat window as a real
human reply must never be read as an acknowledgment. That is the single
most important correctness trap this module has to avoid (see
cron/trend_escalation.py's module docstring).
"""
import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from cron.trend_ack import detect_acknowledgment
from cron.trend_escalation import (
    ESCALATION_LADDER,
    TrendAlertState,
    escalate_to_ticket,
    load_state,
    save_state,
    select_alerts,
)
from cron.trend_state import (
    QUIET_PERIOD_DAYS,
    business_hours_elapsed,
    growth_delta,
    is_quiet,
    prune_state,
    record_acknowledgement,
    trend_signature,
)

CHAT_ID = "19:d72b9e0d737b4dda960814e674c260b7@thread.v2"


# ---------------------------------------------------------------------------
# sqlite fixture matching the real state.db schema (sessions + messages)
# ---------------------------------------------------------------------------

@pytest.fixture
def state_db(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            chat_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_session(db_path, session_id, source, user_id=None, chat_id=CHAT_ID):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO sessions (id, source, user_id, chat_id) VALUES (?, ?, ?, ?)",
        (session_id, source, user_id, chat_id),
    )
    conn.commit()
    conn.close()


def _insert_message(db_path, session_id, role, content, timestamp):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (session_id, role, content, timestamp),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# detect_acknowledgment
# ---------------------------------------------------------------------------

class TestAcknowledgmentTrap:
    def test_synthetic_cron_row_is_not_an_ack(self, state_db):
        """A cron-sourced synthetic prompt in the SAME chat window must
        never be read as a human acknowledgment, even though it shares
        role='user' with real Teams replies."""
        since_ts = 1000.0
        _insert_session(state_db, "cron-session", source="cron")
        _insert_message(
            state_db,
            "cron-session",
            "user",
            "[IMPORTANT: You are running as a scheduled cron job right now]",
            since_ts + 60,
        )

        ack_at, ack_by = detect_acknowledgment(
            "TREND-001", since_ts, CHAT_ID, db_path=state_db
        )
        assert ack_at is None
        assert ack_by is None

    def test_teams_row_is_an_ack(self, state_db):
        since_ts = 1000.0
        _insert_session(state_db, "teams-session", source="teams", user_id="user-abc")
        _insert_message(
            state_db, "teams-session", "user", "on it, looking now", since_ts + 60
        )

        ack_at, ack_by = detect_acknowledgment(
            "TREND-001", since_ts, CHAT_ID, db_path=state_db
        )
        assert ack_at is not None
        assert ack_by == "user-abc"

    def test_mixed_cron_and_teams_only_teams_counts(self, state_db):
        """Both a synthetic cron row and a real Teams reply exist in the
        same window -- only the Teams row may be reported as the ack."""
        since_ts = 1000.0
        _insert_session(state_db, "cron-session", source="cron")
        _insert_message(
            state_db,
            "cron-session",
            "user",
            "A ConnectWise Triage event just came in",
            since_ts + 10,
        )
        _insert_session(state_db, "teams-session", source="teams", user_id="user-xyz")
        _insert_message(
            state_db, "teams-session", "user", "yep saw it", since_ts + 30
        )

        ack_at, ack_by = detect_acknowledgment(
            "TREND-001", since_ts, CHAT_ID, db_path=state_db
        )
        assert ack_by == "user-xyz"

    def test_trend_id_named_counts_outside_window(self, state_db):
        since_ts = 1000.0
        _insert_session(state_db, "teams-session", source="teams", user_id="u1")
        _insert_message(
            state_db,
            "teams-session",
            "user",
            "already handling TREND-001, no action needed",
            since_ts + 6 * 3600,  # 6 hours later, outside the 90-min window
        )

        ack_at, ack_by = detect_acknowledgment(
            "TREND-001", since_ts, CHAT_ID, db_path=state_db
        )
        assert ack_at is not None

    def test_unrelated_reply_hours_later_does_not_count(self, state_db):
        since_ts = 1000.0
        _insert_session(state_db, "teams-session", source="teams", user_id="u1")
        _insert_message(
            state_db,
            "teams-session",
            "user",
            "unrelated question about something else entirely",
            since_ts + 6 * 3600,
        )

        ack_at, ack_by = detect_acknowledgment(
            "TREND-001", since_ts, CHAT_ID, db_path=state_db
        )
        assert ack_at is None

    def test_missing_db_does_not_raise(self, tmp_path):
        ack_at, ack_by = detect_acknowledgment(
            "TREND-001", 0.0, CHAT_ID, db_path=tmp_path / "nonexistent.db"
        )
        assert (ack_at, ack_by) == (None, None)


# ---------------------------------------------------------------------------
# business_hours_elapsed
# ---------------------------------------------------------------------------

class TestBusinessHoursElapsed:
    def test_same_business_day(self):
        start = datetime(2026, 8, 17, 9, 0)  # Monday
        end = datetime(2026, 8, 17, 13, 0)
        assert business_hours_elapsed(start, end) == pytest.approx(4.0)

    def test_weekend_excluded(self):
        friday_5pm = datetime(2026, 8, 14, 17, 0)  # Friday close
        monday_9am = datetime(2026, 8, 17, 9, 0)  # Monday
        # No business hours elapse over the weekend itself.
        assert business_hours_elapsed(friday_5pm, monday_9am) == pytest.approx(1.0)

    def test_friday_evening_to_monday(self):
        friday_evening = datetime(2026, 8, 14, 20, 0)  # after hours Friday
        monday_late_morning = datetime(2026, 8, 17, 11, 0)
        # Nothing counts Fri 20:00-24:00, weekend excluded, Monday 8-11 = 3h.
        assert business_hours_elapsed(friday_evening, monday_late_morning) == pytest.approx(3.0)

    def test_end_before_start_is_zero(self):
        start = datetime(2026, 8, 17, 12, 0)
        end = datetime(2026, 8, 17, 9, 0)
        assert business_hours_elapsed(start, end) == 0.0


# ---------------------------------------------------------------------------
# select_alerts ladder progression + acknowledgment/growth rules
# ---------------------------------------------------------------------------

def _trend(trend_id="TREND-001", ticket_count=3, device_count=2, ticket_ids=None):
    return {
        "trend_id": trend_id,
        "ticket_count": ticket_count,
        "device_count": device_count,
        "ticket_ids": ticket_ids or [101, 102, 103],
        "summary": "repeated disk-full alerts across the fleet",
    }


class TestLadderProgression:
    def test_first_raise_is_level_zero(self, monkeypatch):
        monkeypatch.setattr(
            "cron.trend_escalation.detect_acknowledgment", lambda *a, **k: (None, None)
        )
        now = datetime(2026, 8, 17, 9, 0)  # Monday 9am
        state = {}
        alerts = select_alerts([_trend()], state, now, chat_id=CHAT_ID)
        assert len(alerts) == 1
        assert alerts[0]["level"] == 0
        assert alerts[0]["importance"] == "normal"
        assert alerts[0]["kind"] == "new"
        assert state["TREND-001"].level == 0

    def test_progression_through_all_rungs(self, monkeypatch):
        """Ladder timing is 4h / 12h / 24h of business hours (owner-decided
        change from the original 4h/8h/24h)."""
        monkeypatch.setattr(
            "cron.trend_escalation.detect_acknowledgment", lambda *a, **k: (None, None)
        )
        state = {}
        t0 = datetime(2026, 8, 17, 9, 0)  # Monday 9am
        select_alerts([_trend()], state, t0, chat_id=CHAT_ID)
        assert state["TREND-001"].level == 0

        # +4 business hours -> level 1
        t1 = datetime(2026, 8, 17, 13, 0)
        alerts = select_alerts([_trend()], state, t1, chat_id=CHAT_ID)
        assert alerts[0]["level"] == 1
        assert alerts[0]["importance"] == "normal"
        assert alerts[0]["kind"] == "escalation"

        # +12 business hours from first raise -> level 2, high importance.
        # Monday 9am + 8 remaining business hours that day (9am-5pm) = 4 more
        # needed Tuesday: Tuesday 9am (9h elapsed) is not enough, Tuesday
        # 1pm is (9h Monday-equivalent... walk it explicitly instead).
        t2 = datetime(2026, 8, 18, 13, 0)  # Mon 9-17 (8h) + Tue 9-13 (4h) = 12h
        alerts = select_alerts([_trend()], state, t2, chat_id=CHAT_ID)
        assert alerts[0]["level"] == 2
        assert alerts[0]["importance"] == "high"
        assert alerts[0]["kind"] == "escalation"

        # +24 business hours from first raise -> level 3, create ticket.
        # Mon 9-17 (8h) + Tue 9-17 (8h) + Wed 9-17 (8h) = 24h at Wed 5pm.
        t3 = datetime(2026, 8, 19, 17, 0)
        alerts = select_alerts([_trend()], state, t3, chat_id=CHAT_ID)
        assert alerts[0]["level"] == 3
        assert alerts[0]["action"] == "create_ticket"
        assert alerts[0]["kind"] == "escalation"

    def test_friday_raise_does_not_escalate_until_monday(self, monkeypatch):
        monkeypatch.setattr(
            "cron.trend_escalation.detect_acknowledgment", lambda *a, **k: (None, None)
        )
        state = {}
        friday = datetime(2026, 8, 14, 16, 0)  # Friday, 1h before close
        select_alerts([_trend()], state, friday, chat_id=CHAT_ID)
        assert state["TREND-001"].level == 0

        # Only 1 business hour has elapsed by Friday close -- checking again
        # right at close must not fire level 1 (needs 4 business hours).
        friday_close = datetime(2026, 8, 14, 17, 0)
        alerts = select_alerts([_trend()], state, friday_close, chat_id=CHAT_ID)
        assert alerts == []

        # Monday 9am: only 1 (Fri) + 1 (Mon partial) = 2 business hours have
        # elapsed -- still not enough for level 1.
        monday_9am = datetime(2026, 8, 17, 9, 0)
        alerts = select_alerts([_trend()], state, monday_9am, chat_id=CHAT_ID)
        assert alerts == []
        assert state["TREND-001"].level == 0

        # Monday 12pm: 1 + 4 = 5 business hours elapsed -- level 1 fires now.
        monday_noon = datetime(2026, 8, 17, 12, 0)
        alerts = select_alerts([_trend()], state, monday_noon, chat_id=CHAT_ID)
        assert alerts[0]["level"] == 1

    def test_no_alert_when_rung_not_yet_reached(self, monkeypatch):
        monkeypatch.setattr(
            "cron.trend_escalation.detect_acknowledgment", lambda *a, **k: (None, None)
        )
        state = {}
        t0 = datetime(2026, 8, 17, 9, 0)
        select_alerts([_trend()], state, t0, chat_id=CHAT_ID)
        soon_after = datetime(2026, 8, 17, 9, 30)
        alerts = select_alerts([_trend()], state, soon_after, chat_id=CHAT_ID)
        assert alerts == []


class TestAcknowledgmentStopsTheLadder:
    def test_ack_detected_silences_future_cycles(self, monkeypatch):
        state = {}
        t0 = datetime(2026, 8, 17, 9, 0)
        monkeypatch.setattr(
            "cron.trend_escalation.detect_acknowledgment", lambda *a, **k: (None, None)
        )
        select_alerts([_trend()], state, t0, chat_id=CHAT_ID)

        monkeypatch.setattr(
            "cron.trend_escalation.detect_acknowledgment",
            lambda *a, **k: ("2026-08-17T09:10:00+00:00", "user-abc"),
        )
        t1 = datetime(2026, 8, 18, 9, 0)  # would otherwise be level 2
        alerts = select_alerts([_trend()], state, t1, chat_id=CHAT_ID)
        assert alerts == []
        assert state["TREND-001"].acknowledged_at is not None
        assert state["TREND-001"].acknowledged_by == "user-abc"

    def test_unchanged_acknowledged_trend_never_reselected(self, monkeypatch):
        state = {
            "TREND-001": TrendAlertState(
                trend_id="TREND-001",
                first_raised_at=datetime(2026, 8, 17, 9, 0).isoformat(),
                last_raised_at=datetime(2026, 8, 17, 9, 0).isoformat(),
                level=0,
                raise_count=1,
                acknowledged_at="2026-08-17T09:10:00",
                acknowledged_by="user-abc",
                ticket_created_id=None,
                signature=trend_signature(_trend()),
                ticket_count=3,
                device_count=2,
                peak_ticket_count=3,
                peak_device_count=2,
            )
        }
        later = datetime(2026, 8, 20, 9, 0)
        alerts = select_alerts([_trend()], state, later, chat_id=CHAT_ID)
        assert alerts == []

    def test_growth_reopens_acknowledged_trend(self, monkeypatch):
        state = {
            "TREND-001": TrendAlertState(
                trend_id="TREND-001",
                first_raised_at=datetime(2026, 8, 17, 9, 0).isoformat(),
                last_raised_at=datetime(2026, 8, 17, 9, 0).isoformat(),
                level=0,
                raise_count=1,
                acknowledged_at="2026-08-17T09:10:00",
                acknowledged_by="user-abc",
                ticket_created_id=None,
                signature="oldsig",
                ticket_count=3,
                device_count=2,
                peak_ticket_count=3,
                peak_device_count=2,
            )
        }
        later = datetime(2026, 8, 20, 9, 0)
        grown = _trend(ticket_count=7, device_count=2)
        alerts = select_alerts([grown], state, later, chat_id=CHAT_ID)
        assert len(alerts) == 1
        assert alerts[0]["level"] == 0
        assert alerts[0]["kind"] == "update"
        assert state["TREND-001"].acknowledged_at is None
        assert state["TREND-001"].acknowledged_by is None
        assert state["TREND-001"].ticket_count == 7
        assert state["TREND-001"].peak_ticket_count == 7
        assert state["TREND-001"].peak_device_count == 2
        assert state["TREND-001"].last_growth_at == later.isoformat()


class TestAdditiveLifecycle:
    """peak_*/last_growth_at/retired_at additive tracking (owner requirement:
    growing blast radius is prioritized and followed until it goes quiet)."""

    def test_growth_delta_arithmetic(self):
        existing = TrendAlertState(
            trend_id="TREND-001",
            first_raised_at="2026-08-17T09:00:00",
            last_raised_at="2026-08-17T09:00:00",
            level=0,
            raise_count=1,
            acknowledged_at=None,
            acknowledged_by=None,
            ticket_created_id=None,
            signature="sig",
            ticket_count=3,
            device_count=2,
            peak_ticket_count=3,
            peak_device_count=2,
        )
        grown = growth_delta(existing, _trend(ticket_count=7, device_count=2))
        assert grown == {"new_tickets": 4, "new_devices": 0, "grew": True}

        flat = growth_delta(existing, _trend(ticket_count=3, device_count=2))
        assert flat == {"new_tickets": 0, "new_devices": 0, "grew": False}

        shrunk = growth_delta(existing, _trend(ticket_count=1, device_count=1))
        assert shrunk == {"new_tickets": 0, "new_devices": 0, "grew": False}

    def test_acknowledged_no_growth_stays_silent(self, monkeypatch):
        state = {
            "TREND-001": TrendAlertState(
                trend_id="TREND-001",
                first_raised_at=datetime(2026, 8, 17, 9, 0).isoformat(),
                last_raised_at=datetime(2026, 8, 17, 9, 0).isoformat(),
                level=0,
                raise_count=1,
                acknowledged_at="2026-08-17T09:10:00",
                acknowledged_by="user-abc",
                ticket_created_id=None,
                signature="sig",
                ticket_count=3,
                device_count=2,
                peak_ticket_count=3,
                peak_device_count=2,
            )
        }
        later = datetime(2026, 8, 18, 9, 0)  # 1 day later, well under quiet period
        alerts = select_alerts([_trend(ticket_count=3, device_count=2)], state, later, chat_id=CHAT_ID)
        assert alerts == []
        assert state["TREND-001"].acknowledged_at == "2026-08-17T09:10:00"
        assert state["TREND-001"].retired_at is None

    def test_quiet_acknowledged_trend_retires(self, monkeypatch):
        state = {
            "TREND-001": TrendAlertState(
                trend_id="TREND-001",
                first_raised_at=datetime(2026, 8, 1, 9, 0).isoformat(),
                last_raised_at=datetime(2026, 8, 1, 9, 0).isoformat(),
                level=0,
                raise_count=1,
                acknowledged_at="2026-08-01T09:10:00",
                acknowledged_by="user-abc",
                ticket_created_id=None,
                signature="sig",
                ticket_count=3,
                device_count=2,
                peak_ticket_count=3,
                peak_device_count=2,
                last_growth_at=datetime(2026, 8, 1, 9, 0).isoformat(),
            )
        }
        quiet_now = datetime(2026, 8, 1, 9, 0) + timedelta(days=QUIET_PERIOD_DAYS)
        alerts = select_alerts([_trend(ticket_count=3, device_count=2)], state, quiet_now, chat_id=CHAT_ID)
        assert alerts == []
        assert state["TREND-001"].retired_at == quiet_now.isoformat()

    def test_retired_trend_un_retires_on_growth(self, monkeypatch):
        retired_at = (datetime(2026, 8, 1, 9, 0) + timedelta(days=QUIET_PERIOD_DAYS)).isoformat()
        state = {
            "TREND-001": TrendAlertState(
                trend_id="TREND-001",
                first_raised_at=datetime(2026, 8, 1, 9, 0).isoformat(),
                last_raised_at=datetime(2026, 8, 1, 9, 0).isoformat(),
                level=0,
                raise_count=1,
                acknowledged_at="2026-08-01T09:10:00",
                acknowledged_by="user-abc",
                ticket_created_id=None,
                signature="sig",
                ticket_count=3,
                device_count=2,
                peak_ticket_count=3,
                peak_device_count=2,
                last_growth_at=datetime(2026, 8, 1, 9, 0).isoformat(),
                retired_at=retired_at,
            )
        }
        later = datetime(2026, 8, 12, 9, 0)
        alerts = select_alerts([_trend(ticket_count=9, device_count=2)], state, later, chat_id=CHAT_ID)
        assert len(alerts) == 1
        assert alerts[0]["kind"] == "update"
        assert state["TREND-001"].retired_at is None
        assert state["TREND-001"].acknowledged_at is None
        assert state["TREND-001"].peak_ticket_count == 9


class TestOldSchemaCompatibility:
    def test_old_schema_json_loads_without_raising(self, tmp_path):
        """A state file written before peak_*/last_growth_at/retired_at
        existed must still load -- new fields fall back to their defaults,
        and load_state floors peak_* to the last-known count so an old
        entry doesn't read as spurious growth the moment this code runs."""
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps(
                {
                    "TREND-001": {
                        "trend_id": "TREND-001",
                        "first_raised_at": "2026-08-17T09:00:00",
                        "last_raised_at": "2026-08-17T09:00:00",
                        "level": 1,
                        "raise_count": 2,
                        "acknowledged_at": None,
                        "acknowledged_by": None,
                        "ticket_created_id": None,
                        "signature": "abc123",
                        "ticket_count": 3,
                        "device_count": 2,
                    }
                }
            ),
            encoding="utf-8",
        )
        state = load_state(path=path)
        entry = state["TREND-001"]
        assert entry.peak_ticket_count == 3
        assert entry.peak_device_count == 2
        assert entry.last_growth_at is None
        assert entry.retired_at is None


class TestRecordAcknowledgement:
    def test_known_trend_id_records_ack_and_returns_true(self, tmp_path):
        path = tmp_path / "state.json"
        state = {
            "TREND-001": TrendAlertState(
                trend_id="TREND-001",
                first_raised_at="2026-08-17T09:00:00",
                last_raised_at="2026-08-17T09:00:00",
                level=0,
                raise_count=1,
                acknowledged_at=None,
                acknowledged_by=None,
                ticket_created_id=None,
                signature="sig",
                ticket_count=3,
                device_count=2,
            )
        }
        save_state(state, path=path)

        now = datetime(2026, 8, 17, 10, 0)
        result = record_acknowledgement(
            "TREND-001", "user-id-1", "Jane Doe", now=now, path=path
        )
        assert result is True

        reloaded = load_state(path=path)
        assert reloaded["TREND-001"].acknowledged_at == now.isoformat()
        assert reloaded["TREND-001"].acknowledged_by == "Jane Doe"

    def test_unknown_trend_id_returns_false(self, tmp_path):
        path = tmp_path / "state.json"
        save_state({}, path=path)
        result = record_acknowledgement("TREND-NOPE", "user-id-1", "Jane Doe", path=path)
        assert result is False

    def test_falls_back_to_user_id_when_no_name(self, tmp_path):
        path = tmp_path / "state.json"
        state = {
            "TREND-001": TrendAlertState(
                trend_id="TREND-001",
                first_raised_at="2026-08-17T09:00:00",
                last_raised_at="2026-08-17T09:00:00",
                level=0,
                raise_count=1,
                acknowledged_at=None,
                acknowledged_by=None,
                ticket_created_id=None,
                signature="sig",
                ticket_count=3,
                device_count=2,
            )
        }
        save_state(state, path=path)
        record_acknowledgement("TREND-001", "user-id-1", "", path=path)
        reloaded = load_state(path=path)
        assert reloaded["TREND-001"].acknowledged_by == "user-id-1"


class TestLevelThreeTicketCreation:
    def test_ticket_created_exactly_once_then_never_again(self, monkeypatch):
        monkeypatch.setattr(
            "cron.trend_escalation.detect_acknowledgment", lambda *a, **k: (None, None)
        )
        state = {}
        t0 = datetime(2026, 8, 17, 9, 0)
        select_alerts([_trend()], state, t0, chat_id=CHAT_ID)

        t3 = datetime(2026, 8, 19, 17, 0)  # 24 business hours later (Mon+Tue+Wed 8h each)
        alerts = select_alerts([_trend()], state, t3, chat_id=CHAT_ID)
        assert alerts[0]["action"] == "create_ticket"

        client = MagicMock()
        client.create_ticket.return_value = {"id": 55501}
        result = escalate_to_ticket(alerts[0]["trend"], client=client, dry_run=False)
        assert result["created"] is True
        assert client.create_ticket.call_count == 1

        # Caller persists the ticket id, mirroring the real integration.
        state["TREND-001"].ticket_created_id = result["ticket_id"]

        t4 = datetime(2026, 8, 21, 9, 0)
        alerts_again = select_alerts([_trend()], state, t4, chat_id=CHAT_ID)
        assert alerts_again == []
        assert client.create_ticket.call_count == 1

    def test_dry_run_never_calls_create_ticket(self):
        client = MagicMock()
        result = escalate_to_ticket(_trend(), client=client, dry_run=True)
        assert result["created"] is False
        client.create_ticket.assert_not_called()


# ---------------------------------------------------------------------------
# State round-trip and pruning
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "state.json"
        state = {
            "TREND-001": TrendAlertState(
                trend_id="TREND-001",
                first_raised_at="2026-08-17T09:00:00",
                last_raised_at="2026-08-17T09:00:00",
                level=1,
                raise_count=2,
                acknowledged_at=None,
                acknowledged_by=None,
                ticket_created_id=None,
                signature="abc123",
                ticket_count=3,
                device_count=2,
                peak_ticket_count=3,
                peak_device_count=2,
            )
        }
        save_state(state, path=path)
        loaded = load_state(path=path)
        assert loaded == state

    def test_corrupt_file_degrades_to_empty_state(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert load_state(path=path) == {}

    def test_missing_file_degrades_to_empty_state(self, tmp_path):
        path = tmp_path / "does_not_exist.json"
        assert load_state(path=path) == {}

    def test_pruning_drops_old_keeps_recent(self):
        now = datetime(2026, 8, 18, 9, 0)
        old = TrendAlertState(
            trend_id="TREND-OLD",
            first_raised_at=(now - timedelta(days=45)).isoformat(),
            last_raised_at=(now - timedelta(days=40)).isoformat(),
            level=1,
            raise_count=1,
            acknowledged_at=None,
            acknowledged_by=None,
            ticket_created_id=None,
            signature="x",
            ticket_count=1,
            device_count=1,
        )
        recent = TrendAlertState(
            trend_id="TREND-RECENT",
            first_raised_at=(now - timedelta(days=10)).isoformat(),
            last_raised_at=(now - timedelta(days=10)).isoformat(),
            level=1,
            raise_count=1,
            acknowledged_at=None,
            acknowledged_by=None,
            ticket_created_id=None,
            signature="y",
            ticket_count=1,
            device_count=1,
        )
        state = {"TREND-OLD": old, "TREND-RECENT": recent}
        prune_state(state, now, seen_ids=set())
        assert "TREND-OLD" not in state
        assert "TREND-RECENT" in state


# ---------------------------------------------------------------------------
# Ladder constant sanity
# ---------------------------------------------------------------------------

def test_ladder_has_four_rungs_in_order():
    assert [rung["level"] for rung in ESCALATION_LADDER] == [0, 1, 2, 3]
    hours = [rung["business_hours_elapsed"] for rung in ESCALATION_LADDER]
    assert hours == sorted(hours)
    assert ESCALATION_LADDER[3]["ticket_action"] is True
    assert all(not r["ticket_action"] for r in ESCALATION_LADDER[:3])
