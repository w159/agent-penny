#!/usr/bin/env python3
"""
Acknowledgment-tracking and escalation ladder for Agent Penny's ticket
trend alerts.

The owner's requirement, in their own words: "deliver at high importance
and require a reply for someone in the group. pester until you're heard;
even potentially escalating to creating a ticket for it if nobody
acknowledges your recommendations." That is a state machine, not a model
judgment call: has a human replied, and if not, how hard do we push next
and when. This module owns exactly that arithmetic, same division of
labor as cron/escalation.py -- Python decides what counts as new, worse,
or acknowledged; a model only narrates the result it is handed.

Two traps this module exists to avoid, both learned the hard way on this
codebase (see cron/escalation.py's docstring and
skills/devops/connectwise-triage-sweep/SKILL.md):

  - Re-raising an already-answered finding is the exact defect that got a
    previous job retired. Once a trend is acknowledged it stays silent
    forever unless it MATERIALLY grows (more tickets or more devices than
    it had at ack time) -- a restatement of the same trend must not
    reopen the ladder.
  - "Acknowledged" must mean a real human said so, not the agent's own
    machine traffic. `messages.role='user'` in state.db is used for BOTH
    real Teams replies AND synthetic cron/webhook prompts fed to the
    model -- e.g. "[IMPORTANT: You are running as a scheduled cron job".
    Reading those as an ack would make the ladder self-silencing: the job
    firing itself would look like someone answering it. cron/trend_ack.py's
    join to `sessions.source = 'teams'` is the only thing that prevents
    that, and it is treated as load-bearing there, not incidental.

Ladder timing runs on business hours (8am-5pm, Monday-Friday), matching
the board-watcher cron window `*/15 8-17 * * 1-5` -- a trend raised
Friday evening should not escalate again until Monday morning, not 4
clock-hours later at midnight. Callers are expected to pass Eastern-local
naive datetimes for `now` and for any trend/state timestamps that feed
business_hours_elapsed(); this module does no timezone conversion of its
own (stdlib only, no zoneinfo dependency) and simply trusts its input is
already in that frame, the same assumption the cron schedule itself
makes.

Split across four modules purely to respect the house 300-line file cap
-- each one is a single cohesive concern, not an independent surface:
  - cron/trend_state.py    -- TrendAlertState, business hours math, load/
                               save/prune of trend_alert_state.json
  - cron/trend_ack.py      -- state.db query for a real human reply
  - cron/trend_ticket.py   -- the level-3 ConnectWise ticket payload
  - this module            -- the ladder itself and select_alerts(), the
                               one function that ties the other three
                               together into a per-cycle decision
Everything is re-exported here so callers only ever need
`from cron.trend_escalation import ...`.

Reaching level 3 creates one ConnectWise ticket and then stops
re-raising -- creating the ticket IS being heard; pestering past that
point would be the same noise problem this module exists to prevent,
aimed at CW instead of Teams.
"""
from __future__ import annotations

from datetime import datetime

# Re-exported so `from cron.trend_escalation import X` covers the whole
# public API described in this module's docstring, even though the
# implementations live in the split-out modules above.
from cron.trend_ack import ACKNOWLEDGMENT_WINDOW_MINUTES, detect_acknowledgment  # noqa: F401
from cron.trend_state import (  # noqa: F401
    BUSINESS_HOURS_PER_DAY,
    OPS_DIR,
    PRUNE_AFTER_DAYS,
    QUIET_PERIOD_DAYS,
    STATE_FILE,
    TrendAlertState,
    business_hours_elapsed,
    growth_delta,
    is_quiet,
    load_state,
    prune_state,
    record_acknowledgement,
    save_state,
    trend_signature,
)
from cron.trend_ticket import ESCALATION_BOARD_NAME, ESCALATION_COMPANY_ID, escalate_to_ticket  # noqa: F401

# The ladder itself. Each rung fires once business_hours_elapsed() since
# the trend's first raise reaches its threshold and the trend is still
# unacknowledged. Levels are cumulative floors, not one-shot timers, so a
# gap in cron runs (an outage, a paused job) still lands on the correct
# rung instead of skipping it silently.
ESCALATION_LADDER = (
    {
        # First raise. Immediate -- the owner asked for high importance
        # from the start, not just after pestering has failed.
        "level": 0,
        "business_hours_elapsed": 0.0,
        "importance": "normal",
        "ticket_action": False,
        "label": "first raise",
    },
    {
        # No ack after 4 business hours: re-raise, visibly marked
        # unacknowledged. Still normal importance -- escalating volume
        # (importance) is reserved for level 2, per the owner's wording
        # ("pester until you're heard" starts with visibility, not alarm).
        "level": 1,
        "business_hours_elapsed": 4.0,
        "importance": "normal",
        "ticket_action": False,
        "label": "re-raise, unacknowledged",
    },
    {
        # No ack after 12 business hours: importance goes to high.
        "level": 2,
        "business_hours_elapsed": 12.0,
        "importance": "high",
        "ticket_action": False,
        "label": "re-raise, importance escalated to high",
    },
    {
        # No ack after 24 business hours: create a CW ticket and stop
        # re-raising. Creating the ticket IS being heard.
        "level": 3,
        "business_hours_elapsed": 24.0,
        "importance": "high",
        "ticket_action": True,
        "label": "escalate to ConnectWise ticket",
    },
)


def _rung_for_level(level: int) -> dict:
    return ESCALATION_LADDER[level]


def _build_alert(trend: dict, state_entry: TrendAlertState, rung: dict, kind: str) -> dict:
    return {
        "trend_id": trend.get("trend_id"),
        "trend": trend,
        "level": rung["level"],
        "importance": rung["importance"],
        "requires_ack": rung["level"] < 3,
        "action": "create_ticket" if rung["ticket_action"] else None,
        "raise_count": state_entry.raise_count,
        "label": rung["label"],
        # Lets downstream card/email code label a raise without re-deriving
        # it from level/acknowledged_at itself: "new" | "update" | "escalation".
        "kind": kind,
    }


