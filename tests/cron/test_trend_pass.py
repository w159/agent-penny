#!/usr/bin/env python3
"""
Unit tests for cron/trend_pass.py -- the daily wiring that ties
detect_trends() (cron/trend_cluster.py), select_alerts() (cron/
trend_escalation.py), and build_trend_card() (plugins/platforms/teams/
ticket_card.py) together into one entry point. None of those three had a
call site before this module; these tests exercise the wiring, not their
internal logic (each already has its own test suite).

load_corpus/detect_trends are mocked throughout -- no real ConnectWise or
embedding call. load_state/save_state run for real against a tmp_path
state file (matching tests/cron/test_trend_escalation.py's own pattern)
so the repeat-alert-suppression tests exercise the real dedup logic, not
a mock of it.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from cron.trend_escalation import TrendAlertState, save_state
from cron.trend_pass import run_trend_pass

NOW = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)


def _trend(trend_id="trend-1", ticket_count=3, device_count=2):
    return {
        "trend_id": trend_id,
        "title": "Printer offline across finance floor",
        "summary": "Printer offline across finance floor",
        "ticket_count": ticket_count,
        "device_count": device_count,
        "user_count": 2,
        "ticket_ids": [101, 102, 103],
        "tickets": [{"id": 101, "evidence": "Printer offline"}],
        "confidence": "high",
        "first_seen": "2026-08-15",
        "last_seen": "2026-08-18",
    }


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point trend_state's STATE_FILE at a scratch file so no test ever
    touches the real memories/ops/trend_alert_state.json."""
    state_file = tmp_path / "trend_alert_state.json"
    monkeypatch.setattr("cron.trend_state.OPS_DIR", tmp_path)
    monkeypatch.setattr("cron.trend_state.STATE_FILE", state_file)
    return state_file


@pytest.fixture
def enabled_config(monkeypatch):
    """cron.trend.enabled=True, dry_run=False unless overridden per-test."""
    def _set(
        *,
        dry_run=False,
        window_days=21,
        mention_upns=None,
        email_enabled=True,
        email_to=None,
        email_sender="sender@henssler.com",
        dashboard_url="https://jm-dev.tail80802c.ts.net/",
        max_tickets_per_run=3,
        max_emails_per_run=5,
    ):
        monkeypatch.setattr(
            "cron.trend_pass._trend_config",
            lambda: {
                "enabled": True,
                "window_days": window_days,
                "dry_run": dry_run,
                "mention_upns": mention_upns or [],
                "email_enabled": email_enabled,
                "email_to": email_to or ["itreg@henssler.com"],
                "email_sender": email_sender,
                "dashboard_url": dashboard_url,
                "max_tickets_per_run": max_tickets_per_run,
                "max_emails_per_run": max_emails_per_run,
            },
        )
    return _set


def _patch_pipeline(monkeypatch, trends):
    monkeypatch.setattr("cron.trend_pass.load_corpus", MagicMock(return_value=["digest"] * 5))
    monkeypatch.setattr("cron.trend_pass.detect_trends", MagicMock(return_value=trends))


class TestDisabledByDefault:
    @pytest.mark.asyncio
    async def test_enabled_false_does_not_run(self, monkeypatch):
        monkeypatch.setattr(
            "cron.trend_pass._trend_config",
            lambda: {"enabled": False, "window_days": 21, "dry_run": True},
        )
        load_corpus = MagicMock()
        monkeypatch.setattr("cron.trend_pass.load_corpus", load_corpus)

        result = await run_trend_pass(now=NOW)

        assert result == {"ran": False, "reason": "disabled"}
        load_corpus.assert_not_called()


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_trend_delivers_card_and_creates_ticket(self, monkeypatch, enabled_config):
        # Level-3 alert (action="create_ticket") exercises both delivery
        # paths in one pass: Teams card + ensure_trend_ticket.
        enabled_config(dry_run=False)
        _patch_pipeline(monkeypatch, [_trend()])
        alert = {"trend_id": "trend-1", "trend": _trend(), "action": "create_ticket"}
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=[alert]))
        ensure_ticket = MagicMock(return_value={"created": True, "ticket_id": 555})
        monkeypatch.setattr("cron.trend_pass.ensure_trend_ticket", ensure_ticket)
        send_fn = AsyncMock(return_value={"ok": True})

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        assert result["alerts_selected"] == 1
        assert result["delivered"] == 1
        assert result["ticketed"] == 1
        send_fn.assert_awaited_once()
        ensure_ticket.assert_called_once()


