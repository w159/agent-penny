#!/usr/bin/env python3
"""Tests for cron/outage_windows.py - the hard time gates.

The last test in this file is a contract test rather than a unit test: it
greps every cron/outage_*.py module for a hardcoded day count wider than the
ingest window. A window that creeps wider is the exact mechanism that turned
correlation back into keyword matching before, so it is guarded permanently
here instead of being re-checked by hand.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cron.outage_windows import (
    CORRELATION_MARGIN_DAYS,
    CORRELATION_WINDOW_DAYS,
    INGEST_WINDOW_DAYS,
    OUTAGE_MAX_AGE_DAYS,
    is_within_correlation_window,
)

CRON_DIR = Path(__file__).resolve().parents[2] / "cron"
START = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)


def test_ingest_window_stays_inside_ticket_retention():
    """Signal tickets are retained about a week without updates. Reading
    further back than that returns nothing and hides the gap."""
    assert INGEST_WINDOW_DAYS <= 7


def test_correlation_window_is_days_not_weeks():
    assert CORRELATION_WINDOW_DAYS <= 4
    assert CORRELATION_MARGIN_DAYS <= 2


def test_ticket_inside_an_open_outage_window_is_a_candidate():
    assert is_within_correlation_window(START + timedelta(days=1), START)


def test_ticket_just_inside_the_margin_is_a_candidate():
    assert is_within_correlation_window(START - timedelta(hours=20), START)


def test_ticket_weeks_away_is_rejected():
    """The July 1 and July 22 Outlook tickets, the real false pairing that
    prompted this gate."""
    assert not is_within_correlation_window(datetime(2026, 7, 1, tzinfo=timezone.utc), START)
    assert not is_within_correlation_window(datetime(2026, 8, 20, tzinfo=timezone.utc), START)


def test_a_closed_outage_bounds_the_window_at_its_end():
    end = START + timedelta(days=1)
    assert is_within_correlation_window(end + timedelta(hours=12), START, end)
    assert not is_within_correlation_window(end + timedelta(days=5), START, end)


def test_an_open_outage_does_not_grant_an_unbounded_future():
    """An outage open for three weeks must stop collecting correlations and
    be re-validated instead of quietly explaining everything since."""
    assert not is_within_correlation_window(START + timedelta(days=21), START)


def test_missing_timestamps_reject_rather_than_pass():
    assert not is_within_correlation_window(None, START)
    assert not is_within_correlation_window(START, None)


def test_no_outage_module_hardcodes_a_wider_window():
    """Contract test. Any `days=N` or `timedelta(days=N)` literal in a cron
    outage module wider than the ingest window is a drift back toward the
    45-day habit, and fails here rather than in production."""
    pattern = re.compile(r"days\s*=\s*(\d+)")
    offenders = []
    for path in sorted(CRON_DIR.glob("outage_*.py")):
        if path.name == "outage_windows.py":
            continue  # the constants themselves live here by design
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            for match in pattern.finditer(line):
                if int(match.group(1)) > max(INGEST_WINDOW_DAYS, OUTAGE_MAX_AGE_DAYS):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "hardcoded window wider than the ingest window:\n" + "\n".join(offenders)
