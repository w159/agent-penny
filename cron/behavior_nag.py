#!/usr/bin/env python3
"""Deterministic, SOUL.md-safe surfacing of stale pending behavior-change proposals.

cron/behavior_store.py's propose()/approve() model means a proposal never renders anywhere
on its own -- that is the whole point of the pending/active split (see that module's
docstring). Left there, a proposal nobody has approved or rejected just sits invisible until
someone thinks to go check for it, reproducing the exact "propose a fix, nothing ever
surfaces it for approval" failure memories/ops/ROLE.md already diagnosed for human-written
lessons.

This module turns cron/behavior_store.py's list_stale_pending() into a short prompt block an
existing Teams-delivering job (see the ``nag_stale_proposals`` gate in
cron/scheduler_prompt.py) can read and mention in its own voice. It never renders anything by
itself -- like board_watch/outage_routing, an empty result means zero injected bytes so a
quiet queue produces a silent run, per the [SILENT] convention.

Phrasing is deliberately plain and paraphrased, never a status readout: no rule id, no the
words "rule", "behavior_store", "pending", or "approval" -- SOUL.md bans self-narrating cron
plumbing to Teams, and the safest way to honor that is to never generate the banned
vocabulary in the first place, not to trust the model to filter it out.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from cron.behavior_store import list_stale_pending

# Keep the injected block small: this rides inside an already-busy prompt (triage board state,
# operational memory, etc.) and the point is a nudge, not a queue dump.
_MAX_NAG_ITEMS = 3

_NAG_HEADING = (
    "## Still-open asks from the team\n"
    "Nobody has acted on these yet. Mention one in your own voice if it fits naturally "
    "this run; otherwise say nothing about it."
)


def _paraphrase(proposal: dict) -> str:
    """One plain-language line for a stale proposal -- what a teammate would say about a
    still-open ask, not a status readout. Uses ``scope`` + a trimmed ``text`` clause; never
    the numeric id, and never the words a cron-plumbing readout would use."""
    scope = (proposal.get("scope") or "").strip()
    text = (proposal.get("text") or "").strip().rstrip(".")
    if scope and text:
        return f"- still waiting on a decision about {scope}: {text}."
    return f"- still waiting on a decision about: {text or scope}."


def render_stale_pending_nag(
    proposals: Optional[list[dict]] = None,
    *,
    older_than_hours: int = 24,
    now: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> str:
    """Fenced prompt block for a ``nag_stale_proposals``-opted-in job, or ``""`` when nothing
    is stale. ``proposals`` lets a caller pass an already-fetched list (tests, or a caller that
    wants to log what it found); omitted -> fetched here via list_stale_pending(). ``now``
    pins the age-cutoff clock for deterministic tests; omitted -> real UTC now."""
    rows = (
        list_stale_pending(older_than_hours, now=now, db_path=db_path)
        if proposals is None
        else proposals
    )
    if not rows:
        return ""
    lines = [_NAG_HEADING]
    lines.extend(_paraphrase(row) for row in rows[:_MAX_NAG_ITEMS])
    return "\n".join(lines) + "\n"