class TestSafetyValveCaps:
    """cron.trend.max_tickets_per_run / max_emails_per_run -- a safety
    valve on live production writes, independent of the per-trend
    ensure_trend_ticket ledger (which bounds duplicates of ONE trend, not
    how many DISTINCT trends one cycle may ticket/email)."""

    @pytest.mark.asyncio
    async def test_ticket_cap_stops_after_limit_remaining_alerts_skipped(
        self, monkeypatch, enabled_config
    ):
        enabled_config(dry_run=False, max_tickets_per_run=2)
        trends = [_trend(f"trend-{i}") for i in range(4)]
        _patch_pipeline(monkeypatch, trends)
        alerts = [
            {"trend_id": f"trend-{i}", "trend": trends[i], "action": "create_ticket"}
            for i in range(4)
        ]
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=alerts))
        ensure_ticket = MagicMock(
            side_effect=lambda trend, cw, dry_run: {"created": True, "ticket_id": trend["trend_id"]}
        )
        monkeypatch.setattr("cron.trend_pass.ensure_trend_ticket", ensure_ticket)
        send_fn = AsyncMock(return_value={"ok": True})

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        assert result["ticketed"] == 2
        assert ensure_ticket.call_count == 2

    @pytest.mark.asyncio
    async def test_email_cap_stops_after_limit_remaining_alerts_skipped(
        self, monkeypatch, enabled_config
    ):
        enabled_config(dry_run=False, max_emails_per_run=1)
        trends = [_trend(f"trend-{i}") for i in range(3)]
        _patch_pipeline(monkeypatch, trends)
        alerts = [
            {
                "trend_id": f"trend-{i}", "trend": trends[i], "action": None,
                "kind": "escalation", "level": 1,
            }
            for i in range(3)
        ]
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=alerts))
        send_mail = AsyncMock(return_value={"success": True, "status": 202, "error": None})
        monkeypatch.setattr("cron.trend_pass.send_html_mail", send_mail)
        send_fn = AsyncMock(return_value={"ok": True})

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        assert result["emailed"] == 1
        assert send_mail.await_count == 1

    @pytest.mark.asyncio
    async def test_dry_run_ignores_ticket_cap_since_nothing_is_created(
        self, monkeypatch, enabled_config
    ):
        # DRY RUN logging every alert is not a real write, so the cap must
        # not suppress the log lines an operator uses to review what the
        # pass WOULD have done.
        enabled_config(dry_run=True, max_tickets_per_run=1)
        trends = [_trend(f"trend-{i}") for i in range(3)]
        _patch_pipeline(monkeypatch, trends)
        alerts = [
            {"trend_id": f"trend-{i}", "trend": trends[i], "action": "create_ticket"}
            for i in range(3)
        ]
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=alerts))
        ensure_ticket = MagicMock()
        monkeypatch.setattr("cron.trend_pass.ensure_trend_ticket", ensure_ticket)
        send_fn = AsyncMock()

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        assert result["dry_run"] is True
        ensure_ticket.assert_not_called()


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_skips_teams_and_cw(self, monkeypatch, enabled_config):
        enabled_config(dry_run=True)
        _patch_pipeline(monkeypatch, [_trend()])
        alert = {"trend_id": "trend-1", "trend": _trend(), "action": "create_ticket"}
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=[alert]))
        ensure_ticket = MagicMock()
        monkeypatch.setattr("cron.trend_pass.ensure_trend_ticket", ensure_ticket)
        send_fn = AsyncMock()

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        assert result["dry_run"] is True
        assert result["delivered"] == 0
        send_fn.assert_not_awaited()
        ensure_ticket.assert_not_called()


