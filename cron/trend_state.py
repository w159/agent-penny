#!/usr/bin/env python3
"""
State model and persistence for cron/trend_escalation.py, split into its
own module purely to keep trend_escalation.py under the house 300-line
file cap -- this is one cohesive concern (what a trend's alert state
looks like, and how it survives a crash mid-write) with no reason to
live inline with the ladder's per-cycle selection logic.

State persists in memories/ops/trend_alert_state.json, alongside the
other ops memory files (see cron/ops_memory.py's OPS_DIR).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

OPS_DIR = get_hermes_home() / "memories" / "ops"
STATE_FILE = OPS_DIR / "trend_alert_state.json"

# Business-hours window the ladder's clock runs on, matching the
# board-watcher cron expression `*/15 8-17 * * 1-5` (see
# cron/trend_escalation.py's module docstring).
BUSINESS_START_HOUR = 8
BUSINESS_END_HOUR = 17
BUSINESS_HOURS_PER_DAY = BUSINESS_END_HOUR - BUSINESS_START_HOUR  # 9

# Unacknowledged entries whose trend hasn't reappeared in this many days
# are dropped so the state file can't grow unbounded.
PRUNE_AFTER_DAYS = 30

# An acknowledged trend that hasn't grown in this many days is considered
# quiet and gets retired (stops being tracked) -- "follow until one week
# with no additional spread," in the owner's words.
QUIET_PERIOD_DAYS = 7


@dataclass
class TrendAlertState:
    trend_id: str
    first_raised_at: str
    last_raised_at: str
    level: int
    raise_count: int
    acknowledged_at: Optional[str]
    acknowledged_by: Optional[str]
    ticket_created_id: Optional[int]
    signature: str
    ticket_count: int
    device_count: int
    # Additive lifecycle fields (see growth_delta/is_quiet below). Defaulted
    # so a state file written before these existed still loads cleanly --
    # load_state() below just fills them in via dataclass defaults.
    peak_ticket_count: int = 0
    peak_device_count: int = 0
    last_growth_at: Optional[str] = None
    retired_at: Optional[str] = None


def business_hours_elapsed(start: datetime, end: datetime) -> float:
    """Business hours (Mon-Fri, 8am-5pm) between two naive datetimes.

    Walks day by day rather than using a fixed multiplier so a span that
    crosses a weekend correctly excludes it -- a Friday-evening raise must
    not count Saturday/Sunday clock-hours toward the next rung.
    """
    if end <= start:
        return 0.0

    total = 0.0
    cur = start
    while cur.date() <= end.date():
        if cur.weekday() < 5:  # Monday=0 .. Friday=4
            day_start = cur.replace(hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)
            day_end = cur.replace(hour=BUSINESS_END_HOUR, minute=0, second=0, microsecond=0)
            window_start = max(cur, day_start)
            window_end = min(end, day_end)
            if window_end > window_start:
                total += (window_end - window_start).total_seconds() / 3600.0
        next_day = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if next_day > end:
            break
        cur = next_day
    return total


def trend_signature(trend: dict) -> str:
    """Deterministic fingerprint of a trend's current shape.

    Used only for state bookkeeping (what did this trend look like when
    we last raised it) -- material-growth comparison itself is done on
    the plain ticket_count/device_count fields, not on this hash, so a
    changed signature alone never triggers a re-raise.
    """
    payload = json.dumps(
        {
            "trend_id": trend.get("trend_id"),
            "ticket_count": trend.get("ticket_count"),
            "device_count": trend.get("device_count"),
            "ticket_ids": sorted(str(t) for t in trend.get("ticket_ids", [])),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def growth_delta(existing: TrendAlertState, trend: dict) -> dict:
    """How much a trend grew this cycle relative to its recorded peak.

    Compared against the PEAK, not the last-seen count, so a trend that
    dips and re-grows still reads as growth against the largest blast
    radius it ever reached -- the additive tracking the owner asked for.
    """
    ticket_count = int(trend.get("ticket_count", 0))
    device_count = int(trend.get("device_count", 0))
    new_tickets = max(0, ticket_count - existing.peak_ticket_count)
    new_devices = max(0, device_count - existing.peak_device_count)
    grew = ticket_count > existing.peak_ticket_count or device_count > existing.peak_device_count
    return {"new_tickets": new_tickets, "new_devices": new_devices, "grew": grew}


def is_quiet(existing: TrendAlertState, now: datetime) -> bool:
    """True once QUIET_PERIOD_DAYS have passed with no observed growth.

    Falls back to first_raised_at when the trend has never grown (no
    last_growth_at yet) so a trend that was acknowledged on day one and
    never grew still ages out after a week, instead of being tracked
    forever.
    """
    reference = existing.last_growth_at or existing.first_raised_at
    try:
        ref_dt = datetime.fromisoformat(reference)
    except (ValueError, TypeError):
        return False
    return (now - ref_dt) >= timedelta(days=QUIET_PERIOD_DAYS)


def record_acknowledgement(
    trend_id: str,
    user_id: str,
    user_name: str,
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    """Mark a trend acknowledged from outside the escalation cron cycle
    (e.g. a Teams button/command another agent owns). Reuses load_state/
    save_state's atomic temp+rename write, so a call here racing the
    scheduler's own load-mutate-save cycle can't corrupt the file -- worst
    case is a lost update (last writer wins), never a torn write.
    """
    state = load_state(path)
    entry = state.get(trend_id)
    if entry is None:
        return False
    entry.acknowledged_at = (now or datetime.now()).isoformat()
    entry.acknowledged_by = user_name or user_id
    save_state(state, path)
    return True


def load_state(path: Optional[Path] = None) -> dict:
    """Read trend_alert_state.json. Missing or corrupt file -> empty state."""
    p = path or STATE_FILE
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("trend_state: state file unreadable (%s), starting fresh", e)
        return {}

    state: dict = {}
    for trend_id, fields in raw.items():
        try:
            entry = TrendAlertState(**fields)
        except TypeError as e:
            logger.warning("trend_state: dropping malformed state entry %s (%s)", trend_id, e)
            continue
        # Peak can never legitimately be below the last-known count -- a
        # state file written before peak_* existed (or built without it,
        # like a test fixture) defaults peaks to 0, which would otherwise
        # read as "grew" against the current ticket/device count the
        # instant this code runs. Floor it here so that's a one-time,
        # silent self-heal instead of a spurious re-raise.
        entry.peak_ticket_count = max(entry.peak_ticket_count, entry.ticket_count)
        entry.peak_device_count = max(entry.peak_device_count, entry.device_count)
        state[trend_id] = entry
    return state


def save_state(state: dict, path: Optional[Path] = None) -> None:
    """Write state atomically (temp file + rename) so a crash mid-write
    can't leave a truncated, unparseable file behind."""
    p = path or STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {trend_id: asdict(st) for trend_id, st in state.items()}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def prune_state(state: dict, now: datetime, seen_ids: set) -> None:
    """Drop entries not present this cycle whose last raise is stale.

    `seen_ids` (trends present in this cycle's input) are never pruned
    even if old, since the trend is demonstrably still alive -- only a
    trend that has genuinely stopped appearing ages out.
    """
    cutoff = now - timedelta(days=PRUNE_AFTER_DAYS)
    stale = []
    for trend_id, st in state.items():
        if trend_id in seen_ids:
            continue
        try:
            last = datetime.fromisoformat(st.last_raised_at)
        except (ValueError, TypeError):
            last = None
        if last is None or last < cutoff:
            stale.append(trend_id)
    for trend_id in stale:
        del state[trend_id]
