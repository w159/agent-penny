#!/usr/bin/env python3
"""
Nightly day-in-review analysis for Agent Penny's DREAMS.md reflection.

Root cause this replaces: the "penny-memory-maintenance" cron job (the
"nightly dreams" job -- see SOUL.md's "Nightly learning (DREAMS.md)"
section) used to be a free-hand agent turn with no deterministic data
gathering at all -- its own prompt told it to "pull today's activity" via
tool calls and write a reflective entry, entirely trusting the model to
remember to call the right tools, cover both Teams and ConnectWise, and
notice its own recurring mistakes. In production this produced entries
scoped to "what happened on the Triage board today" only (see the
2026-09-17 DREAMS.md entry) -- no Teams conversation review, no behavior
corrections, no mechanism to catch a recurring gap without Jerry noticing
and reporting it by hand.

This module is the structural fix: the SAME kind of deterministic,
tool-call-independent pass cron/trend_detection.py already is for ticket
trend counting. It gathers real Teams message activity (hermes_state's
session/message tables), real ConnectWise ticket activity
(cron/cw_contact_index.py's local index), and real behavior corrections
(cron/behavior_store.py's audit trail) directly from their stores -- no
step here depends on a model choosing to call the right tool at the right
moment. Where it finds a genuine, specific, recurring gap (currently: an
"end user" / ticket-history question answered without a ConnectWise
contact-tool lookup -- the exact pattern Jerry reported and
plugins/cw_contact_context structurally fixes going forward), it
auto-proposes a durable behavior rule via cron.behavior_store.propose(),
the same self-improvement mechanism agent/turn_finalizer.py's
_propose_tool_refusal_correction already uses for same-turn tool-refusal
corrections -- no second, parallel proposal mechanism.

cron.ops_memory.extract_operational_memory() calls run_nightly_review()
once per day, gated to the "penny-memory-maintenance" job name, and hands
the resulting narrative to ops_memory.append_dreams_entry() for the
locked, size-capped write DREAMS.md already gets for every other ops file.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cron import behavior_store, cw_contact_index

# Tool names registered by tools/cw_contact_tool.py. A reply that answers an
# end-user/ticket-history question without calling one of these is exactly
# the gap Jerry reported 2026-09-18: Penny defaulting to an internal-roster
# reading of "end user" instead of recognizing it as a ConnectWise contact.
CONTACT_TOOL_NAMES = frozenset({
    "get_ticket_contact", "find_tickets_by_contact", "ticket_trend_by_requester",
})

# Deliberately narrow: "end user(s)" and "ticket/issue history" are the
# phrasings Jerry's actual corrections used today. Widening this to catch
# more phrasings is a future tuning pass, not a reason to hold this back --
# a narrow, evidence-grounded detector beats a broad, noisy one.
_CONTACT_CONTEXT_RE = re.compile(r"\bend[- ]users?\b|\bticket history\b|\btheir (?:ticket|issue)s?\b", re.IGNORECASE)

_CONTACT_GAP_RULE_TEXT = (
    "When a Teams message calls someone an 'end user' or asks about their ticket or issue "
    "history without further context, treat it as a ConnectWise ticket-contact question: call "
    "get_ticket_contact / find_tickets_by_contact / ticket_trend_by_requester before replying, "
    "rather than defaulting to an internal-roster interpretation that needs a correction to "
    "trigger the lookup."
)


def _state_db_path() -> Path:
    # Imported lazily: hermes_state.py pulls in a large chunk of the agent
    # runtime, which this module (invoked from a post-job hook) should not
    # have to load just to read a file path.
    from hermes_state import _default_db_path

    return _default_db_path()


def _open_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _extract_tool_names(tool_calls_json: Optional[str]) -> list[str]:
    """Flatten a message row's ``tool_calls`` JSON into a list of tool names.

    Handles both a direct ``function.name`` call and the aggregator shape
    (``function.name == "tool_call"`` wrapping ``arguments.calls[].name``)
    seen in real transcripts -- see the real 2026-09-18 20:21 UTC
    ticket_trend_by_requester call, which arrives wrapped this way.
    """
    if not tool_calls_json:
        return []
    try:
        calls = json.loads(tool_calls_json)
    except (TypeError, json.JSONDecodeError):
        return []
    names: list[str] = []
    for call in calls or []:
        fn = (call or {}).get("function") or {}
        name = fn.get("name")
        if name == "tool_call":
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            for sub in args.get("calls") or []:
                sub_name = (sub or {}).get("name")
                if sub_name:
                    names.append(sub_name)
        elif name:
            names.append(name)
    return names


def gather_teams_activity(date_str: str, *, db_path: Optional[Path] = None) -> dict:
    """Real Teams message activity for one UTC calendar day (``YYYY-MM-DD``):
    counts, distinct sessions, and ordered turn records (role/content/tool
    names) used by detect_contact_context_gaps()."""
    db_path = db_path or _state_db_path()
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    day_end = day_start + 86400
    conn = _open_ro(db_path)
    try:
        rows = conn.execute(
            """
            SELECT m.session_id, m.role, m.timestamp, m.content, m.tool_calls
            FROM messages m JOIN sessions s ON s.id = m.session_id
            WHERE s.source = 'teams' AND m.active = 1
              AND m.timestamp >= ? AND m.timestamp < ?
            ORDER BY m.timestamp ASC
            """,
            (day_start, day_end),
        ).fetchall()
    finally:
        conn.close()
    turns = [
        {
            "session_id": r["session_id"],
            "role": r["role"],
            "timestamp": r["timestamp"],
            "content": r["content"] or "",
            "tool_names": _extract_tool_names(r["tool_calls"]),
        }
        for r in rows
    ]
    return {
        "date": date_str,
        "message_count": len(turns),
        "user_message_count": sum(1 for t in turns if t["role"] == "user"),
        "assistant_message_count": sum(1 for t in turns if t["role"] == "assistant"),
        "sessions_touched": sorted({t["session_id"] for t in turns}),
        "turns": turns,
    }


def detect_contact_context_gaps(teams_activity: dict) -> list[dict]:
    """Deterministic detector generalizing the "end user = ConnectWise ticket
    contact" gap: a user turn asking an end-user/ticket-history question
    where nothing in the immediately following assistant turns (up to the
    next user turn) calls a cw_contact tool. Structural, not
    prompt-compliance-based -- the same signal fires whether a structural
    pre-fetch plugin or a SOUL.md instruction was supposed to catch it and
    didn't, so this keeps working as a backstop after either changes."""
    turns = teams_activity.get("turns") or []
    gaps: list[dict] = []
    i, n = 0, len(turns)
    while i < n:
        turn = turns[i]
        if turn["role"] == "user" and _CONTACT_CONTEXT_RE.search(turn["content"] or ""):
            used_contact_tool = False
            j = i + 1
            while j < n and turns[j]["role"] != "user":
                if any(name in CONTACT_TOOL_NAMES for name in turns[j]["tool_names"]):
                    used_contact_tool = True
                j += 1
            if not used_contact_tool:
                gaps.append({
                    "session_id": turn["session_id"],
                    "timestamp": turn["timestamp"],
                    "user_text": turn["content"],
                })
            i = j
        else:
            i += 1
    return gaps


