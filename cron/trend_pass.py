#!/usr/bin/env python3
"""
Daily trend-detection-and-escalation entry point.

Closes the gap between three already-tested, never-called modules:
cron/trend_cluster.py's detect_trends() (detection, no call site),
cron/trend_escalation.py's select_alerts() (the pester ladder, no call
site), and plugins/platforms/teams/ticket_card.py's build_trend_card()
(the renderer, no call site). This module pulls a rolling window of
tickets, detects cross-ticket trends, runs the escalation ladder, and
delivers whatever the ladder says to raise this cycle -- reusing the same
render_card_fence + send_paced delivery pair cron/board_watch.py already
uses for per-ticket cards, rather than inventing a second delivery path.

Lives in its own module rather than folded into board_watch.py because it
runs on a different cadence (daily, not 15-minute) off a different corpus
(a live 21-day ConnectWise pull via trend_corpus.load_corpus(), not the
local callback log trend_detection.load_tickets_from_cw_log() reads).
CW's tickets_since() returns tickets regardless of status, so closed
tickets flow into the corpus the same as open ones -- a wave of
closed-but-recurring issues is exactly the pattern this exists to catch.

Wired into cron/scheduler.py's job dispatch (maybe_run_trend_pass, called
every tick, gated by _trend_pass_due -- Mon/Wed/Fri 7am local by default;
see cron/scheduler.py, owned by another agent). Runs live against
production ConnectWise, Teams, and email once cron.trend.dry_run is false.

Config (cfg_get(load_config(), "cron", "trend", ...) pattern):
  enabled     bool, default False -- the pass does nothing at all when off.
  window_days int,  default 21    -- rolling CW pull window, also passed
                                     through to detect_trends() so the
                                     detection window and the CW pull
                                     window always agree.
  dry_run     bool, default True  -- no CW ticket, no Teams post, no email;
                                     logs what would have gone out instead.
  schedule    str,  default "daily" -- documentation only in this pass;
                                       no scheduler wiring here.
  mention_upns list[str], default [] -- UPNs to @mention on every trend
                                        card (see build_trend_card).
  email_enabled bool, default True  -- escalation-only email channel, see
                                        _deliver_alerts.
  email_to     list[str], default ["itreg@henssler.com"]
  email_sender str, default ""     -- blank means "not configured yet";
                                       the escalation email is skipped
                                       with a logged error, never a crash.
  dashboard_url str, default "https://jm-dev.tail80802c.ts.net/"
  max_tickets_per_run int, default 3 -- safety valve: this pass writes to
                                     production CW and emails real staff at
                                     a regulated firm, so one cycle can
                                     never mint an unbounded number of
                                     tickets even if detect_trends() has a
                                     bad day. Alerts past the cap are
                                     skipped (not queued) and logged loudly
                                     -- they are re-evaluated fresh next
                                     cycle, not silently dropped forever.
                                     ensure_trend_ticket (cron/trend_ticket.py)
                                     is separately idempotent per trend via
                                     its fingerprint ledger, so the cap and
                                     the dedup are independent protections.
  max_emails_per_run  int, default 5 -- same safety valve, escalation
                                     email channel.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from cron.cw_client import CWClient
from cron.trend_cluster import detect_trends
from cron.trend_corpus import load_corpus
from cron.trend_email import build_trend_email
from cron.trend_escalation import ESCALATION_LADDER, load_state, save_state, select_alerts
from cron.trend_ticket import ensure_trend_ticket
from hermes_cli.config import cfg_get, load_config
from plugins.platforms.teams.paced_send import DEFAULT_AUTONOMOUS_SEND_DELAY_S, send_paced
from plugins.platforms.teams.ticket_card import build_trend_card, render_card_fence
from tools.graph_mail import send_html_mail

logger = logging.getLogger(__name__)


def _trend_config() -> dict:
    """Read cron.trend.* with safe, feature-off-by-default values."""
    cfg = load_config()
    return {
        "enabled": bool(cfg_get(cfg, "cron", "trend", "enabled", default=False)),
        "window_days": int(cfg_get(cfg, "cron", "trend", "window_days", default=21)),
        "dry_run": bool(cfg_get(cfg, "cron", "trend", "dry_run", default=True)),
        "mention_upns": list(cfg_get(cfg, "cron", "trend", "mention_upns", default=[]) or []),
        "email_enabled": bool(cfg_get(cfg, "cron", "trend", "email_enabled", default=True)),
        "email_to": list(
            cfg_get(cfg, "cron", "trend", "email_to", default=["itreg@henssler.com"]) or []
        ),
        "email_sender": str(cfg_get(cfg, "cron", "trend", "email_sender", default="") or ""),
        "dashboard_url": str(
            cfg_get(cfg, "cron", "trend", "dashboard_url", default="https://jm-dev.tail80802c.ts.net/")
        ),
        "max_tickets_per_run": int(cfg_get(cfg, "cron", "trend", "max_tickets_per_run", default=3)),
        "max_emails_per_run": int(cfg_get(cfg, "cron", "trend", "max_emails_per_run", default=5)),
    }


async def run_trend_pass(
    *,
    now: Optional[datetime] = None,
    cw_client: Optional[CWClient] = None,
    send_fn: Optional[Callable[[str], Awaitable[Any]]] = None,
    chat_id: str = "",
    model_call=None,
    delay_s: float = DEFAULT_AUTONOMOUS_SEND_DELAY_S,
) -> dict:
    """Run one trend-detection-and-escalation cycle over the rolling window.

    Manual dry-run against real data (once cron.trend.enabled is set true
    in config.yaml, dry_run stays true by default):
        .venv/bin/python -c "
        import asyncio
        from cron.trend_pass import run_trend_pass
        print(asyncio.run(run_trend_pass()))"

    Returns a summary dict on every path, including "disabled" and
    "found nothing this cycle" -- both are ran=True/False plus explicit
    counts, never an empty dict, so a silent zero-trend run is
    distinguishable in the caller's logs from one that quietly broke.
    Upstream failures (CW, embeddings, Teams) are not caught here: they
    propagate so they are never mistaken for "no trends today".
    """
    cfg = _trend_config()
    if not cfg["enabled"]:
        logger.info("trend_pass: disabled (cron.trend.enabled=false), skipping")
        return {"ran": False, "reason": "disabled"}

    now = now or datetime.now()
    dry_run = cfg["dry_run"]
    window_days = cfg["window_days"]

    # Both calls are plain blocking synchronous functions (CW HTTP pull,
    # then CPU/HTTP-bound embedding over the whole corpus). Run them off
    # the gateway's event loop -- inline they stalled the loop long enough
    # to trip the liveness watchdog and kill the process (see incident
    # notes: gateway.shutdown_watchdog exit code 75).
    digests = await asyncio.to_thread(load_corpus, days=window_days, client=cw_client)
    trends = await asyncio.to_thread(
        detect_trends, digests, now=now, model_call=model_call, window_days=window_days
    )

    state = load_state()
    alerts = select_alerts(trends, state, now, chat_id=chat_id)
    delivered, ticketed, emailed, email_failed = await _deliver_alerts(
        alerts,
        cw_client,
        send_fn,
        dry_run=dry_run,
        delay_s=delay_s,
        mention_upns=cfg["mention_upns"],
        email_enabled=cfg["email_enabled"],
        email_to=cfg["email_to"],
        email_sender=cfg["email_sender"],
        dashboard_url=cfg["dashboard_url"],
        max_tickets_per_run=cfg["max_tickets_per_run"],
        max_emails_per_run=cfg["max_emails_per_run"],
    )
    save_state(state)

    logger.info(
        "trend_pass: tickets_loaded=%d digests_built=%d candidates=%d alerts_selected=%d "
        "delivered=%d ticketed=%d emailed=%d email_failed=%d dry_run=%s window_days=%d",
        len(digests), len(digests), len(trends), len(alerts), delivered, ticketed,
        emailed, email_failed, dry_run, window_days,
    )
    return {
        "ran": True,
        "tickets_loaded": len(digests),
        "digests_built": len(digests),
        "candidates": len(trends),
        "alerts_selected": len(alerts),
        "delivered": delivered,
        "ticketed": ticketed,
        "emailed": emailed,
        "email_failed": email_failed,
        "dry_run": dry_run,
    }


def _hours_since_raised(alert: dict) -> Optional[float]:
    """Business-hours-elapsed for the alert's rung, for the card's escalation label.

    Alerts carry ``level`` (see trend_escalation._build_alert), which maps
    directly onto ESCALATION_LADDER -- same derivation trend_email.py's
    ``_escalation_hours`` uses for the email subject/badge.
    """
    level = alert.get("level")
    if level is None:
        return None
    try:
        level = int(level)
    except (TypeError, ValueError):
        return None
    level = max(0, min(level, len(ESCALATION_LADDER) - 1))
    return ESCALATION_LADDER[level]["business_hours_elapsed"]


async def _send_escalation_emails(
    alerts: list,
    *,
    dry_run: bool,
    email_enabled: bool,
    email_to: list,
    email_sender: str,
    dashboard_url: str,
    max_emails_per_run: int = 5,
) -> tuple:
    """Email channel of the escalation ladder -- fires ONLY on kind=="escalation".

    Failure here (bad credentials, Graph throttling, a blank sender) must
    never abort Teams delivery or the rest of the pass: this always
    returns counts, it never raises. Returns (email_sent, email_failed).

    ``max_emails_per_run`` is the safety valve (see module docstring): once
    that many emails have gone out this cycle, remaining escalation emails
    are skipped and logged loudly rather than sent -- the alert itself is
    not lost, it is just re-evaluated by the ladder next cycle.
    """
    if not email_enabled:
        return 0, 0

    sent = 0
    failed = 0
    for alert in alerts:
        if alert.get("kind") != "escalation":
            continue

        if not dry_run and sent >= max_emails_per_run:
            logger.warning(
                "trend_pass: max_emails_per_run=%d reached, skipping escalation email "
                "for trend %s this cycle",
                max_emails_per_run, alert.get("trend_id"),
            )
            continue

        subject, html = build_trend_email(alert, dashboard_url=dashboard_url)

        if not email_sender:
            logger.error(
                "trend_pass: email_sender not configured, skipping escalation email "
                "for trend %s",
                alert.get("trend_id"),
            )
            failed += 1
            continue

        if dry_run:
            logger.info(
                "trend_pass: DRY RUN would send escalation email for trend %s to %s",
                alert.get("trend_id"), email_to,
            )
            continue

        try:
            result = await send_html_mail(
                to=email_to, subject=subject, html=html, sender=email_sender,
            )
        except Exception:
            logger.exception(
                "trend_pass: send_html_mail raised for trend %s", alert.get("trend_id")
            )
            failed += 1
            continue

        if result.get("success"):
            sent += 1
        else:
            logger.error(
                "trend_pass: escalation email failed for trend %s: %s",
                alert.get("trend_id"), result.get("error"),
            )
            failed += 1

    return sent, failed


async def _deliver_alerts(
    alerts: list,
    cw_client: Optional[CWClient],
    send_fn: Optional[Callable[[str], Awaitable[Any]]],
    *,
    dry_run: bool,
    delay_s: float,
    mention_upns: Optional[list] = None,
    email_enabled: bool = True,
    email_to: Optional[list] = None,
    email_sender: str = "",
    dashboard_url: str = "",
    max_tickets_per_run: int = 3,
    max_emails_per_run: int = 5,
) -> tuple:
    """Create the CW parent ticket for any level-3 alert, send the
    escalation-only email, then post one Teams card per alert.

    ``max_tickets_per_run``/``max_emails_per_run`` are the safety valve
    (see module docstring): this writes to production CW and emails real
    staff at a regulated firm, so one cycle can never create or send an
    unbounded amount even if detect_trends() has a bad day. A trend
    skipped by the cap is not lost -- it is re-evaluated by the escalation
    ladder next cycle, same as one that failed to promote this time.
    ensure_trend_ticket's own fingerprint ledger (cron/trend_ticket.py)
    separately guarantees a given trend is ticketed at most once, ever --
    the cap here bounds *how many distinct trends* one cycle may ticket,
    which the per-trend ledger cannot do on its own.

    Returns (delivered_count, ticketed_count, email_sent, email_failed).
    """
    if not alerts:
        return 0, 0, 0, 0

    ticketed = 0
    for alert in alerts:
        if alert.get("action") != "create_ticket":
            continue
        trend = alert["trend"]
        if dry_run:
            logger.info(
                "trend_pass: DRY RUN would create CW ticket for trend %s",
                trend.get("trend_id"),
            )
            continue
        if ticketed >= max_tickets_per_run:
            logger.warning(
                "trend_pass: max_tickets_per_run=%d reached, skipping CW ticket "
                "for trend %s this cycle",
                max_tickets_per_run, trend.get("trend_id"),
            )
            continue
        # No try/except: ensure_trend_ticket's own docstring explains why a
        # create_ticket failure must propagate rather than be absorbed here.
        # ensure_trend_ticket is sync (blocking CW HTTP call + ledger file
        # I/O) -- same event-loop-stall risk as load_corpus/detect_trends
        # above, so it also runs off the loop.
        result = await asyncio.to_thread(
            functools.partial(ensure_trend_ticket, trend, cw_client, dry_run=False)
        )
        logger.info("trend_pass: ensure_trend_ticket -> %s", result)
        if result.get("created"):
            ticketed += 1

    emailed, email_failed = await _send_escalation_emails(
        alerts,
        dry_run=dry_run,
        email_enabled=email_enabled,
        email_to=email_to or [],
        email_sender=email_sender,
        dashboard_url=dashboard_url,
        max_emails_per_run=max_emails_per_run,
    )

    messages = [
        render_card_fence(
            build_trend_card(
                a["trend"],
                kind=a.get("kind"),
                hours_since_raised=_hours_since_raised(a),
                mention_upns=mention_upns,
            )
        )
        for a in alerts
    ]
    if dry_run:
        logger.info("trend_pass: DRY RUN would deliver %d trend card(s)", len(messages))
        return 0, ticketed, emailed, email_failed

    if send_fn is None:
        logger.error("trend_pass: no send_fn provided, cannot deliver %d alert(s)", len(alerts))
        raise RuntimeError("trend_pass: send_fn is required when dry_run is False")

    sent = await send_paced(send_fn, messages, delay_s=delay_s)
    return len(sent), ticketed, emailed, email_failed
