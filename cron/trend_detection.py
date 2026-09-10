#!/usr/bin/env python3
"""
Deterministic trend detection over ticket history for Agent Penny.

Owner priority: "ensure tickets for end users that may not be able to work
... are picked up, responded to, resolved as quickly as possible" and
"ensure prior issues are tracked to identify trends." This module is the
counting/clustering/age-math engine behind both. All arithmetic happens in
plain Python — no model call, no guessed numbers. A model may narrate a
finding this module returns, but it never computes the finding.

Runs from `ops_memory.py`'s existing post-sweep extraction hook (every job
that already has `operational_memory: true` calls extract_operational_memory
after it finishes — see cron/scheduler.py:4019-4024). That hook already
runs every cycle (15 min watcher, hourly sweep, nightly dreams, weekly
audit) and writes to memory files only — it has never posted to Teams. This
module extends that hook rather than adding a 5th cron job, per the owner's
standing ban on noisy scheduled Teams output: detection can run constantly
because "running" here means "recomputing counters and writing a memory
file," not "speaking."

Input is a list of plain ticket dicts (dependency injection) rather than a
live ConnectWise call, because inbound CW ingress is down (expired
devtunnel token). `load_tickets_from_cw_log()` reconstructs that shape from
the recorded callback payload log for testing and for the one worked
example in this module's docstring-level report; it is NOT a live feed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

CW_LOG = get_hermes_home() / "logs" / "cw_callback_payloads.jsonl"

# Priority level -> hours of no activity before a blocking ticket escalates.
# End users who "can't work" are the owner's top-ranked case, so severity is
# read from CW's own priority field, not re-guessed here.
STALL_THRESHOLD_HOURS = {
    "Priority 1 - Emergency": 4,
    "Priority 2 - High": 8,
    "Priority 3 - Medium": 24,
    "Priority 4 - Low": 72,
}
DEFAULT_STALL_HOURS = 24

# Words that carry no clustering signal on their own.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "with", "from", "into", "onto",
    "for", "to", "of", "on", "in", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "not", "no", "new", "please", "help", "issue",
    "issues", "ticket", "user", "users", "email", "re", "fw", "fwd",
    "notification", "notifications", "test", "this", "that", "will", "can",
    "does", "doesn", "cannot", "unable", "getting", "still", "again",
}

# System/monitoring senders are not end users — excluded from stall and
# repeat-offender detection (they're a separate, already-tracked noise
# problem in events.md — "Catchall Monitoring Flood" — not a can't-work
# case). Matched as a substring against contactName since these arrive as
# product/vendor names, not person names (e.g. "Microsoft 365 Defender",
# "Auvik System"), confirmed against the recorded CW callback log.
_SYSTEM_CONTACT_MARKERS = (
    "notification", "system", "monitoring", "alert", "defender", "auvik",
    "bot", "service account", "noreply", "no-reply", "help desk",
    "cloud app", "lansweeper", "reports",
)


@dataclass
class Ticket:
    id: int
    summary: str
    contact: str
    status: str
    closed: bool
    priority: str
    entered_at: Optional[datetime]
    updated_at: Optional[datetime]
    tokens: frozenset = field(default_factory=frozenset)
    # Board name (e.g. "Triage"). Added for cron/board_watch.py's "new
    # unassigned ticket ON TRIAGE" fire condition — default keeps existing
    # positional/keyword callers (see tests/cron/test_trend_detection.py)
    # working unchanged.
    board: str = ""

    def is_system_generated(self) -> bool:
        name = self.contact.strip().lower()
        return not name or any(marker in name for marker in _SYSTEM_CONTACT_MARKERS)


@dataclass
class ClusterFinding:
    token: str
    ticket_ids: list
    users: list
    window_days: int


@dataclass
class RepeatOffenderFinding:
    contact: str
    token: str
    ticket_ids: list
    count: int


@dataclass
class StallFinding:
    ticket_id: int
    contact: str
    priority: str
    hours_stale: float
    threshold_hours: float
    owner_missing: bool


def _significant_tokens(summary: str) -> frozenset:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", summary.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) >= 4)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_tickets_from_cw_log(path: Path = CW_LOG) -> list:
    """
    Reconstruct one Ticket per unique CW ticket id from the recorded
    callback log, keeping the most recent payload per id (CW sends
    "added" then repeated "updated" events for the same ticket).

    Read-only. Does not touch or replay into the log.
    """
    latest: dict = {}
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                entity = json.loads(rec["payload"]["Entity"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            ts = _parse_ts(rec.get("ts"))
            tid = entity.get("id")
            if tid is None:
                continue
            prev_ts = latest.get(tid, (None, None))[0]
            if prev_ts is None or (ts and ts >= prev_ts):
                latest[tid] = (ts, entity)

    tickets = []
    for tid, (_, entity) in latest.items():
        summary = entity.get("summary", "") or ""
        info = entity.get("_info", {}) or {}
        tickets.append(
            Ticket(
                id=tid,
                summary=summary,
                contact=entity.get("contactName", "") or "",
                status=(entity.get("status") or {}).get("name", "") or "",
                closed=bool(entity.get("closedFlag")),
                priority=(entity.get("priority") or {}).get("name", "") or "",
                entered_at=_parse_ts(info.get("dateEntered")),
                updated_at=_parse_ts(info.get("lastUpdated")),
                tokens=_significant_tokens(summary),
                board=(entity.get("board") or {}).get("name", "") or "",
            )
        )
    return tickets


def detect_symptom_clusters(
    tickets: list, now: datetime, window_days: int = 7, min_users: int = 3
) -> list:
    """
    Same token appearing in tickets from >= min_users distinct end users
    within window_days. This is the "Outlook sign-out storm" / "Tamarac
    timeout" shape: one symptom, many people, a short window.
    """
    window_start = now - _days(window_days)
    by_token: dict = {}
    for t in tickets:
        if t.is_system_generated() or t.entered_at is None:
            continue
        if t.entered_at < window_start:
            continue
        for tok in t.tokens:
            by_token.setdefault(tok, []).append(t)

    findings = []
    for tok, tix in by_token.items():
        users = sorted({t.contact for t in tix if t.contact})
        if len(users) >= min_users:
            findings.append(
                ClusterFinding(
                    token=tok,
                    ticket_ids=sorted({t.id for t in tix}),
                    users=users,
                    window_days=window_days,
                )
            )
    findings.sort(key=lambda f: len(f.users), reverse=True)
    return findings


def detect_repeat_offenders(
    tickets: list, min_count: int = 3, window_days: int = 30, now: Optional[datetime] = None
) -> list:
    """Same person, same symptom token, N+ times within window_days."""
    if now is None:
        now = _latest_ts(tickets)
    window_start = now - _days(window_days) if now else None

    by_pair: dict = {}
    for t in tickets:
        if t.is_system_generated() or not t.contact:
            continue
        if window_start and t.entered_at and t.entered_at < window_start:
            continue
        for tok in t.tokens:
            by_pair.setdefault((t.contact, tok), []).append(t)

    findings = []
    for (contact, tok), tix in by_pair.items():
        ids = sorted({t.id for t in tix})
        if len(ids) >= min_count:
            findings.append(
                RepeatOffenderFinding(contact=contact, token=tok, ticket_ids=ids, count=len(ids))
            )
    findings.sort(key=lambda f: f.count, reverse=True)
    return findings


def detect_stalled_tickets(tickets: list, now: datetime) -> list:
    """
    Open tickets with no activity (no status change since lastUpdated) for
    longer than their priority's stall threshold. Age is measured against
    severity, not a flat keyword list — a Priority 1 stalls in hours, a
    Priority 4 in days.
    """
    findings = []
    for t in tickets:
        if t.closed or t.is_system_generated() or t.updated_at is None:
            continue
        threshold = STALL_THRESHOLD_HOURS.get(t.priority, DEFAULT_STALL_HOURS)
        hours_stale = (now - t.updated_at).total_seconds() / 3600.0
        if hours_stale >= threshold:
            findings.append(
                StallFinding(
                    ticket_id=t.id,
                    contact=t.contact,
                    priority=t.priority or "Unspecified",
                    hours_stale=round(hours_stale, 1),
                    threshold_hours=threshold,
                    owner_missing=True,  # CW payload log carries no assignee field
                )
            )
    findings.sort(key=lambda f: f.hours_stale - f.threshold_hours, reverse=True)
    return findings


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _latest_ts(tickets: list) -> Optional[datetime]:
    ts = [t.updated_at for t in tickets if t.updated_at] + [t.entered_at for t in tickets if t.entered_at]
    return max(ts) if ts else None


def to_event_update(finding: ClusterFinding, now: datetime) -> dict:
    date = now.strftime("%Y-%m-%d")
    name = f"{finding.token.title()} Cluster"
    entry = (
        f"## {date} — {name} (auto-detected)\n"
        f"- **Type:** trend_detection\n"
        f"- **Tickets:** {', '.join('#' + str(i) for i in finding.ticket_ids)}\n"
        f"- **Description:** {len(finding.users)} users ({', '.join(finding.users)}) "
        f"filed tickets mentioning \"{finding.token}\" within {finding.window_days} days.\n"
        f"- **Impact:** Cross-ticket pattern, not yet confirmed as a single root cause.\n"
        f"- **Status:** detected\n"
        f"- **Owner:** unassigned — needs triage\n"
        f"- **Last update:** {date} — auto-detected by trend_detection.py, {len(finding.ticket_ids)} tickets\n"
    )
    return {"date": date, "name": name, "entry": entry}


def run_trend_detection(tickets: Optional[list] = None) -> dict:
    """
    Entry point wired from ops_memory.extract_operational_memory. Runs all
    three deterministic detectors and returns update dicts shaped to slot
    directly into ops_memory's existing _apply_event_update /
    apply_stall_flags.

    `tickets` is dependency-injected so a future live CW source can be
    passed in. When omitted, falls back to the recorded callback log —
    this is historical replay, NOT a live feed, and results will not
    reflect the current board until a live source is wired in.
    """
    if tickets is None:
        tickets = load_tickets_from_cw_log()
    if not tickets:
        return {"cluster_findings": [], "repeat_findings": [], "stall_findings": [], "event_updates": []}

    now = _latest_ts(tickets) or datetime.now(timezone.utc)

    clusters = detect_symptom_clusters(tickets, now)
    repeats = detect_repeat_offenders(tickets, now=now)
    stalls = detect_stalled_tickets(tickets, now)

    # High signal only: don't turn every 3-user token match into an event.
    # Require a real spread (>=3 distinct users) — already enforced by
    # detect_symptom_clusters's min_users default — and cap what gets
    # written per cycle so a quiet run stays quiet.
    event_updates = [to_event_update(c, now) for c in clusters[:3]]

    return {
        "cluster_findings": clusters,
        "repeat_findings": repeats,
        "stall_findings": stalls,
        "event_updates": event_updates,
    }


def to_roster_note(finding: RepeatOffenderFinding, now: datetime) -> dict:
    date = now.strftime("%Y-%m-%d")
    note = (
        f"- {date}: auto-detected repeat pattern — \"{finding.token}\" reported "
        f"{finding.count} times ({', '.join('#' + str(i) for i in finding.ticket_ids)})."
    )
    return {"name": finding.contact, "note": note}
