#!/usr/bin/env python3
"""
Deterministic dedup for board-watcher-001's own delta findings: new
unassigned urgent tickets, reopens, and priority jumps into a blocking
band. Companion to `escalation.py`'s stalled-ticket dedup, kept as a
separate module because the fire conditions are a different kind of
thing. escalation.py tracks *staleness* crossing a severity band over
time; this module tracks board-state *transitions* (new / reopened /
priority jump) that have nothing to do with how long a ticket sits.

ROOT CAUSE this replaces: board-watcher-001's prompt (cron/jobs.json)
used to instruct the model to run `cw_search_tickets with dateEntered >
[last_check_timestamp]`, a literal placeholder string nothing ever
substituted. Every 15-minute run therefore re-discovered and re-reported
the same still-open tickets (confirmed: ticket #94689 posted 7 times
between 08:46 and 10:35 ET on 2026-08-04). A real timestamp alone would
not have been enough: "genuinely changed" still needs a definition, or
an unrelated field bump (e.g. lastUpdated ticking over) would keep
re-firing the same ticket. Following escalation.py's own precedent
instead: Python owns the dedup arithmetic, keyed by ticket id, against
the same ticket snapshot trend_detection.load_tickets_from_cw_log()
already provides. The model narrates the short list this module hands
it; it does not decide what counts as new (same rationale as
escalation.py's module docstring; a recorded incident involved a model
posting counts that did not reconcile with what it listed).

State persists in memories/ops/board_watch_state.json, same directory
convention as escalation_state.json (see escalation.py's OPS_DIR).

"Genuinely changed", concretely: a ticket fires when, compared to its
last recorded snapshot:
  - NEW: never seen before by this module, still open, on the Triage
    board, and already in a blocking priority band (Priority 1/2). A
    brand-new Priority 4 ticket is not urgent enough to interrupt anyone
    on a 15-minute cadence; the hourly sweep covers it.
  - REOPENED: previously recorded closed=True, now open again.
  - PRIORITY_ESCALATED: priority crossed from a non-blocking band into a
    blocking band (Priority 1/2) since it was last seen, independent of
    how long the ticket has been open.
Age alone (a ticket that has simply been sitting unassigned longer)
never re-fires by itself. That is the exact repeat-posting pattern this
module exists to stop; see TestSameTicketUnchangedStaysSilent in
tests/cron/test_board_watch.py.

Known gap: "owner changed to UNASSIGNED" was one of board-watcher-001's
original four watch conditions. The only ticket data source available
here (the recorded CW callback payload log, see
trend_detection.load_tickets_from_cw_log's docstring) carries no
assignee/resource field at all, so that condition cannot be computed
deterministically. It has been dropped from automatic detection rather
than left as a free-text instruction for the model to re-implement with
its own live search; that free-text pattern is the exact defect being
fixed here. If assignee data becomes available in this pipeline, "owner
reverted to unassigned" should be added as a fourth fire condition.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from cron.escalation import BLOCKING_PRIORITIES, OPS_DIR, WATCHER_OWN_TICKET_BUDGET, severity_band
from plugins.platforms.teams.paced_send import DEFAULT_AUTONOMOUS_SEND_DELAY_S, send_paced
from plugins.platforms.teams.ticket_card import build_ticket_card, render_card_fence

logger = logging.getLogger(__name__)

STATE_FILE = OPS_DIR / "board_watch_state.json"

TRIAGE_BOARD_NAME = "Triage"
_UNKNOWN_BOARD = "Unknown"

# Shared between build_board_prompt_block (model narration) and
# deliver_board_deltas (Python-built cards) so the two surfaces describe a
# fired candidate's reason identically.
_REASON_LABEL = {
    "new": "NEW on Triage",
    "reopened": "REOPENED",
    "priority_escalated": "PRIORITY JUMPED to blocking",
}

# Reuses escalation.py's WATCHER_OWN_TICKET_BUDGET rather than a second
# magic number: that constant already IS "how many tickets the watcher's
# own delta findings may contribute", reserved out of the shared
# TEAMS_MESSAGE_TICKET_CAP alongside escalation.py's own selection (see
# escalation.py's module docstring, TestCombinedCapArithmetic in
# tests/cron/test_escalation.py). This module is what now fills that
# budget, in place of the model's own free-text search.
MAX_DELTAS_PER_CYCLE = WATCHER_OWN_TICKET_BUDGET


@dataclass
class BoardDeltaCandidate:
    ticket_id: int
    contact: str
    priority: str
    status: str
    summary: str
    reason: str  # "new", "reopened", or "priority_escalated"
    board: str = _UNKNOWN_BOARD


@dataclass
class BoardDeltaResult:
    fired: list = field(default_factory=list)   # list[BoardDeltaCandidate], capped
    deferred_count: int = 0
    total_open: int = 0
    silent: bool = True


def load_state() -> dict:
    """Read board_watch_state.json. Missing or corrupt file -> empty state."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("board_watch: state file unreadable (%s), starting fresh", e)
        return {}