class TestRepeatAlertSuppression:
    @pytest.mark.asyncio
    async def test_same_trend_two_runs_same_day_alerts_once(self, monkeypatch, enabled_config):
        # Real select_alerts/load_state/save_state (only load_corpus and
        # detect_trends are mocked) -- the second run, same `now`, must not
        # cross the ladder's next business-hours rung, so it stays silent.
        enabled_config(dry_run=True)
        _patch_pipeline(monkeypatch, [_trend()])
        send_fn = AsyncMock()

        first = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")
        second = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        assert first["alerts_selected"] == 1
        assert second["alerts_selected"] == 0

    @pytest.mark.asyncio
    async def test_acknowledged_trend_not_re_alerted(self, monkeypatch, enabled_config, isolated_state):
        enabled_config(dry_run=True)
        trend = _trend(ticket_count=3, device_count=2)
        _patch_pipeline(monkeypatch, [trend])
        # Seed state as already acknowledged with the SAME counts the trend
        # still has -- _handle_acknowledged_trend only re-opens on material
        # growth, so this must stay silent.
        from cron.trend_state import trend_signature

        state = {
            "trend-1": TrendAlertState(
                trend_id="trend-1",
                first_raised_at=(NOW - timedelta(days=1)).isoformat(),
                last_raised_at=(NOW - timedelta(days=1)).isoformat(),
                level=0,
                raise_count=1,
                acknowledged_at=(NOW - timedelta(hours=12)).isoformat(),
                acknowledged_by="tech@henssler.com",
                ticket_created_id=None,
                signature=trend_signature(trend),
                ticket_count=3,
                device_count=2,
            )
        }
        save_state(state)
        send_fn = AsyncMock()

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        assert result["alerts_selected"] == 0
        send_fn.assert_not_awaited()


class TestFailurePropagation:
    @pytest.mark.asyncio
    async def test_cw_failure_propagates_not_swallowed(self, monkeypatch, enabled_config):
        enabled_config(dry_run=True)

        def _boom(**kwargs):
            raise RuntimeError("ConnectWise unreachable")

        monkeypatch.setattr("cron.trend_pass.load_corpus", _boom)

        with pytest.raises(RuntimeError, match="ConnectWise unreachable"):
            await run_trend_pass(now=NOW)

    @pytest.mark.asyncio
    async def test_live_send_without_send_fn_raises(self, monkeypatch, enabled_config):
        # dry_run False but no send_fn given must fail loudly, never
        # silently report zero delivered.
        enabled_config(dry_run=False)
        _patch_pipeline(monkeypatch, [_trend()])
        alert = {"trend_id": "trend-1", "trend": _trend(), "action": None}
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=[alert]))

        with pytest.raises(RuntimeError, match="send_fn is required"):
            await run_trend_pass(now=NOW, chat_id="chat-1")


class TestWindow:
    def test_ticket_outside_window_excluded_inside_included(self):
        # Direct unit test of the window_days parameter this task added to
        # detect_trends() -- exercises the real clustering pipeline, no
        # mocks, matching tests/cron/test_trend_cluster.py's own fixtures.
        from cron.trend_cluster import detect_trends
        from cron.trend_corpus import TicketDigest

        def _digest(id, date, contact):
            return TicketDigest(
                id=id, date=date, board="Triage", summary="Printer offline",
                contact=contact, status="Open", priority="Priority 3 - Medium",
                issue="Printer offline", resolution="", tech_notes=[], techs=[],
                devices=[], entities={"printer_offline"}, is_automated=False,
            )

        now = datetime(2026, 8, 19, tzinfo=timezone.utc)
        in_window = [
            _digest(1, "2026-08-01", "A"),   # 18 days old: inside a 21-day window
            _digest(2, "2026-08-05", "B"),
            _digest(3, "2026-08-10", "C"),
        ]
        outside_window = [_digest(4, "2026-07-01", "D")]  # 49 days old: outside
        noise = [
            TicketDigest(
                id=100 + i, date="2026-08-15", board="Triage", summary="x",
                contact=f"Noise {i}", status="Open", priority="Priority 3 - Medium",
                issue="x", resolution="", tech_notes=[], techs=[], devices=[],
                entities={f"noise_{i}"}, is_automated=False,
            )
            for i in range(40)
        ]

        trends = detect_trends(
            in_window + outside_window + noise, now=now, model_call=None, window_days=21,
        )

        assert len(trends) == 1
        ids = {t["id"] for t in trends[0]["tickets"]}
        assert ids == {1, 2, 3}
        assert 4 not in ids