def _new_trend_state(trend_id: str, now: datetime, sig: str, ticket_count: int, device_count: int) -> TrendAlertState:
    return TrendAlertState(
        trend_id=trend_id,
        first_raised_at=now.isoformat(),
        last_raised_at=now.isoformat(),
        level=0,
        raise_count=1,
        acknowledged_at=None,
        acknowledged_by=None,
        ticket_created_id=None,
        signature=sig,
        ticket_count=ticket_count,
        device_count=device_count,
        peak_ticket_count=ticket_count,
        peak_device_count=device_count,
    )


def _handle_new_trend(trend: dict, trend_id: str, now: datetime, sig: str, ticket_count: int, device_count: int, state: dict) -> dict:
    new_state = _new_trend_state(trend_id, now, sig, ticket_count, device_count)
    state[trend_id] = new_state
    return _build_alert(trend, new_state, _rung_for_level(0), kind="new")


def _handle_acknowledged_trend(trend: dict, trend_id: str, now: datetime, sig: str, ticket_count: int, device_count: int, existing: TrendAlertState, state: dict):
    """Answered trends stay silent unless the trend materially grew past
    its recorded peak. Growth reopens the ladder from level 0 as an
    UPDATE, same as a brand-new trend, and un-retires a trend that had
    gone quiet. Otherwise, an acknowledged trend that's been quiet for
    QUIET_PERIOD_DAYS retires and stops being tracked."""
    delta = growth_delta(existing, trend)
    if delta["grew"]:
        reopened = TrendAlertState(
            trend_id=trend_id,
            first_raised_at=existing.first_raised_at,
            last_raised_at=now.isoformat(),
            level=0,
            raise_count=existing.raise_count + 1,
            acknowledged_at=None,
            acknowledged_by=None,
            ticket_created_id=None,
            signature=sig,
            ticket_count=ticket_count,
            device_count=device_count,
            peak_ticket_count=max(existing.peak_ticket_count, ticket_count),
            peak_device_count=max(existing.peak_device_count, device_count),
            last_growth_at=now.isoformat(),
            retired_at=None,
        )
        state[trend_id] = reopened
        return _build_alert(trend, reopened, _rung_for_level(0), kind="update")

    if existing.retired_at is None and is_quiet(existing, now):
        existing.retired_at = now.isoformat()
    return None  # answered and unchanged (or already retired) -- silence is correct


def _handle_pending_trend(trend: dict, trend_id: str, now: datetime, sig: str, ticket_count: int, device_count: int, existing: TrendAlertState, chat_id: str):
    """Unacknowledged trend: track peak/growth, check for a real human
    reply, then advance the ladder if the elapsed business hours crossed a
    rung this cycle didn't already account for."""
    delta = growth_delta(existing, trend)
    existing.peak_ticket_count = max(existing.peak_ticket_count, ticket_count)
    existing.peak_device_count = max(existing.peak_device_count, device_count)
    if delta["grew"]:
        existing.last_growth_at = now.isoformat()

    since_ts = datetime.fromisoformat(existing.last_raised_at).timestamp()
    ack_at, ack_by = detect_acknowledgment(trend_id, since_ts, chat_id)
    if ack_at:
        existing.acknowledged_at = ack_at
        existing.acknowledged_by = ack_by
        existing.ticket_count = ticket_count
        existing.device_count = device_count
        return None

    elapsed = business_hours_elapsed(datetime.fromisoformat(existing.first_raised_at), now)
    target_level = existing.level
    for rung in ESCALATION_LADDER:
        if elapsed >= rung["business_hours_elapsed"]:
            target_level = rung["level"]

    if target_level <= existing.level:
        return None  # hasn't reached the next rung yet -- stay silent

    rung = _rung_for_level(target_level)
    existing.level = target_level
    existing.last_raised_at = now.isoformat()
    existing.raise_count += 1
    existing.ticket_count = ticket_count
    existing.device_count = device_count
    existing.signature = sig
    return _build_alert(trend, existing, rung, kind="escalation")


def select_alerts(trends: list, state: dict, now: datetime, *, chat_id: str) -> list:
    """Decide, per trend, whether to raise this cycle and at what rung.

    Mutates `state` in place (mirrors cron/escalation.py's
    select_escalations pattern) and prunes stale entries; callers persist
    with save_state(). Does not create CW tickets itself -- a level-3
    alert is returned with action="create_ticket" and it is the caller's
    job to invoke escalate_to_ticket() and then write ticket_created_id
    back into state before the next cycle, using the same load_state/
    save_state functions this module re-exports.
    """
    alerts = []
    seen_ids = set()

    for trend in trends:
        trend_id = trend["trend_id"]
        seen_ids.add(trend_id)
        ticket_count = int(trend.get("ticket_count", 0))
        device_count = int(trend.get("device_count", 0))
        sig = trend_signature(trend)
        existing = state.get(trend_id)

        if existing is not None and existing.ticket_created_id is not None:
            continue  # already escalated to a ticket -- being heard, stop pestering

        if existing is None:
            alert = _handle_new_trend(trend, trend_id, now, sig, ticket_count, device_count, state)
        elif existing.acknowledged_at is not None:
            alert = _handle_acknowledged_trend(trend, trend_id, now, sig, ticket_count, device_count, existing, state)
        else:
            alert = _handle_pending_trend(trend, trend_id, now, sig, ticket_count, device_count, existing, chat_id)

        if alert:
            alerts.append(alert)

    prune_state(state, now, seen_ids)
    return alerts
