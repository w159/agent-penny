#!/usr/bin/env python3
"""
Escalation surface for `trend_detection.detect_stalled_tickets` output.

Owner's standing ban: a previous job was retired for posting cold, massive
multi-ticket Teams messages every cycle (see
skills/devops/connectwise-triage-sweep/SKILL.md, "History: Adaptive Cards"
and the hourly-sweep rewrite it describes). This module exists so stalled
tickets actually reach a human without repeating that failure:

  - DEDUPLICATION is the point. A ticket already escalated must not be
    re-raised next cycle unless it has genuinely crossed into a worse
    severity band. All of that math — what's new, what's worse, what's
    unchanged — happens here in plain Python. A model only narrates the
    short list this module hands it; it never decides what counts as new.
  - The 6-ticket cap from SKILL.md is enforced here too, not left to the
    model's judgment. TWO enforcement points, deliberately: (1)
    select_escalations() budgets its own contribution so it never selects
    more than fits alongside the board-watcher job's own "max 3 tickets"
    prompt instruction (see TEAMS_MESSAGE_TICKET_CAP /
    WATCHER_OWN_TICKET_BUDGET / MAX_ESCALATIONS_PER_CYCLE below), and (2)
    enforce_ticket_cap() is a delivery-time backstop that counts actual
    ticket references in the composed message and truncates with a
    roll-up line if the model didn't follow its own prompt. (1) alone
    would leave the watcher's own delta list unenforced by code — a
    repeat of this exact defect for that half of the message. (2) alone
    would let select_escalations() mark tickets as "escalated" in state
    that then get silently dropped at delivery for being over budget,
    which would wrongly suppress their re-escalation later. Budgeting at
    selection avoids that; the backstop catches what budgeting can't see.
  - Severity is read from CW's own priority field (same convention as
    trend_detection.STALL_THRESHOLD_HOURS): Priority 1/2 tickets are the
    owner's top-ranked "someone can't work" case and outrank a merely old
    Priority 3/4 ticket, regardless of which one has been stalled longer.

State persists in memories/ops/escalation_state.json, alongside the other
ops memory files (see cron/ops_memory.py's OPS_DIR). Unlike roster/tickets/
events.md, this file doesn't need the size-cap/archive machinery in
ops_memory._write_with_cap — it's a dict keyed by open ticket id, pruned to
exactly the currently-open stalled set on every run (see
select_escalations), so it never grows unbounded.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

OPS_DIR = get_hermes_home() / "memories" / "ops"
STATE_FILE = OPS_DIR / "escalation_state.json"

# Single source of truth for the hard cap from
# skills/devops/connectwise-triage-sweep/SKILL.md — "A Teams message never
# lists more than 6 tickets — sweep or otherwise." Every other ticket-count
# constant in this module is derived from this one number; do not hardcode
# 6 (or any other cap) anywhere else.
TEAMS_MESSAGE_TICKET_CAP = 6

# The board-watcher-001 job's own prompt (cron/jobs.json) separately
# instructs the model to report "max 3 tickets" from its own delta check —
# that instruction lives in prose, same as the cap did before this fix, and
# nothing in Python constrains it directly. Reserving this many slots out of
# the shared cap for the watcher's own findings keeps the two prompt-worth
# additions honest without touching that prompt text.
WATCHER_OWN_TICKET_BUDGET = 3

# What select_escalations() may hand the model this cycle: the shared cap
# minus the watcher's own reserved budget, so the two additive prompt
# injections (escalation block + watcher's own delta list) cannot together
# exceed TEAMS_MESSAGE_TICKET_CAP even if the model follows both prompts to
# the letter. Kept as a named, computed value rather than a second magic
# number.
MAX_ESCALATIONS_PER_CYCLE = TEAMS_MESSAGE_TICKET_CAP - WATCHER_OWN_TICKET_BUDGET

# Delivery-time backstop match: CW ticket references in this codebase are
# always rendered "#<digits>" (see build_prompt_block below and the
# CONNECTWISE TICKET PRESENTATION convention in SKILL.md) — 3-7 digits
# covers observed CW ticket id ranges without also matching short prose
# numbers like "#1" in a numbered list.
_TICKET_REF_RE = re.compile(r"#(\d{3,7})\b")

# Severity bands, ranked. "blocking" = CW priority signals someone can't
# work (Priority 1 - Emergency / Priority 2 - High); "stale" = merely past
# its age threshold (Priority 3 - Medium / Priority 4 - Low / unknown).
# Same priority strings trend_detection.STALL_THRESHOLD_HOURS keys on.
# Public (no leading underscore): cron/board_watch.py reuses this same
# blocking-band definition for its own "priority escalated to blocking"
# fire condition, so there is exactly one place that says what counts as
# an urgent CW priority.
BLOCKING_PRIORITIES = frozenset({"Priority 1 - Emergency", "Priority 2 - High"})
_BAND_RANK = {"stale": 0, "blocking": 1}


def severity_band(priority: str) -> str:
    return "blocking" if priority in BLOCKING_PRIORITIES else "stale"


@dataclass
class EscalationCandidate:
    ticket_id: int
    contact: str
    priority: str
    hours_stale: float
    threshold_hours: float
    severity_band: str
    reason: str  # "new" or "worsened"


@dataclass
class EscalationResult:
    escalate_now: list = field(default_factory=list)   # list[EscalationCandidate], capped
    deferred_count: int = 0     # newly-fired candidates cut by the cap
    total_open_stalled: int = 0  # every ticket detect_stalled_tickets returned this cycle
    silent: bool = True

    @property
    def has_blocking(self) -> bool:
        return any(c.severity_band == "blocking" for c in self.escalate_now)


def load_state() -> dict:
    """Read escalation_state.json. Missing or corrupt file -> empty state."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("escalation: state file unreadable (%s), starting fresh", e)
        return {}


