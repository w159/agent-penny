"""Tests for cron/trend_email.py."""

from __future__ import annotations

from cron.trend_email import build_trend_email


def _base_trend(**overrides) -> dict:
    trend = {
        "trend_id": "trend-abc123",
        "title": "VPN drops on Contoso branch devices",
        "confidence": 0.82,
        "first_seen": "2026-08-18 09:00",
        "last_seen": "2026-08-20 07:30",
        "ticket_count": 14,
        "device_count": 9,
        "user_count": 6,
        "techs": ["Alice Tech", "Bob Tech"],
        "devices": ["FW-BRANCH-01", "SW-BRANCH-02"],
        "why_related": "All tickets reference the same VPN tunnel dropping.",
        "recommended_action": "Restart the branch VPN tunnel and check the ISP circuit.",
        "tickets": [
            {"id": f"T-{i}", "date": "2026-08-19", "summary": f"summary {i}", "evidence": f"evidence {i}"}
            for i in range(1, 4)
        ],
        "escalation_level": 2,
    }
    trend.update(overrides)
    return trend


def _alert(kind="escalation", trend=None) -> dict:
    return {"kind": kind, "trend": trend or _base_trend()}


class TestSubject:
    def test_subject_reflects_title_and_counts(self):
        subject, _ = build_trend_email(_alert())
        assert "VPN drops on Contoso branch devices" in subject
        assert "14 tickets" in subject
        assert "9 devices" in subject

    def test_subject_reflects_escalation_level_hours(self):
        from cron.trend_escalation import ESCALATION_LADDER

        expected = ESCALATION_LADDER[2]["business_hours_elapsed"]
        subject, _ = build_trend_email(_alert(trend=_base_trend(escalation_level=2)))
        assert f"{int(expected)}h" in subject

    def test_subject_differs_by_kind_via_hours(self):
        low, _ = build_trend_email(_alert(kind="new", trend=_base_trend(escalation_level=0)))
        high, _ = build_trend_email(_alert(kind="escalation", trend=_base_trend(escalation_level=3)))
        assert low != high


class TestEscaping:
    def test_malicious_ticket_summary_is_escaped(self):
        trend = _base_trend(
            tickets=[
                {
                    "id": "T-999",
                    "date": "2026-08-20",
                    "summary": '<script>alert("xss")</script>',
                    "evidence": "raw <b>note</b> with \"quotes\" & ampersands",
                }
            ]
        )
        _, html = build_trend_email(_alert(trend=trend))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp;" in html
        assert "&quot;" in html or "&#34;" in html or "&#x27;" in html or "quotes" in html
        # The raw unescaped payload must not appear verbatim anywhere.
        assert '<script>alert("xss")</script>' not in html

    def test_malicious_title_is_escaped(self):
        trend = _base_trend(title='<img src=x onerror=alert(1)>Evil Trend')
        _, html = build_trend_email(_alert(trend=trend))
        assert "<img src=x onerror=alert(1)>" not in html
        assert "&lt;img" in html


class TestTicketTruncation:
    def test_caps_rendered_rows_at_25_and_reports_overflow(self):
        tickets = [
            {"id": f"T-{i}", "date": "2026-08-19", "summary": f"s{i}", "evidence": f"e{i}"}
            for i in range(1, 31)
        ]
        trend = _base_trend(tickets=tickets)
        _, html = build_trend_email(_alert(trend=trend))
        assert html.count("T-1</td>") == 1
        assert "T-26" not in html
        assert "T-30" not in html
        assert "... and 5 more" in html

    def test_no_overflow_line_when_under_cap(self):
        _, html = build_trend_email(_alert())
        assert "more" not in html.lower()


class TestEmptyCollections:
    def test_empty_devices_techs_tickets_does_not_crash(self):
        trend = _base_trend(devices=[], techs=[], tickets=[])
        subject, html = build_trend_email(_alert(trend=trend))
        assert subject
        assert "None recorded." in html


class TestAsciiAndBranding:
    def test_output_is_ascii_only(self):
        subject, html = build_trend_email(_alert())
        subject.encode("ascii")
        html.encode("ascii")

    def test_no_forbidden_agent_names(self):
        subject, html = build_trend_email(_alert())
        combined = (subject + html).lower()
        assert "hermes" not in combined
        assert "nous" not in combined

    def test_mentions_agent_penny_in_footer(self):
        _, html = build_trend_email(_alert())
        assert "Agent Penny" in html

    def test_uses_henssler_brand_colors(self):
        _, html = build_trend_email(_alert())
        assert "#154734" in html
        assert "#C49A22" in html


class TestDashboardLink:
    def test_dashboard_url_included_when_given(self):
        _, html = build_trend_email(_alert(), dashboard_url="https://dashboard.example.com/trends/abc")
        assert "https://dashboard.example.com/trends/abc" in html

    def test_no_dashboard_link_when_omitted(self):
        _, html = build_trend_email(_alert())
        assert "trend dashboard" not in html
