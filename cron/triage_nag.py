#!/usr/bin/env python3
"""
Selection and decision engine for Penny's Triage-board ticket triage.

Architecture (2026-09-09): this module answers WHICH open Triage tickets
are candidates, whether each is work-blocking, what escalation tier it
has earned, and which techs can be PROVEN free right now - the same
selection and proof logic the agent-penny-assist plugin's offer
detectors (reschedule/offboarding-split) build on. It no longer renders
any text meant for a human to read.

The content-free "nag" - a Teams message whose only content is "this
ticket is old, someone should look at it" - was removed on 2026-09-09
per the product owner: it offered nothing actionable and only added
burden. build_fact_block() and build_triage_nag_prompt_block(), which
fed a model a fact block so it could narrate that complaint in its own
voice, are deleted along with the triage-nag-001 job's use of them (see
cron/scheduler.py git history and cron/jobs.json). Penny now either
delivers a PROVEN, actionable assist-offer card (agent-penny-assist) or
says nothing about a ticket's age at all.

This replaces the prior design, where this module handed a NagFacts
struct to cron/triage_nag_voice.py's render_nag() for deterministic
template assembly. That renderer was rejected three times by the product
owner: fixing each reported tell (a fixed 5-slot skeleton, rotating
preambles, a free-tech sentence repeated on 9 of 12 messages) grew a new
one. Templates assembled from phrase pools are structurally bad at not
sounding assembled. triage_nag_voice.py and its opener-rotation
bookkeeping are retired along with it - see that module's git history.

What this module still owns, unchanged from the prior design:

  1. Which open Triage tickets are candidates at all (human tickets only).
  2. Whether a ticket is work-blocking, and what escalation tier it has
     earned given its age and nag history.
  3. Which techs can be PROVEN free right now, with the live-data receipt
     for that proof - never a guess.
  4. Anti-parroting state so the same ticket does not get renagged at the
     same tier forever, and a volume cap so one run cannot flood the chat.

Availability is the single highest-risk piece of this feature. Naming a
tech as free when they are not burns the feature's credibility with the
help desk permanently, so probe_tech_availability() only ever returns a
tech when live schedule/time-entry data proves it, and every returned
reason is the literal proof, not a paraphrase. The fact block carries
that reason verbatim as a free_tech line, and omits the line entirely
when availability was not verified - never a placeholder, never a hedge.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from cron.escalation import OPS_DIR
from cron.outage_triage import ACTION_LEAVE, route_ticket

logger = logging.getLogger(__name__)

STATE_FILE = OPS_DIR / "triage_nag_state.json"

TRIAGE_BOARD_ID = 1

# Tuned against real Triage summaries and expected to be revised as more
# tickets are observed. Substring match, case-insensitive, against the
# ticket summary only - never the notes, which run long enough to false-
# positive on unrelated mentions of these phrases.
BLOCKING_PHRASES = (
    "cannot log in",
    "can't log in",
    "cannot access",
    "can't access",
    "locked out",
    "no email",
    "cannot print",
    "can't print",
    "blue screen",
    "password reset",
    "mfa",
    "vpn down",
    "computer won't start",
    "won't turn on",
    "cannot open",
    "can't open",
)

# CW priority band that counts as work-blocking on its own, independent of
# summary wording, mirroring escalation.py's BLOCKING_PRIORITIES.
_BLOCKING_PRIORITY_NAMES = frozenset({"Priority 1 - Emergency", "Priority 2 - High"})

# Escalation tiers by minutes-since-entered. Each row is
# (threshold_minutes, tier); the ticket's tier is the highest threshold its
# age has crossed. Below the first threshold: silent (None). These are the
# user's stated numbers - a table, not literals scattered through the logic.
_BLOCKING_TIER_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (240, "furious"),
    (120, "loud"),
    (60, "named"),
    (30, "poke"),
)
_NON_BLOCKING_TIER_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (24 * 60, "furious"),
    (8 * 60, "loud"),
    (4 * 60, "named"),
    (2 * 60, "poke"),
)

_TIER_RANK = {"poke": 0, "named": 1, "loud": 2, "furious": 3}

# Never re-send the same tier for the same ticket without escalating, unless
# this many minutes have passed since the last nag at that tier.
MIN_RENAG_MINUTES = 45

# Worst-first, capped per run so one sweep cannot flood the chat.
MAX_NAGS_PER_RUN = 3

# The three active, full-license Triage-board technicians this feature is
# scoped to, per the live ConnectWise /system/members + cw_search_members
# probe on 2026-09-09. Excludes service-account/integration members
# (ConnectWise SVC, CalendarSync, ContactSync, passwordboss, Help Desk,
# AgentPenny, NinjaOne - all licenseClass "A"). Ids 26/17 were stale/wrong
# here and silently dropped both real techs from every availability check
# (see _verify_roster: a mismatched id fails that tech closed, not loudly).
#
# Jerry Morgan (155, JMorgan) was added to this tuple earlier the same day
# and must stay out: he is Henssler's Director of IT, the requester of this
# work, not a front-line tech who takes Triage-board tickets. Do not "fix"
# a roster mismatch by re-adding him - if he ever appears missing from an
# availability check, that is correct behavior, not a bug.
TECH_ROSTER = (
    {"id": 167, "name": "Ernesto Velarde", "identifier": "evelarde"},
    {"id": 169, "name": "Scarlet Mendoza", "identifier": "smendoza"},
    {"id": 176, "name": "Jarvis Williams", "identifier": "jwilliams"},
)

# Status/summary language that marks a ticket's most recent time entry as a
# hands-off wait rather than active work - the second qualifying proof of
# availability.
_HANDS_OFF_STATUS_MARKERS = ("waiting", "scheduled", "on hold")

# A status that means a human is actively engaged with the ticket right now.
# This ALWAYS wins over a summary keyword: a summary is free text written
# by whoever logged the entry and can mention "update"/"sync" in a dozen
# unrelated senses (a user's complaint, a passing remark), while a status
# is a structured field the tech or dispatch set deliberately. When both
# are present, trust the status and omit the tech - see defect writeup,
# 2026-08-25 verifier report: "In Progress" + summary containing "sync"
# was previously read as hands-off and produced a false public claim.
_ACTIVE_STATUS_MARKERS = (
    "in progress", "working", "assigned", "in process", "responding",
    "escalated", "new",
)

# Kept intentionally small. Pruned from the original set: "update",
# "sync", and "migrat" are constant in ordinary active help-desk summaries
# ("please give me an update", "cannot sync mail") and are weak evidence
# on their own - they described what the USER wants, not proof the work
# itself is unattended machine time. What remains all describe long
# unattended machine operations that plausibly leave a tech free while
# they run: an install/imaging/patch/reboot pushed and left to finish.
# When in doubt, omit - a smaller free_techs tuple is always the safe
# direction.
_HANDS_OFF_SUMMARY_MARKERS = ("install", "imaging", "reboot", "patch")


# ---------------------------------------------------------------------------
# Ticket age
# ---------------------------------------------------------------------------


def ticket_entered_at(ticket: dict) -> Optional[datetime]:
    """Parse a ticket's true dateEntered.

    TRAP (proven live): dateEntered is empty at the top level of a ticket
    read and lives at ticket["_info"]["dateEntered"] instead. Reading the
    wrong key silently ages every ticket to epoch, which would make every
    ticket look ancient and firing "furious" immediately. Check _info
    first, fall back to the top-level field, and if BOTH are missing
    return None so the caller SKIPS the ticket instead of fabricating an
    age.
    """
    info = ticket.get("_info") if isinstance(ticket.get("_info"), dict) else {}
    raw = info.get("dateEntered") or ticket.get("dateEntered")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_phrase(age_minutes: int) -> str:
    """Plain-English age for the fact block, no raw minute count dumped
    on the reader."""
    if age_minutes < 60:
        return f"{age_minutes} minutes"
    hours = age_minutes / 60
    if hours < 24:
        return f"{hours:.0f} hours" if hours >= 1.5 else "about an hour"
    days = hours / 24
    return f"{days:.0f} days" if days >= 1.5 else "about a day"


# ---------------------------------------------------------------------------
# Ticket selection - human tickets only
# ---------------------------------------------------------------------------


def is_human_ticket(ticket: dict) -> bool:
    """True when the machine-noise classifier says this ticket is a
    human's, reusing cron.outage_triage.route_ticket rather than
    reimplementing shape/contact classification here.

    Called with empty registry/routing/shape_routing tables: this module
    has no historical ticket corpus to learn from, so it relies on the
    structural signals (NEVER_MOVE_SHAPES, contact markers, outage
    signal classification) route_ticket already applies without needing
    learned tables.
    """
    decision = route_ticket(ticket, {}, {}, {})
    return decision.action == ACTION_LEAVE


# ---------------------------------------------------------------------------
# Work-blocking assessment
# ---------------------------------------------------------------------------


def assess_blocking(ticket: dict) -> tuple[bool, str]:
    """Is this ticket work-blocking, and what is the one-line impact.

    A ticket is work-blocking when its summary matches BLOCKING_PHRASES
    or it sits in a high CW priority band. Pure function over a ticket
    dict so BLOCKING_PHRASES can be retuned without touching call sites.
    """
    summary = (ticket.get("summary") or "").strip()
    lowered = summary.lower()
    priority = ((ticket.get("priority") or {}) if isinstance(ticket.get("priority"), dict) else {}).get("name", "")

    matched_phrase = next((phrase for phrase in BLOCKING_PHRASES if phrase in lowered), None)
    if matched_phrase:
        return True, summary or f"summary mentions '{matched_phrase}'"
    if priority in _BLOCKING_PRIORITY_NAMES:
        return True, summary or f"filed as {priority}"
    return False, summary


# ---------------------------------------------------------------------------
# Escalation tiers
# ---------------------------------------------------------------------------


def tier_for(age_minutes: int, *, blocking: bool, prior_nag_count: int) -> Optional[str]:
    """The escalation tier a ticket has earned, or None to stay silent.

    prior_nag_count is accepted for interface symmetry with the caller's
    per-ticket state (a future revision may use it to accelerate
    escalation on repeat offenders) but does not currently change the
    threshold table - the age-based table alone implements the user's
    stated thresholds.
    """
    thresholds = _BLOCKING_TIER_THRESHOLDS if blocking else _NON_BLOCKING_TIER_THRESHOLDS
    for threshold_minutes, tier in thresholds:
        if age_minutes >= threshold_minutes:
            return tier
    return None


# ---------------------------------------------------------------------------
# Tech availability - proof only, never a guess
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TechAvailability:
    """One tech PROVEN free right now, with the receipt for why."""

    name: str
    reason: str


def _schedule_busy_now(client, member_id: int, now: datetime) -> bool:
    """True if a not-done schedule entry for this member spans `now`.

    Uses client.paged() rather than a single client.get(), matching
    _hands_off_time_entry_reason's pattern below: a single-page get()
    with no `page` param silently truncates at pageSize and would miss
    a busy entry that sorts past the first page, wrongly declaring the
    tech free.
    """
    entries = client.paged(
        "/schedule/entries",
        f"member/id={member_id} and doneFlag=false",
        page_size=200,
    ) or []
    now_aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    for entry in entries:
        start = entry.get("dateStart")
        end = entry.get("dateEnd")
        if not start or not end:
            continue
        try:
            start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        except ValueError:
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        if start_dt <= now_aware <= end_dt:
            return True
    return False


def _next_free_time_phrase(client, member_id: int, now: datetime) -> Optional[str]:
    """Plain-English "nothing on his calendar until X" when a schedule
    entry exists later today, else a generic "nothing on his calendar
    today" phrase. Returns None only if the caller should not claim
    anything (handled by the caller checking _schedule_busy_now first)."""
    entries = client.get(
        "/schedule/entries",
        conditions=f"member/id={member_id} and doneFlag=false and dateStart>[{now.isoformat()}]",
        pageSize=1,
    ) or []
    if entries:
        start = entries[0].get("dateStart")
        try:
            start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            return f"nothing on his calendar until {start_dt.strftime('%-I%p').lower()}"
        except (ValueError, TypeError):
            pass
    return "nothing on his calendar for the rest of today"


def _hands_off_time_entry_reason(client, member_id: int, ticket_id: object, now: datetime) -> Optional[str]:
    """If this member's most recent time entry today is against a ticket
    whose status or summary indicates a hands-off wait, return the human
    reason. Otherwise None - never a hedged guess.

    An active status on the latest entry ALWAYS wins over a summary
    keyword (see _ACTIVE_STATUS_MARKERS) - a summary keyword can never
    outvote a status that says the tech is actively engaged. Separately,
    if any OTHER ticket logged today is itself active, the tech is not
    free at all regardless of what the latest entry says: they may be
    mid-context-switch, not actually free.
    """
    since = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    entries = client.paged("/time/entries", f"member/id={member_id} and dateEntered>=[{since}]", page_size=200)
    if not entries:
        return None
    # Most recent by timeStart/dateEntered.
    entries = sorted(entries, key=lambda e: e.get("timeStart") or e.get("dateEntered") or "", reverse=True)
    latest = entries[0]
    latest_ticket = latest.get("ticket") or {}
    ticket_number = latest_ticket.get("id") if isinstance(latest_ticket, dict) else None
    status = ((latest.get("status") or {}) if isinstance(latest.get("status"), dict) else {}).get("name", "")
    summary = (latest.get("summary") or latest_ticket.get("summary") or "") if isinstance(latest_ticket, dict) else ""
    status_lower = status.lower()
    summary_lower = str(summary).lower()

    if any(marker in status_lower for marker in _ACTIVE_STATUS_MARKERS):
        # Active status always wins - never let a summary keyword
        # outvote it, no matter what words appear in the free text.
        return None

    if any(marker in status_lower for marker in _HANDS_OFF_STATUS_MARKERS):
        wait_desc = status
    elif any(marker in summary_lower for marker in _HANDS_OFF_SUMMARY_MARKERS):
        wait_desc = next(m for m in _HANDS_OFF_SUMMARY_MARKERS if m in summary_lower)
    else:
        return None

    # Rule out any OTHER ticket logged today that is itself actively
    # worked - the tech is not "free" if they are still carrying active
    # work elsewhere, even if the most recent entry looks hands-off.
    for entry in entries[1:]:
        other_ticket = entry.get("ticket") or {}
        other_number = other_ticket.get("id") if isinstance(other_ticket, dict) else None
        if other_number is not None and other_number == ticket_number:
            continue
        other_status = ((entry.get("status") or {}) if isinstance(entry.get("status"), dict) else {}).get("name", "")
        if any(marker in other_status.lower() for marker in _ACTIVE_STATUS_MARKERS):
            return None

    # Only one open ticket was seen in today's entries (all others, if
    # any, referenced the same ticket or were not active) - "only" is
    # proven, not assumed.
    other_ticket_ids = {
        (e.get("ticket") or {}).get("id")
        for e in entries[1:]
        if isinstance(e.get("ticket"), dict) and (e.get("ticket") or {}).get("id") is not None
    }
    other_ticket_ids.discard(ticket_number)

    ticket_ref = f"#{ticket_number}" if ticket_number else "his open ticket"
    if other_ticket_ids:
        return f"the ticket he's on is {ticket_ref}, waiting on {wait_desc}"
    return f"his only open ticket is {ticket_ref}, waiting on {wait_desc}"


def _verify_roster(client) -> dict[int, str]:
    """Fetch /system/members ONCE and return {id: full_name} for members
    CW currently reports active. Any failure (network error, malformed
    response) returns an empty dict, which fails every tech closed -
    never fall back to the hardcoded TECH_ROSTER assumption. A stale or
    wrong hardcoded id would silently attribute another person's
    schedule to the wrong name in a company chat, so this is a runtime
    check, not a one-time assumption.
    """
    try:
        members = client.get("/system/members", conditions="inactiveFlag=false") or []
    except Exception:
        logger.warning("triage_nag: /system/members lookup failed, omitting all roster techs", exc_info=True)
        return {}
    verified: dict[int, str] = {}
    for member in members:
        if not isinstance(member, dict) or member.get("inactiveFlag"):
            continue
        member_id = member.get("id")
        if member_id is None:
            continue
        name = f"{member.get('firstName', '')} {member.get('lastName', '')}".strip()
        verified[member_id] = name or str(member.get("identifier", ""))
    return verified


def probe_tech_availability(client, now: datetime) -> tuple[TechAvailability, ...]:
    """Return only the techs from TECH_ROSTER that live data PROVES are
    free right now, each with the plain-language proof.

    Zero-th, mandatory gate: the roster id must verify against a live
    /system/members read this run (see _verify_roster) - a mismatch,
    inactive member, or lookup failure omits the tech outright.

    Two qualifying proofs, checked per tech, first match wins:
      (a) no not-done schedule entry spans `now`.
      (b) the tech's most recent time entry today is against a ticket in
          a hands-off wait (status or summary says so).
    Neither proof holding means the tech is OMITTED - an empty tuple is
    the correct, common, and expected result. This function never
    fabricates a reason from ambiguous data.
    """
    verified_roster = _verify_roster(client)

    free: list[TechAvailability] = []
    for tech in TECH_ROSTER:
        member_id = tech["id"]
        actual_name = verified_roster.get(member_id)
        if not actual_name or actual_name.strip().lower() != tech["name"].strip().lower():
            logger.warning(
                "triage_nag: member id %s failed roster verification (expected %r, live %r) - omitting",
                member_id, tech["name"], actual_name,
            )
            continue

        try:
            busy = _schedule_busy_now(client, member_id, now)
        except Exception:
            logger.warning("triage_nag: schedule probe failed for member %s", member_id, exc_info=True)
            continue

        if not busy:
            free.append(TechAvailability(name=tech["name"], reason=_next_free_time_phrase(client, member_id, now)))
            continue

        try:
            reason = _hands_off_time_entry_reason(client, member_id, None, now)
        except Exception:
            logger.warning("triage_nag: time-entry probe failed for member %s", member_id, exc_info=True)
            continue

        if reason:
            free.append(TechAvailability(name=tech["name"], reason=reason))
        # else: busy AND no hands-off proof -> omitted, no guess.

    return tuple(free)


# ---------------------------------------------------------------------------
# State + selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NagFacts:
    """Everything a caller needs to say something true and specific about
    a ticket. Consumed by agent-penny-assist's offer detectors, never
    assembled into a sentence by this module."""

    ticket_id: int
    summary: str
    contact_label: str
    impact_line: str
    age_minutes: int
    tier: str  # "poke" | "named" | "loud" | "furious"
    owner: str
    prior_nag_count: int
    company: str = ""
    free_techs: tuple[TechAvailability, ...] = ()


@dataclass
class NagCandidate:
    ticket_id: int
    facts: NagFacts
    tier: str


@dataclass
class TriageNagResult:
    fired: list = field(default_factory=list)  # list[NagCandidate], capped
    deferred_count: int = 0
    silent: bool = True


def load_state() -> dict:
    """Read triage_nag_state.json. Missing or corrupt file -> empty state."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("triage_nag: state file unreadable (%s), starting fresh", e)
        return {}