def save_state(state: dict) -> None:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def select_escalations(stall_findings: list, now: datetime) -> EscalationResult:
    """
    Filter `detect_stalled_tickets()` output to what is genuinely new-or-
    worse since the last run, rank it, cap it, and persist the new state.

    A ticket fires when:
      - it is not in the saved state at all (newly stalled), or
      - its severity band got worse since it was last escalated (crossed
        from "stale" into "blocking").
    A ticket that stalled further within the SAME band (e.g. 30h -> 40h,
    still Priority 3) does not re-fire — that is the exact re-listing
    pattern the owner banned. Its `hours_stale` in state is still updated
    so a future band change is measured from the current number, not a
    stale one.

    A ticket that drops out of `stall_findings` (resolved, or picked up
    before the next detection cycle) is dropped from state entirely, so if
    it stalls again later it is treated as newly stalled, not a repeat.

    Ranking: blocking (can't-work) before stale, then by how far over
    threshold (hours_stale - threshold_hours) descending — worst first.
    """
    old_state = load_state()
    new_state: dict = {}
    fired: list = []

    for f in stall_findings:
        key = str(f.ticket_id)
        band = severity_band(f.priority)
        prev = old_state.get(key)

        if prev is None:
            reason = "new"
        elif _BAND_RANK[band] > _BAND_RANK.get(prev.get("severity_band"), 0):
            reason = "worsened"
        else:
            reason = None  # already escalated at this band or better — stay silent

        if reason:
            fired.append(
                EscalationCandidate(
                    ticket_id=f.ticket_id,
                    contact=f.contact,
                    priority=f.priority,
                    hours_stale=f.hours_stale,
                    threshold_hours=f.threshold_hours,
                    severity_band=band,
                    reason=reason,
                )
            )
            escalated_at = now.isoformat()
        else:
            escalated_at = prev.get("escalated_at")

        new_state[key] = {
            "severity_band": band,
            "hours_stale": f.hours_stale,
            "priority": f.priority,
            "escalated_at": escalated_at,
            "last_seen": now.isoformat(),
        }

    fired.sort(
        key=lambda c: (_BAND_RANK[c.severity_band], c.hours_stale - c.threshold_hours),
        reverse=True,
    )

    escalate_now = fired[:MAX_ESCALATIONS_PER_CYCLE]
    deferred_count = len(fired) - len(escalate_now)

    save_state(new_state)

    return EscalationResult(
        escalate_now=escalate_now,
        deferred_count=deferred_count,
        total_open_stalled=len(stall_findings),
        silent=not escalate_now,
    )