def gather_ticket_activity(date_str: str, *, cw_db_path: Optional[Path] = None) -> dict:
    """Real ConnectWise ticket activity for the day from the local
    ticket-contact index (cron/cw_contact_index.py), which is refreshed on
    its own schedule and is the same store tools/cw_contact_tool.py queries."""
    cw_db_path = cw_db_path or cw_contact_index.DB_PATH
    conn = cw_contact_index.connect(cw_db_path)
    try:
        rows = conn.execute(
            "SELECT id, contact_name, company_name, status, summary FROM tickets "
            "WHERE date_entered LIKE ? ORDER BY date_entered ASC",
            (f"{date_str}%",),
        ).fetchall()
    finally:
        conn.close()
    tickets_today = [dict(r) for r in rows]
    return {
        "date": date_str,
        "tickets_touched": len(tickets_today),
        "tickets": tickets_today,
        "top_requesters": cw_contact_index.trend_by_requester(
            days=1, min_tickets=1, limit=10, db_path=cw_db_path
        ),
        "top_companies": cw_contact_index.trend_by_company(
            days=1, min_tickets=1, limit=10, db_path=cw_db_path
        ),
    }


def gather_behavior_corrections(date_str: str, *, db_path: Optional[Path] = None) -> list[dict]:
    """Rules proposed today, from behavior_store's own audit trail
    (``created_at`` on the proposal), regardless of current status --
    rejected/retired rows are still corrections Jerry weighed in on that
    day. Reuses behavior_store.history() rather than querying its schema
    directly, so this stays correct if that schema ever changes."""
    rows = behavior_store.history(limit=200, db_path=db_path)
    return [r for r in rows if str(r.get("created_at", "")).startswith(date_str)]


