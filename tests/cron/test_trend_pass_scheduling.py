"""Tests for cron/scheduler.py's trend-pass scheduling.

cron/trend_pass.py's run_trend_pass() was built and tested standalone but
had no call site -- nothing ever invoked it. maybe_run_trend_pass() closes
that gap: it decides WHEN to fire the scheduled pass and resolves send_fn/
chat_id via the same home-channel delivery primitive (_deliver_result)
board_watch's card sender already uses, without adding a second delivery
path or a jobs.json entry.
"""
import json
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from cron.scheduler import (
    _trend_pass_due,
    _trend_pass_state_path,
    maybe_run_trend_pass,
)

# 2026-08-19 is a Wednesday. 11:05 UTC = 07:05 EDT (America/New_York, DST in
# effect in August) -- at/after the default cron.trend schedule (Mon/Wed/Fri
# 07:00 America/New_York), so it is due under the default config.
NOW = datetime(2026, 8, 19, 11, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_trend_pass_state(tmp_path, monkeypatch):
    """Point the cadence state file at a per-test tmp path so tests
    never share or persist real scheduling state."""
    state_path = tmp_path / "trend_pass_schedule.json"
    monkeypatch.setattr("cron.scheduler._trend_pass_state_path", lambda: state_path)
    yield state_path


class TestDisabledNeverFires:
    def test_enabled_false_skips_before_any_due_check_or_call(self, monkeypatch, _isolated_trend_pass_state):
        monkeypatch.setattr(
            "cron.scheduler.cfg_get",
            lambda cfg, *keys, default=None: False if keys == ("cron", "trend", "enabled") else default,
        )
        with patch("cron.trend_pass.run_trend_pass", new_callable=AsyncMock) as mock_run:
            maybe_run_trend_pass(now=NOW)

        mock_run.assert_not_called()
        # Disabled path never even touches the state file.
        assert not _isolated_trend_pass_state.exists()


class TestEnabledDailyInvocation:
    def _enable(self, monkeypatch):
        monkeypatch.setattr(
            "cron.scheduler.cfg_get",
            lambda cfg, *keys, default=None: True if keys == ("cron", "trend", "enabled") else default,
        )

    def test_enabled_true_invokes_run_trend_pass_with_send_fn_and_chat_id(self, monkeypatch):
        self._enable(monkeypatch)
        monkeypatch.setattr("cron.scheduler._iter_home_target_platforms", lambda: ["teams"])
        monkeypatch.setattr("cron.scheduler._get_home_target_chat_id", lambda p: "19:abc@thread.tacv2")

        captured = {}

        async def fake_run_trend_pass(*, send_fn=None, chat_id=""):
            captured["send_fn"] = send_fn
            captured["chat_id"] = chat_id
            return {"ran": True}

        with patch("cron.trend_pass.run_trend_pass", side_effect=fake_run_trend_pass):
            maybe_run_trend_pass(now=NOW)

        assert captured["send_fn"] is not None
        assert captured["chat_id"] == "teams:19:abc@thread.tacv2"

    def test_cadence_gate_is_once_per_scheduled_local_date(self, _isolated_trend_pass_state, monkeypatch):
        """Full weekday/time/timezone cadence lives in
        test_trend_pass_cadence.py -- this just confirms the marker still
        blocks a second run on the same scheduled local date."""
        self._enable(monkeypatch)
        from cron.scheduler import _trend_pass_tz
        local_date = NOW.astimezone(_trend_pass_tz(None)).date().isoformat()
        _isolated_trend_pass_state.write_text(
            json.dumps({"last_run_date": local_date}), encoding="utf-8"
        )
        assert _trend_pass_due(NOW) is False

        from datetime import timedelta
        # Aug 21 2026 is a Friday -- the next scheduled day after Wed Aug 19.
        assert _trend_pass_due(NOW + timedelta(days=2)) is True

    def test_dry_run_default_true_carried_through(self, monkeypatch):
        """run_trend_pass reads cron.trend.dry_run itself (default True) --
        this scheduling layer never overrides it, so a real cfg_get default
        of True flows straight through to a live call."""
        self._enable(monkeypatch)
        monkeypatch.setattr("cron.scheduler._iter_home_target_platforms", lambda: [])
        monkeypatch.setattr("cron.scheduler._get_home_target_chat_id", lambda p: "")

        with patch("cron.trend_pass.run_trend_pass", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"ran": True, "dry_run": True}
            maybe_run_trend_pass(now=NOW)

        # No dry_run kwarg passed -- run_trend_pass's own cfg_get(default=True) governs.
        _, kwargs = mock_run.call_args
        assert "dry_run" not in kwargs


class TestOverlapSafety:
    def test_concurrent_invocation_runs_trend_pass_exactly_once(self, monkeypatch):
        self._enable = lambda mp: mp.setattr(
            "cron.scheduler.cfg_get",
            lambda cfg, *keys, default=None: True if keys == ("cron", "trend", "enabled") else default,
        )
        self._enable(monkeypatch)
        monkeypatch.setattr("cron.scheduler._iter_home_target_platforms", lambda: [])
        monkeypatch.setattr("cron.scheduler._get_home_target_chat_id", lambda p: "")

        release_first = threading.Event()
        entered_first = threading.Event()
        call_count = {"n": 0}

        async def slow_run_trend_pass(*, send_fn=None, chat_id=""):
            call_count["n"] += 1
            entered_first.set()
            release_first.wait(timeout=5)
            return {"ran": True}

        with patch("cron.trend_pass.run_trend_pass", side_effect=slow_run_trend_pass):
            t1 = threading.Thread(target=maybe_run_trend_pass, kwargs={"now": NOW})
            t1.start()
            entered_first.wait(timeout=5)

            # Second invocation while the first is still in flight must be
            # skipped, not queued and not run in parallel.
            maybe_run_trend_pass(now=NOW)

            release_first.set()
            t1.join(timeout=5)

        assert call_count["n"] == 1


class TestFailureIsolation:
    def test_run_trend_pass_raising_is_logged_and_swallowed(self, monkeypatch, caplog):
        monkeypatch.setattr(
            "cron.scheduler.cfg_get",
            lambda cfg, *keys, default=None: True if keys == ("cron", "trend", "enabled") else default,
        )
        monkeypatch.setattr("cron.scheduler._iter_home_target_platforms", lambda: [])
        monkeypatch.setattr("cron.scheduler._get_home_target_chat_id", lambda p: "")

        async def boom(*, send_fn=None, chat_id=""):
            raise RuntimeError("CW pull failed")

        with patch("cron.trend_pass.run_trend_pass", side_effect=boom):
            with caplog.at_level("ERROR"):
                maybe_run_trend_pass(now=NOW)  # must not raise

        assert any("trend_pass" in r.message and "scheduled pass failed" in r.message for r in caplog.records)

        # The scheduler survives: a second call on the NEXT scheduled day
        # (Aug 21 2026 is the Friday after Wed Aug 19) still attempts a run.
        from datetime import timedelta
        async def ok(*, send_fn=None, chat_id=""):
            return {"ran": True}

        with patch("cron.trend_pass.run_trend_pass", side_effect=ok) as mock_ok:
            maybe_run_trend_pass(now=NOW + timedelta(days=2))
        mock_ok.assert_called_once()