def build_prompt_block(result: EscalationResult) -> Optional[str]:
    """
    Render `result` into a prompt-injection block naming exactly the
    tickets to speak about, with pre-reconciled counts so the model narrates
    numbers Python already computed instead of doing its own arithmetic
    (root cause of a recorded incident: a sweep posted a roll-up count that
    did not reconcile with what it listed).

    Returns None when there is nothing new-or-worse — the caller should not
    inject anything, and the model should not speak about stalled tickets
    this cycle. Silence is correct, not a fallback.
    """
    if result.silent:
        return None

    lines = [
        "## Stalled-Ticket Escalation (auto-detected, new or worsened since last check)",
        "The tickets below are the ONLY stalled tickets you should raise this cycle — "
        "they are newly stalled or have gotten worse since you last mentioned them. "
        "Do not re-list a stalled ticket you have already flagged in a prior run unless "
        "it appears here; that repetition is exactly the noise this channel got banned "
        "for before.",
        "",
    ]
    for c in result.escalate_now:
        tag = "CAN'T WORK" if c.severity_band == "blocking" else "STALE"
        lines.append(
            f"- #{c.ticket_id} [{tag}, {c.reason}] {c.contact} — {c.priority}, "
            f"stalled {c.hours_stale}h (threshold {c.threshold_hours}h)"
        )
    if result.deferred_count:
        lines.append(
            f"\n{result.deferred_count} more ticket(s) also newly crossed their stall "
            f"threshold this cycle but are held back by the {MAX_ESCALATIONS_PER_CYCLE}-ticket "
            f"escalation budget — do not list them individually; a single roll-up line "
            f"covering them is fine."
        )
    if result.has_blocking:
        lines.append(
            "\nAt least one of these is a can't-work case — this message should be sent "
            "as high importance."
        )
    lines.append("")
    return "\n".join(lines)


def enforce_ticket_cap(text: str, cap: int = TEAMS_MESSAGE_TICKET_CAP) -> str:
    """
    Delivery-time backstop for the SKILL.md ticket cap. Applies to the
    fully composed message right before it goes to Teams, regardless of
    which component put tickets into it — the escalation block above,
    the board-watcher job's own "max 3 tickets" delta findings, or both —
    because that composition happens inside the model, where Python has
    no other visibility into the final count.

    Ticket references are matched as "#<digits>" (see build_prompt_block
    above and the CONNECTWISE TICKET PRESENTATION convention every job
    that reaches this path is instructed to use). Matching is line-based
    and counts unique ticket numbers only, so the same ticket mentioned
    twice (e.g. named again in a closing summary) does not spend two
    slots. This is a heuristic, not a parser: prose that happens to
    contain a "#123"-shaped token unrelated to a ticket would be
    miscounted as one. That risk is accepted because every job that
    reaches this function is instructed, both in its own prompt and in
    build_prompt_block's block, to use exactly this format for tickets
    and nothing else — and the failure mode of a false positive is a
    slightly more aggressive truncation, never a message that exceeds
    the cap the owner set.

    Returns `text` unchanged when it references `cap` or fewer unique
    tickets. Otherwise returns everything up to (and including) the line
    that fills the cap, plus a reconciling roll-up line naming how many
    more were cut — never a truncation with no explanation, per the same
    reconciliation rule build_prompt_block follows for its own cap.
    """
    lines = text.split("\n")
    seen: set = set()
    kept: list = []
    truncated = False

    for line in lines:
        refs = set(_TICKET_REF_RE.findall(line))
        new_refs = refs - seen
        if seen and len(seen) + len(new_refs) > cap:
            truncated = True
            break
        seen |= new_refs
        kept.append(line)

    if not truncated:
        return text

    all_refs = set(_TICKET_REF_RE.findall(text))
    omitted = len(all_refs) - len(seen)
    result = "\n".join(kept).rstrip()
    if omitted > 0:
        result += (
            f"\n\n[{omitted} more ticket(s) omitted from this message — a Teams "
            f"message never lists more than {cap} tickets. Check the CW board for "
            f"the rest.]"
        )
    return result


# ---------------------------------------------------------------------------
# Pending-importance handoff: `_build_job_prompt` (scheduler.py) computes the
# escalation result before the agent runs; the send-metadata for THIS job's
# eventual delivery is decided at that point (any blocking escalation ->
# high importance). Delivery itself happens later, in a different function
# (`run_one_job`), after the agent has produced its final response. A plain
# module-level dict keyed by job_id hands that decision across the gap
# without threading a new field through the persisted job dict (which would
# risk that transient flag leaking into jobs.json on the next save).
#
# Scoped to one pending value per job_id and popped on read, so a stale
# entry cannot leak into an unrelated later run of the same job.
# ---------------------------------------------------------------------------
_pending_importance: dict = {}


def set_pending_importance(job_id: str, level: str) -> None:
    _pending_importance[job_id] = level


def pop_pending_importance(job_id: str) -> Optional[str]:
    return _pending_importance.pop(job_id, None)