def save_state(state: dict) -> None:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def select_board_deltas(tickets: list, now: datetime) -> BoardDeltaResult:
    """
    Diff `tickets` (trend_detection.Ticket instances) against the saved
    snapshot to find what genuinely changed since the last run, cap it,
    and persist the new snapshot.

    Every ticket in `tickets` gets a snapshot entry regardless of whether
    it fires, so the next run has something to diff against. A ticket
    that disappears from `tickets` (closed and aged out of the log, or
    picked up) is dropped from state; if it reappears later it is
    treated as newly seen, not a repeat. This mirrors
    escalation.select_escalations's same rule for stalled tickets.
    """
    old_state = load_state()
    new_state: dict = {}
    fired: list = []

    for t in tickets:
        key = str(t.id)
        prev = old_state.get(key)
        band = severity_band(t.priority)

        reason = None
        if prev is not None:
            # "new" and "reopened" are deliberately NOT fired here. The
            # ConnectWise callback lane (webhook route cw-ticket) already
            # announces both the instant CW fires the callback, and this
            # poller re-announced the same ticket up to 15 minutes later:
            # one new ticket, two Teams cards. Measured 2026-08-11: the
            # 12:46 run logged "board_watch delivered 3 card(s) for 3"
            # covering tickets the webhook lane had already posted.
            #
            # A priority jump is the one transition CW does not reliably
            # fire a callback for, so it stays: it is what this poller is
            # still here to catch.
            if band == "blocking" and prev.get("severity_band") != "blocking":
                reason = "priority_escalated"
            # Same band, still open, still closed, or dropped to a
            # lower band: none of those re-fire. In particular: still
            # open and still blocking is exactly the "sitting there"
            # case that must NOT re-fire on age alone.

        if reason:
            fired.append(
                BoardDeltaCandidate(
                    ticket_id=t.id,
                    contact=t.contact,
                    priority=t.priority or "Unspecified",
                    status=t.status or "Unknown",
                    summary=t.summary or "",
                    reason=reason,
                    board=t.board or _UNKNOWN_BOARD,
                )
            )

        new_state[key] = {
            "closed": t.closed,
            "severity_band": band,
            "status": t.status,
            "last_seen": now.isoformat(),
        }

    # Only priority_escalated fires from this module now, but the rank map
    # keeps the older reasons so a candidate built elsewhere cannot KeyError
    # its way out of the cap, and so the order stays deterministic.
    _reason_rank = {"new": 2, "reopened": 1, "priority_escalated": 0}
    fired.sort(key=lambda c: _reason_rank.get(c.reason, 0), reverse=True)

    kept = fired[:MAX_DELTAS_PER_CYCLE]
    deferred_count = len(fired) - len(kept)

    save_state(new_state)

    return BoardDeltaResult(
        fired=kept,
        deferred_count=deferred_count,
        total_open=len(tickets),
        silent=not kept,
    )


def build_board_prompt_block(result: BoardDeltaResult) -> Optional[str]:
    """
    Render `result` into a prompt-injection block naming exactly the
    tickets to speak about. Returns None when nothing genuinely changed
   , the caller should not inject anything, and the model should not
    invent a delta finding this cycle. Silence is correct, not a
    fallback to search on its own.
    """
    if result.silent:
        return None

    # Labels state only what was actually measured. "Unassigned" is
    # deliberately absent: the CW callback log carries no assignee, so
    # claiming it would put an unverified fact in Penny's mouth.
    lines = [
        "## Board Delta (auto-detected, genuinely new or changed since last check)",
        "The tickets below are the ONLY board changes you should raise this cycle, "
        "each is newly discovered, reopened, or has jumped to a blocking priority "
        "since you last checked. Do not search the board yourself or re-list a "
        "ticket that isn't in this list; that repetition is exactly the noise this "
        "channel got banned for before.",
        "",
    ]
    for c in result.fired:
        lines.append(
            f"- #{c.ticket_id} [{_REASON_LABEL[c.reason]}] {c.contact}, "
            f"{c.priority}, {c.status}, {c.summary}"
        )
    if result.deferred_count:
        lines.append(
            f"\n{result.deferred_count} more ticket(s) also changed this cycle but are "
            f"held back by the {MAX_DELTAS_PER_CYCLE}-ticket delta budget, do not list "
            f"them individually."
        )
    lines.append("")
    return "\n".join(lines)


def _candidate_to_ticket_dict(c: BoardDeltaCandidate) -> dict:
    """Map one fired delta to the flat dict ``ticket_card.build_ticket_card``
    expects, built entirely in Python so no model ever authors card JSON -
    the root cause of the 2026-08-04 `Expecting ',' delimiter` break.

    ``status`` drives which of the four card designs the ticket renders as
    (``ticket_card.select_card_variant``), so it is the one field that must
    be carried through even when it is empty.

    No ``owner`` key is sent, deliberately. The CW callback log carries no
    assignee field (see this module's "Known gap" above); omitting the key
    makes the card leave the Owner row out, where sending "Unknown" would
    print a placeholder and sending "" would claim the ticket is unassigned
    and offer a claim button for work someone may already be doing.
    """
    return {
        "number": c.ticket_id,
        "status": c.status,
        "priority": c.priority,
        "board": c.board,
        "contact": c.contact,
        "summary": f"[{_REASON_LABEL[c.reason]}] {c.summary}",
    }


async def deliver_board_deltas(
    result: BoardDeltaResult,
    send_fn,
    *,
    delay_s: float = DEFAULT_AUTONOMOUS_SEND_DELAY_S,
) -> list:
    """Send one compact Adaptive Card per fired ticket via ``send_fn``.

    Replaces the old "hand the model a prompt block and let it narrate"
    delivery for this watcher: ``result.fired`` becomes exactly
    ``len(result.fired)`` calls to ``send_fn``, one ticket per card, paced
    the same way ``ticket_card``/``paced_send`` already guarantee for any
    other autonomous multi-ticket send. A silent result (nothing fired)
    sends nothing.
    """
    if not result.fired:
        return []
    messages = [
        render_card_fence(build_ticket_card(_candidate_to_ticket_dict(c)))
        for c in result.fired
    ]
    return await send_paced(send_fn, messages, delay_s=delay_s)
