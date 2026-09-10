"""Tests for cron/scheduler.py's config-driven trend-pass cadence.

_trend_pass_due used to be a plain "once per calendar day" gate (see
test_trend_pass_scheduling.py's history). It is now driven by
cron.trend.{days,hour,minute,timezone}, defaulting to Mon/Wed/Fri 07:00
America/New_York, evaluated via zoneinfo so DST is handled correctly.
"""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cron.scheduler import _trend_pass_due


DEFAULT_CFG = {"cron": {"trend": {"enabled": True}}}

ET = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _isolated_trend_pass_state(tmp_path, monkeypatch):
    state_path = tmp_path / "trend_pass_schedule.json"
    monkeypatch.setattr("cron.scheduler._trend_pass_state_path", lambda: state_path)
    yield state_path


def _et(year, month, day, hour, minute):
    """Build a tz-aware datetime directly in America/New_York local time."""
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class TestDefaultScheduleWeekdayAndTime:
    def test_monday_before_seven_am_et_not_due(self):
        # 2026-08-17 is a Monday.
        assert _trend_pass_due(_et(2026, 8, 17, 6, 59), cfg=DEFAULT_CFG) is False

    def test_monday_at_seven_am_et_due(self):
        assert _trend_pass_due(_et(2026, 8, 17, 7, 0), cfg=DEFAULT_CFG) is True

    def test_monday_later_in_the_day_still_due_if_not_yet_run(self):
        assert _trend_pass_due(_et(2026, 8, 17, 9, 30), cfg=DEFAULT_CFG) is True

    @pytest.mark.parametrize("day", [18, 20, 22, 23])  # Tue, Thu, Sat, Sun
    def test_non_scheduled_weekday_at_seven_am_et_not_due(self, day):
        # Week of 2026-08-17: Mon 17, Tue 18, Wed 19, Thu 20, Fri 21, Sat 22, Sun 23.
        assert _trend_pass_due(_et(2026, 8, day, 7, 0), cfg=DEFAULT_CFG) is False


class TestRunsExactlyOncePerScheduledDay:
    def test_second_tick_after_successful_run_same_local_day_not_due(self, _isolated_trend_pass_state):
        first = _et(2026, 8, 17, 7, 0)
        assert _trend_pass_due(first, cfg=DEFAULT_CFG) is True

        _isolated_trend_pass_state.write_text(
            json.dumps({"last_run_date": first.date().isoformat()}), encoding="utf-8"
        )

        later_same_day = _et(2026, 8, 17, 9, 0)
        assert _trend_pass_due(later_same_day, cfg=DEFAULT_CFG) is False

        # Next scheduled day (Wed 2026-08-19) is due again.
        assert _trend_pass_due(_et(2026, 8, 19, 7, 0), cfg=DEFAULT_CFG) is True


class TestUtcLocalDateBoundaryCrossesCorrectly:
    def test_utc_monday_still_sunday_et_not_due(self):
        # 2026-08-17 00:30 UTC is 2026-08-16 20:30 EDT -- Sunday in ET.
        moment = datetime(2026, 8, 17, 0, 30, tzinfo=timezone.utc)
        assert moment.astimezone(ET).weekday() == 6  # Sunday
        assert _trend_pass_due(moment, cfg=DEFAULT_CFG) is False

    def test_utc_sunday_already_monday_et_due_if_past_scheduled_time(self):
        # 2026-08-17 (Mon) 11:30 UTC is 07:30 EDT -- past the 07:00 gate.
        moment = datetime(2026, 8, 17, 11, 30, tzinfo=timezone.utc)
        assert moment.astimezone(ET).weekday() == 0  # Monday
        assert _trend_pass_due(moment, cfg=DEFAULT_CFG) is True


class TestDstHandledViaZoneinfo:
    def test_fires_at_local_seven_am_in_est_winter(self):
        # 2026-01-05 is a Monday, EST (UTC-5) -- no DST in effect.
        moment = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)  # 07:00 EST
        assert moment.astimezone(ET).hour == 7
        assert _trend_pass_due(moment, cfg=DEFAULT_CFG) is True

    def test_fires_at_local_seven_am_in_edt_summer(self):
        # 2026-08-17 is a Monday, EDT (UTC-4) -- DST in effect.
        moment = datetime(2026, 8, 17, 11, 0, tzinfo=timezone.utc)  # 07:00 EDT
        assert moment.astimezone(ET).hour == 7
        assert _trend_pass_due(moment, cfg=DEFAULT_CFG) is True


class TestEnabledFlagIsNotCheckedHere:
    """enabled is gated earlier, in maybe_run_trend_pass, before _trend_pass_due
    is ever called (see test_trend_pass_scheduling.py::TestDisabledNeverFires).
    _trend_pass_due itself only implements the weekday/time/marker logic, so
    it is exercised directly here with cfg carrying enabled=True throughout."""

    def test_disabled_cfg_does_not_affect_due_check_directly(self):
        # _trend_pass_due does not read "enabled" at all -- confirms the
        # separation of concerns between the two gates.
        cfg = {"cron": {"trend": {"enabled": False}}}
        assert _trend_pass_due(_et(2026, 8, 17, 7, 0), cfg=cfg) is True


class TestConfigurableDaysHourMinuteTimezone:
    def test_custom_days_list_restricts_which_weekdays_fire(self):
        cfg = {"cron": {"trend": {"enabled": True, "days": ["tue", "thu"]}}}
        assert _trend_pass_due(_et(2026, 8, 18, 7, 0), cfg=cfg) is True  # Tuesday
        assert _trend_pass_due(_et(2026, 8, 17, 7, 0), cfg=cfg) is False  # Monday

    def test_custom_hour_minute_shifts_the_gate(self):
        cfg = {"cron": {"trend": {"enabled": True, "hour": 8, "minute": 30}}}
        assert _trend_pass_due(_et(2026, 8, 17, 8, 29), cfg=cfg) is False
        assert _trend_pass_due(_et(2026, 8, 17, 8, 30), cfg=cfg) is True

    def test_custom_timezone_shifts_the_local_wall_clock(self):
        cfg = {"cron": {"trend": {"enabled": True, "timezone": "America/Los_Angeles"}}}
        pt = ZoneInfo("America/Los_Angeles")
        # 07:00 PT on a Monday.
        moment = datetime(2026, 8, 17, 7, 0, tzinfo=pt)
        assert _trend_pass_due(moment, cfg=cfg) is True
        # Same wall-clock hour but only 04:00 PT (07:00 ET) -- not due yet.
        moment_early = moment.astimezone(ET).replace(hour=7, minute=0)
        assert _trend_pass_due(moment_early, cfg=cfg) is False