class TestEscalationEmail:
    """cron.trend.email_* wiring: email fires ONLY on kind=="escalation"."""

    @pytest.mark.asyncio
    async def test_escalation_alert_sends_email(self, monkeypatch, enabled_config):
        enabled_config(dry_run=False)
        _patch_pipeline(monkeypatch, [_trend()])
        alert = {
            "trend_id": "trend-1", "trend": _trend(), "action": None,
            "kind": "escalation", "level": 1,
        }
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=[alert]))
        send_mail = AsyncMock(return_value={"success": True, "status": 202, "error": None})
        monkeypatch.setattr("cron.trend_pass.send_html_mail", send_mail)
        send_fn = AsyncMock(return_value={"ok": True})

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        send_mail.assert_awaited_once()
        kwargs = send_mail.await_args.kwargs
        assert kwargs["to"] == ["itreg@henssler.com"]
        assert kwargs["sender"] == "sender@henssler.com"
        assert result["emailed"] == 1
        assert result["email_failed"] == 0

    @pytest.mark.asyncio
    async def test_new_and_update_alerts_do_not_email(self, monkeypatch, enabled_config):
        enabled_config(dry_run=False)
        _patch_pipeline(monkeypatch, [_trend()])
        alerts = [
            {"trend_id": "trend-1", "trend": _trend(), "action": None, "kind": "new"},
            {"trend_id": "trend-1", "trend": _trend(), "action": None, "kind": "update"},
        ]
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=alerts))
        send_mail = AsyncMock()
        monkeypatch.setattr("cron.trend_pass.send_html_mail", send_mail)
        send_fn = AsyncMock(return_value={"ok": True})

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        send_mail.assert_not_awaited()
        assert result["emailed"] == 0
        assert result["email_failed"] == 0

    @pytest.mark.asyncio
    async def test_email_failure_does_not_stop_teams_delivery(self, monkeypatch, enabled_config):
        enabled_config(dry_run=False)
        _patch_pipeline(monkeypatch, [_trend()])
        alert = {
            "trend_id": "trend-1", "trend": _trend(), "action": None,
            "kind": "escalation", "level": 1,
        }
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=[alert]))
        send_mail = AsyncMock(side_effect=RuntimeError("Graph unreachable"))
        monkeypatch.setattr("cron.trend_pass.send_html_mail", send_mail)
        send_fn = AsyncMock(return_value={"ok": True})

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        send_fn.assert_awaited_once()
        assert result["delivered"] == 1
        assert result["emailed"] == 0
        assert result["email_failed"] == 1

    @pytest.mark.asyncio
    async def test_blank_email_sender_skips_cleanly(self, monkeypatch, enabled_config, caplog):
        enabled_config(dry_run=False, email_sender="")
        _patch_pipeline(monkeypatch, [_trend()])
        alert = {
            "trend_id": "trend-1", "trend": _trend(), "action": None,
            "kind": "escalation", "level": 1,
        }
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=[alert]))
        send_mail = AsyncMock()
        monkeypatch.setattr("cron.trend_pass.send_html_mail", send_mail)
        send_fn = AsyncMock(return_value={"ok": True})

        with caplog.at_level("ERROR"):
            result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        send_mail.assert_not_awaited()
        assert result["delivered"] == 1
        assert any("email_sender" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_dry_run_does_not_send_email(self, monkeypatch, enabled_config):
        enabled_config(dry_run=True)
        _patch_pipeline(monkeypatch, [_trend()])
        alert = {
            "trend_id": "trend-1", "trend": _trend(), "action": None,
            "kind": "escalation", "level": 1,
        }
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=[alert]))
        send_mail = AsyncMock()
        monkeypatch.setattr("cron.trend_pass.send_html_mail", send_mail)
        send_fn = AsyncMock()

        result = await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        send_mail.assert_not_awaited()
        assert result["emailed"] == 0