def save_state(state: dict) -> None:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _has_new_note_since(ticket: dict, prev: dict) -> bool:
    last_note_at = ticket.get("_last_note_at")
    return bool(last_note_at) and last_note_at != prev.get("last_note_at")


def select_triage_nags(tickets: list, client, now: datetime) -> TriageNagResult:
    """Build this run's nag candidates from open Triage tickets.

    `tickets` are raw CW ticket dicts (already filtered by the caller to
    board/id=1 and closedFlag=false - see the conditions string in this
    module's docstring/tests). `client` is used only for the tech
    availability probe.
    """
    old_state = load_state()
    new_state: dict = {}
    candidates: list[NagCandidate] = []
    seen_ids = set()

    free_techs = probe_tech_availability(client, now)

    for ticket in tickets:
        if not isinstance(ticket, dict):
            continue
        ticket_id = ticket.get("id")
        if ticket_id is None:
            continue
        seen_ids.add(str(ticket_id))

        if not is_human_ticket(ticket):
            continue

        entered_at = ticket_entered_at(ticket)
        if entered_at is None:
            logger.info("triage_nag: skipping ticket %s, dateEntered unknown", ticket_id)
            continue
        age_minutes = int((now - entered_at).total_seconds() // 60)
        if age_minutes < 0:
            continue

        blocking, impact_line = assess_blocking(ticket)
        prev = old_state.get(str(ticket_id), {})

        company = ((ticket.get("company") or {}).get("name") if isinstance(ticket.get("company"), dict) else "") or ""
        owner = (ticket.get("owner") or "").strip() if isinstance(ticket.get("owner"), str) else ""
        acted_upon = bool(owner) and _has_new_note_since(ticket, prev)
        if acted_upon:
            # Reset and go silent: a human has picked this up.
            new_state[str(ticket_id)] = {
                "prior_nag_count": 0,
                "last_tier": None,
                "last_nagged_at": prev.get("last_nagged_at"),
                "last_note_at": ticket.get("_last_note_at"),
            }
            continue

        prior_nag_count = prev.get("prior_nag_count", 0)
        tier = tier_for(age_minutes, blocking=blocking, prior_nag_count=prior_nag_count)

        carry_forward = dict(prev)
        carry_forward["last_note_at"] = ticket.get("_last_note_at", prev.get("last_note_at"))
        carry_forward.setdefault("prior_nag_count", 0)

        if tier is None:
            new_state[str(ticket_id)] = carry_forward
            continue

        last_tier = prev.get("last_tier")
        last_nagged_at_raw = prev.get("last_nagged_at")
        escalating = last_tier is not None and _TIER_RANK.get(tier, 0) > _TIER_RANK.get(last_tier, -1)

        if last_tier == tier and not escalating and last_nagged_at_raw:
            try:
                last_nagged_at = datetime.fromisoformat(last_nagged_at_raw)
            except ValueError:
                last_nagged_at = None
            if last_nagged_at is not None:
                minutes_since = (now - last_nagged_at).total_seconds() / 60
                if minutes_since < MIN_RENAG_MINUTES:
                    new_state[str(ticket_id)] = carry_forward
                    continue

        facts = NagFacts(
            ticket_id=ticket_id,
            summary=ticket.get("summary") or "",
            contact_label=((ticket.get("contact") or {}).get("name") if isinstance(ticket.get("contact"), dict) else "") or "",
            impact_line=impact_line,
            age_minutes=age_minutes,
            tier=tier,
            owner=owner,
            prior_nag_count=prior_nag_count,
            company=company,
            free_techs=free_techs if tier in ("named", "loud", "furious") else (),
        )

        candidates.append(NagCandidate(ticket_id=ticket_id, facts=facts, tier=tier))

        carry_forward["_pending_prior_nag_count"] = prior_nag_count + 1
        carry_forward["_pending_tier"] = tier
        new_state[str(ticket_id)] = carry_forward

    # Drop tickets that closed or left Triage - they are no longer in
    # `tickets` at all, so anything not touched above is gone already
    # because new_state only ever gets entries from this loop.

    # Worst-first: highest tier, then oldest (largest age_minutes).
    candidates.sort(key=lambda c: (_TIER_RANK.get(c.tier, 0), c.facts.age_minutes), reverse=True)
    kept = candidates[:MAX_NAGS_PER_RUN]
    deferred_count = len(candidates) - len(kept)

    kept_ids = {c.ticket_id for c in kept}
    for ticket_id_str, entry in new_state.items():
        if int(ticket_id_str) in kept_ids and "_pending_tier" in entry:
            entry["prior_nag_count"] = entry.pop("_pending_prior_nag_count")
            entry["last_tier"] = entry.pop("_pending_tier")
            entry["last_nagged_at"] = now.isoformat()
        else:
            entry.pop("_pending_prior_nag_count", None)
            entry.pop("_pending_tier", None)

    save_state(new_state)

    return TriageNagResult(fired=kept, deferred_count=deferred_count, silent=not kept)


# build_fact_block() and build_triage_nag_prompt_block() were removed
# 2026-09-09: they were the only code that turned a ticket's age into
# prose for a human to read, and that prose was the content-free nag the
# product owner rejected. select_triage_nags() above still selects
# candidates, tiers, and proven-free techs - agent-penny-assist's offer
# detectors and CLI (cli.py, schedule.py) consume that output directly
# and are unaffected.
