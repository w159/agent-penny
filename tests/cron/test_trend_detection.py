"""
Unit tests for cron/trend_detection.py — deterministic cluster, repeat
offender, and stall detection. Fixture data only; no live CW calls.
"""
from datetime import datetime, timedelta, timezone

import pytest

from cron.trend_detection import (
    Ticket,
    detect_repeat_offenders,
    detect_stalled_tickets,
    detect_symptom_clusters,
    run_trend_detection,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _ticket(id, summary, contact, priority="Priority 2 - High", entered_days_ago=1,
            updated_hours_ago=1, closed=False, status="New"):
    from cron.trend_detection import _significant_tokens

    return Ticket(
        id=id,
        summary=summary,
        contact=contact,
        status=status,
        closed=closed,
        priority=priority,
        entered_at=NOW - timedelta(days=entered_days_ago),
        updated_at=NOW - timedelta(hours=updated_hours_ago),
        tokens=_significant_tokens(summary),
    )


class TestSymptomClusters:
    def test_genuine_cluster_fires(self):
        """3 distinct users hitting the same symptom within the window must fire."""
        tickets = [
            _ticket(1, "Outlook keeps signing me out", "Alice Smith"),
            _ticket(2, "Getting signed out of Outlook repeatedly", "Bob Jones"),
            _ticket(3, "Outlook signing out every 30 minutes", "Carol Lee"),
        ]
        findings = detect_symptom_clusters(tickets, NOW, window_days=7, min_users=3)
        assert any(f.token == "outlook" for f in findings)
        outlook = next(f for f in findings if f.token == "outlook")
        assert sorted(outlook.users) == ["Alice Smith", "Bob Jones", "Carol Lee"]
        assert outlook.ticket_ids == [1, 2, 3]

    def test_near_miss_two_users_does_not_fire(self):
        """Only 2 distinct users on the same symptom must NOT produce a cluster finding."""
        tickets = [
            _ticket(1, "Tamarac session timeout again", "Dave Park"),
            _ticket(2, "Tamarac keeps timing out", "Erin Cole"),
        ]
        findings = detect_symptom_clusters(tickets, NOW, window_days=7, min_users=3)
        assert findings == []

    def test_near_miss_outside_window_does_not_fire(self):
        """3 users, same symptom, but spread outside the detection window must NOT fire."""
        tickets = [
            _ticket(1, "VPN connection dropping", "Alice Smith", entered_days_ago=1),
            _ticket(2, "VPN connection dropping", "Bob Jones", entered_days_ago=2),
            _ticket(3, "VPN connection dropping", "Carol Lee", entered_days_ago=30),
        ]
        findings = detect_symptom_clusters(tickets, NOW, window_days=7, min_users=3)
        assert findings == []

    def test_quiet_period_empty_input(self):
        assert detect_symptom_clusters([], NOW) == []


class TestRepeatOffenders:
    def test_repeat_offender_fires(self):
        tickets = [
            _ticket(1, "Checkscanner not working", "Karis Simpson", entered_days_ago=20),
            _ticket(2, "Checkscanner broken again", "Karis Simpson", entered_days_ago=10),
            _ticket(3, "Checkscanner down once more", "Karis Simpson", entered_days_ago=1),
        ]
        findings = detect_repeat_offenders(tickets, min_count=3, window_days=30, now=NOW)
        assert len(findings) == 1
        assert findings[0].contact == "Karis Simpson"
        assert findings[0].count == 3

    def test_below_threshold_does_not_fire(self):
        tickets = [
            _ticket(1, "Printer jammed", "Frank Gray", entered_days_ago=5),
            _ticket(2, "Printer jammed again", "Frank Gray", entered_days_ago=1),
        ]
        findings = detect_repeat_offenders(tickets, min_count=3, window_days=30, now=NOW)
        assert findings == []


class TestStalledTickets:
    def test_cant_work_ticket_past_threshold_fires(self):
        """A high-priority ticket idle well past its threshold must escalate."""
        tickets = [
            _ticket(
                1, "Cannot access files, completely blocked", "Nicole McFarland",
                priority="Priority 1 - Emergency", updated_hours_ago=10,  # threshold is 4h
            )
        ]
        findings = detect_stalled_tickets(tickets, NOW)
        assert len(findings) == 1
        assert findings[0].ticket_id == 1
        assert findings[0].hours_stale == 10.0
        assert findings[0].threshold_hours == 4

    def test_fresh_ticket_under_threshold_does_not_fire(self):
        tickets = [
            _ticket(
                2, "Cannot access files", "Someone Else",
                priority="Priority 1 - Emergency", updated_hours_ago=1,
            )
        ]
        assert detect_stalled_tickets(tickets, NOW) == []

    def test_closed_ticket_never_flagged(self):
        tickets = [
            _ticket(
                3, "Cannot access files", "Someone Else",
                priority="Priority 1 - Emergency", updated_hours_ago=999, closed=True,
            )
        ]
        assert detect_stalled_tickets(tickets, NOW) == []

    def test_system_generated_ticket_excluded(self):
        tickets = [
            _ticket(4, "Synology DSM update alert", "notifications",
                    priority="Priority 1 - Emergency", updated_hours_ago=999)
        ]
        assert detect_stalled_tickets(tickets, NOW) == []


class TestRunTrendDetectionEndToEnd:
    def test_quiet_period_produces_no_event_updates(self):
        result = run_trend_detection(tickets=[])
        assert result["event_updates"] == []
        assert result["stall_findings"] == []

    def test_genuine_cluster_produces_event_update(self):
        tickets = [
            _ticket(1, "Outlook keeps signing me out", "Alice Smith"),
            _ticket(2, "Getting signed out of Outlook repeatedly", "Bob Jones"),
            _ticket(3, "Outlook signing out every 30 minutes", "Carol Lee"),
        ]
        result = run_trend_detection(tickets=tickets)
        assert len(result["event_updates"]) >= 1
        ev = result["event_updates"][0]
        assert set(ev.keys()) == {"date", "name", "entry"}
        assert "outlook" in ev["name"].lower()