class TestMentionWiring:
    @pytest.mark.asyncio
    async def test_mention_upns_passed_to_card_builder(self, monkeypatch, enabled_config):
        enabled_config(dry_run=False, mention_upns=["jmorgan@henssler.com"])
        _patch_pipeline(monkeypatch, [_trend()])
        alert = {"trend_id": "trend-1", "trend": _trend(), "action": None, "kind": "new"}
        monkeypatch.setattr("cron.trend_pass.select_alerts", MagicMock(return_value=[alert]))
        build_card = MagicMock(wraps=__import__(
            "plugins.platforms.teams.ticket_card", fromlist=["build_trend_card"]
        ).build_trend_card)
        monkeypatch.setattr("cron.trend_pass.build_trend_card", build_card)
        send_fn = AsyncMock(return_value={"ok": True})

        await run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")

        build_card.assert_called_once()
        assert build_card.call_args.kwargs["mention_upns"] == ["jmorgan@henssler.com"]


class TestClosedTicketsIncluded:
    def test_closed_ticket_flows_into_digest_corpus(self):
        # build_digests() (cron/trend_corpus.py) applies no closed/open
        # filter -- a closed CW ticket dict produces a TicketDigest exactly
        # like an open one, which is what lets a wave of closed-but-
        # recurring issues surface as a trend.
        from cron.trend_corpus import build_digests

        ticket = {
            "id": 900,
            "summary": "VPN drops after 10 minutes",
            "contactName": "Closed Case Carl",
            "status": {"name": "Closed"},
            "closedFlag": True,
            "priority": {"name": "Priority 3 - Medium"},
            "board": {"name": "Triage"},
            "_info": {"dateEntered": "2026-08-10"},
        }

        digests = build_digests([ticket], notes_by_id={}, time_entries=[])

        assert len(digests) == 1
        assert digests[0].id == 900
        assert digests[0].status == "Closed"


class TestEventLoopNotBlocked:
    """Regression for the 2026-08-21 incident: run_trend_pass() called
    load_corpus/detect_trends directly on the gateway's event loop. Both
    are plain synchronous functions -- on the live box, load_corpus does
    blocking ConnectWise HTTP and detect_trends does blocking Ollama
    embedding HTTP, together stalling the loop for minutes. The gateway's
    liveness watchdog correctly killed the process (exit code 75).

    Proves the fix (asyncio.to_thread) by using time.sleep -- a REAL
    synchronous stall, not asyncio.sleep -- inside the mocked pipeline
    functions, and asserting a concurrent heartbeat coroutine kept
    ticking while run_trend_pass was "working". Before the fix this
    heartbeat count is ~0-1; after it is many.
    """

    @pytest.mark.asyncio
    async def test_heartbeat_keeps_ticking_during_blocking_pipeline(
        self, monkeypatch, enabled_config
    ):
        enabled_config(dry_run=True)

        # Real synchronous blocking calls -- time.sleep, not asyncio.sleep.
        # This is the shape of the real load_corpus (blocking CW HTTP) and
        # detect_trends (blocking Ollama embedding HTTP) on the live box.
        def _blocking_load_corpus(**kwargs):
            time.sleep(0.15)
            return ["digest"] * 5

        def _blocking_detect_trends(*args, **kwargs):
            time.sleep(0.15)
            return [_trend()]

        monkeypatch.setattr("cron.trend_pass.load_corpus", _blocking_load_corpus)
        monkeypatch.setattr("cron.trend_pass.detect_trends", _blocking_detect_trends)
        send_fn = AsyncMock()

        heartbeat_ticks = 0
        stop = False

        async def _heartbeat():
            nonlocal heartbeat_ticks
            while not stop:
                await asyncio.sleep(0.01)
                heartbeat_ticks += 1

        heartbeat_task = asyncio.create_task(_heartbeat())
        pass_task = asyncio.create_task(
            run_trend_pass(now=NOW, send_fn=send_fn, chat_id="chat-1")
        )

        result = await pass_task
        stop = True
        await heartbeat_task

        # ~0.3s of blocking work at a 10ms heartbeat interval should yield
        # dozens of ticks if the loop stayed responsive. Before the fix
        # (calls made directly on the loop, no to_thread) this is ~0-1.
        assert heartbeat_ticks >= 10, (
            f"event loop was blocked during run_trend_pass: only "
            f"{heartbeat_ticks} heartbeat ticks landed"
        )

        # Fix must not change run_trend_pass's output shape or values.
        assert result["ran"] is True
        assert result["tickets_loaded"] == 5
        assert result["candidates"] == 1
        assert result["dry_run"] is True