def propose_contact_gap_rule(
    gaps: list[dict], *, date_str: str, db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Auto-propose the standing correction when today's real data shows the
    gap, using the same commit model as
    agent/turn_finalizer.py's _propose_tool_refusal_correction: a PENDING
    row via behavior_store.propose(), never auto-approved, with the day's
    actual evidence as the audit ``reason`` -- a human's approval is a
    single ``approve BEH-<n>`` instead of re-investigating the pattern."""
    if not gaps:
        return None
    sample = gaps[0]
    when = datetime.fromtimestamp(sample["timestamp"], tz=timezone.utc).strftime("%H:%M UTC")
    reason = (
        f"nightly ops review {date_str}: {len(gaps)} Teams turn(s) asked an end-user/"
        f"ticket-history question with no cw_contact tool call in the reply -- e.g. "
        f"{when} session {sample['session_id']}: {sample['user_text'][:160]!r}"
    )
    return behavior_store.propose(
        "instruction", _CONTACT_GAP_RULE_TEXT, scope="ops-review",
        requested_by="ops-memory-nightly-review", reason=reason, db_path=db_path,
    )


def build_dreams_narrative(
    date_str: str, teams_activity: dict, ticket_activity: dict,
    corrections: list[dict], gaps: list[dict], proposal: Optional[dict],
) -> str:
    """Human-readable daily narrative for DREAMS.md, grounded in the
    deterministic gather_* results above rather than free-hand model recall."""
    lines = [f"## {date_str} — End of Day Summary"]
    lines.append(
        f"- Teams activity: {teams_activity['message_count']} message(s) across "
        f"{len(teams_activity['sessions_touched'])} conversation(s) "
        f"({teams_activity['user_message_count']} from users, "
        f"{teams_activity['assistant_message_count']} replies)."
    )
    lines.append(f"- ConnectWise activity: {ticket_activity['tickets_touched']} ticket(s) touched today.")
    if ticket_activity["top_requesters"]:
        top = ", ".join(
            f"{r['contact_name']} ({r['ticket_count']})" for r in ticket_activity["top_requesters"][:5]
        )
        lines.append(f"  Top requesters today: {top}.")
    if ticket_activity["top_companies"]:
        top = ", ".join(
            f"{c['company']} ({c['ticket_count']})" for c in ticket_activity["top_companies"][:5]
        )
        lines.append(f"  Top companies today: {top}.")
    if corrections:
        summary = "; ".join(f"{c['scope']}: {c['text'][:100]}" for c in corrections)
        lines.append(f"- Behavior corrections Jerry made today ({len(corrections)}): {summary}")
    else:
        lines.append("- No behavior corrections logged today.")
    if gaps:
        when = datetime.fromtimestamp(gaps[0]["timestamp"], tz=timezone.utc).strftime("%H:%M UTC")
        lines.append(
            f"- Recurring-behavior gap detected: {len(gaps)} end-user/ticket-history question(s) "
            f"answered without a ConnectWise contact lookup (e.g. {gaps[0]['user_text'][:120]!r} at {when})."
        )
        if proposal:
            state = "deduped against an existing pending/active rule" if proposal.get("deduped") else "newly proposed"
            lines.append(f"  -> Auto-proposed BEH-{proposal.get('id')} ({state}); pending human approval.")
    else:
        lines.append("- No recurring end-user/ticket-contact recognition gaps detected today.")
    return "\n".join(lines) + "\n"


def run_nightly_review(
    date_str: Optional[str] = None, *,
    state_db_path: Optional[Path] = None,
    cw_db_path: Optional[Path] = None,
    behavior_db_path: Optional[Path] = None,
) -> dict:
    """Orchestrates one night's review: gather real data, detect recurring
    gaps, auto-propose a rule for a genuine finding, write the DREAMS.md
    entry. Returns the full result dict (used by tests and by the caller's
    observability log line)."""
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    teams_activity = gather_teams_activity(date_str, db_path=state_db_path)
    ticket_activity = gather_ticket_activity(date_str, cw_db_path=cw_db_path)
    corrections = gather_behavior_corrections(date_str, db_path=behavior_db_path)
    gaps = detect_contact_context_gaps(teams_activity)
    proposal = propose_contact_gap_rule(gaps, date_str=date_str, db_path=behavior_db_path)
    entry = build_dreams_narrative(date_str, teams_activity, ticket_activity, corrections, gaps, proposal)

    from cron import ops_memory

    ops_memory.append_dreams_entry(entry)

    return {
        "date": date_str,
        "teams_activity": teams_activity,
        "ticket_activity": ticket_activity,
        "corrections": corrections,
        "gaps": gaps,
        "proposal": proposal,
        "entry": entry,
    }
