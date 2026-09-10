#!/usr/bin/env python3
"""
Deciding which Triage tickets are NOC noise, and where each one belongs.

This is the module that actually clears the help desk board. Measured on live
open Triage, 13 of 60 tickets were machine-generated NOC or SOC output sitting
in the queue humans work.

The distinction that makes it correct, and the one the earlier phases got
wrong by conflating: whether a ticket should LEAVE Triage is a different
question from whether Penny TRACKS it as an outage. Of those 13, only 5 were
outages against services Henssler runs. Brivo, Sentry and Defender tickets are
not Penny's to track - but they are still noise on the help desk board, and
gating the move on the tracking decision would leave them there forever.

So there are three outcomes:

  MOVE_AND_TRACK  machine outage signal for a tracked service. Moves off
                  Triage and enters the outage store.
  MOVE_AND_CLOSE  machine output that is not a tracked outage. Moves off
                  Triage, no memory kept.
  LEAVE           a human's ticket. Untouched.

Destination is LEARNED from where these tickets historically went, not
asserted from what seems sensible. Over 90 days Microsoft 365 Defender filed
620 tickets to SOC and 18 to NOC; the status feed filed 952 to NOC and 44 to
SOC; Auvik filed 304 to SOC and 55 to NOC, which is not what anyone would
guess about a network monitor. The boards' own history is the policy.

Precision matters more than recall here. Sweeping one real person's ticket
onto the NOC board hides it from the humans who would have worked it, which is
a worse outcome than leaving a piece of noise in the queue. A missing contact
is NOT evidence of a machine: live ticket 96013 is a forwarded marketing email
with no contact, and it stays.
"""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass

from cron.outage_patterns import MACHINE_CONTACT_MARKERS
from cron.outage_signals import classify_ticket

__all__ = [
    "ACTION_LEAVE", "ACTION_MOVE_AND_CLOSE", "ACTION_MOVE_AND_TRACK",
    "BOARD_NOC", "BOARD_SOC", "BOARD_TRIAGE",
    "TriageDecision", "learn_routing", "learn_shape_routing", "route_ticket",
    "summary_shape", "NEVER_MOVE_SHAPES",
]

# The live board name begins with a space. A literal without it silently
# matches nothing.
BOARD_NOC = " Network Operations Center"
BOARD_SOC = "Security Operations Center"
BOARD_TRIAGE = "Triage"

ACTION_MOVE_AND_TRACK = "move_and_track"
ACTION_MOVE_AND_CLOSE = "move_and_close"
ACTION_LEAVE = "leave"

# A contact needs this many historical tickets, and this share of them on one
# board, before its destination counts as learned. Below either threshold the
# structural rules decide instead of a coin flip.
MIN_ROUTING_SAMPLES = 10
MIN_ROUTING_MAJORITY = 0.75

# Where a machine ticket goes when nothing else says. NOC is the safer default:
# a security ticket parked on NOC is still off the help desk board and still
# visible, whereas an operational ticket parked on SOC is off in a queue that
# does not watch for it.
DEFAULT_MACHINE_BOARD = BOARD_NOC

# Shapes a human has told us are NOT machine noise, whatever the board
# history says. Matched as a substring of the normalized shape.
#
# Every entry here overrides learned routing, because a person who reads the
# contents outranks a board statistic. All three were added after the routing
# moved a ticket it should not have.
#
#   Jerry, 2026-08-25:
#     "Email Notification"  - not NOC tickets, though 84 of 126 landed on NOC.
#     "NOC Checks (AM/PM)"  - the name says NOC and the board history agreed
#                             83 to 1, and both were wrong. This is help desk
#                             work, not monitoring output.
#     ThreatLocker, ANY     - requests and approvals are human workflow. The
#                             board history sent them to SOC 68 to 22, which
#                             is where they were WORKED, not where they belong
#                             while open.
#
# The lesson worth keeping: "where these tickets ended up" is not the same
# question as "where does this ticket belong while it still needs a human".
# Learned routing cannot tell those apart, so this list exists.
NEVER_MOVE_SHAPES = (
    "email notification",
    "noc checks",
    "threatlocker",
)

# A summary shape needs this many historical tickets, this share on one
# machine board, and this little presence on Triage before its destination
# counts as learned. The Triage ceiling is the precision guard: a shape humans
# routinely file to the help desk is a human shape no matter where copies of
# it also ended up.
MIN_SHAPE_SAMPLES = 10
MIN_SHAPE_MAJORITY = 0.70
MAX_SHAPE_TRIAGE_SHARE = 0.15

# Structure that marks security-source output, used when the contact is absent
# or unlearned. Substring match on lowercase.
SECURITY_STRUCTURE_MARKERS = (
    "defender", "phish", "threat analytics", "vulnerabilit", "cloud app security",
    "credentials found", "malware", "security threat", "severity alert:",
)


@dataclass
class TriageDecision:
    action: str
    destination: str = ""
    reason: str = ""
    service_key: str = ""
    signal_state: str = ""


def learn_routing(tickets, known_machine_contacts=None) -> dict:
    """Derive {lowercased contact: destination board} from ticket history.

    Only machine contacts are eligible. A real person whose ticket once landed
    on NOC must not become a routing rule that sweeps their future tickets off
    the help desk board.
    """
    counts = collections.defaultdict(collections.Counter)
    for ticket in tickets or []:
        if not isinstance(ticket, dict):
            continue
        contact = _contact(ticket)
        board = ((ticket.get("board") or {}) if isinstance(ticket.get("board"), dict) else {}).get("name", "")
        if not contact or not board or board == BOARD_TRIAGE:
            continue
        if not _looks_like_machine_contact(contact, known_machine_contacts):
            continue
        counts[contact][board] += 1

    routing = {}
    for contact, boards in counts.items():
        total = sum(boards.values())
        board, top = boards.most_common(1)[0]
        if total >= MIN_ROUTING_SAMPLES and (top / total) >= MIN_ROUTING_MAJORITY:
            routing[contact] = board
    return routing


def summary_shape(summary: str) -> str:
    """Collapse a summary to the shape it shares with its siblings.

    "NAP-CONFROOM - Windows Workstation" and "GWH-MJ0HSEXZ - Windows
    Workstation" are the same shape; the device name is the only difference
    and it is the part that varies 424 times.
    """
    text = " ".join((summary or "").split())
    if not text:
        return ""
    # Hostname-ish and filename-ish tokens, then bare numbers.
    text = re.sub(r"\b[A-Za-z0-9]{2,}[-_.][A-Za-z0-9\-_.]+\b", "<X>", text)
    text = re.sub(r"\b\d+\b", "<N>", text)
    return text.lower()[:80]


def learn_shape_routing(tickets) -> dict:
    """Derive {summary shape: destination board} from ticket history.

    This is the generalization of learn_routing. Keying only on contact missed
    424 "<device> - Windows Workstation" tickets, which carry no contact at
    all and are unmistakably machine output. The shape carries the signal that
    the contact does not.
    """
    counts = collections.defaultdict(collections.Counter)
    for ticket in tickets or []:
        if not isinstance(ticket, dict):
            continue
        board = ((ticket.get("board") or {}) if isinstance(ticket.get("board"), dict) else {}).get("name", "")
        shape = summary_shape(ticket.get("summary"))
        if shape and board:
            counts[shape][board] += 1

    routing = {}
    for shape, boards in counts.items():
        total = sum(boards.values())
        if total < MIN_SHAPE_SAMPLES:
            continue
        if (boards.get(BOARD_TRIAGE, 0) / total) > MAX_SHAPE_TRIAGE_SHARE:
            continue  # humans file this shape; not ours to move
        machine = collections.Counter({b: n for b, n in boards.items() if b != BOARD_TRIAGE})
        if not machine:
            continue
        board, top = machine.most_common(1)[0]
        if (top / total) >= MIN_SHAPE_MAJORITY:
            routing[shape] = board
    return routing


def route_ticket(ticket: dict, registry: dict, routing: dict = None,
                 shape_routing: dict = None) -> TriageDecision:
    """Decide what to do with one ticket. Never raises, never returns None."""
    ticket = ticket if isinstance(ticket, dict) else {}
    board = ((ticket.get("board") or {}) if isinstance(ticket.get("board"), dict) else {}).get("name", "")

    # Penny tidies the help desk board only. A ticket a human deliberately
    # filed elsewhere is not hers to move again.
    if board and board != BOARD_TRIAGE:
        return TriageDecision(ACTION_LEAVE, reason=f"already on {board.strip()}, not Penny's to move")

    signal = classify_ticket(ticket, registry)
    contact = _contact(ticket)
    routing = routing or {}
    shape_routing = shape_routing or {}

    shape = summary_shape(ticket.get("summary"))
    if any(marker in shape for marker in NEVER_MOVE_SHAPES):
        return TriageDecision(ACTION_LEAVE, reason="shape is on the never-move list")
    learned_shape_board = shape_routing.get(shape)

    machine_by_contact = contact in routing or _looks_like_machine_contact(contact, None)
    machine_by_structure = signal.state != "unclassified" or signal.stream != "infrastructure"

    # A recognized outage transition for a tracked service: move it and keep
    # the memory.
    if signal.state in ("open", "clear", "retracted", "maintenance") and signal.service_key:
        return TriageDecision(
            ACTION_MOVE_AND_TRACK,
            destination=_destination(contact, ticket, routing),
            reason=f"{signal.stream} signal for tracked service {signal.service_name}",
            service_key=signal.service_key,
            signal_state=signal.state,
        )

    # Machine output that is not a tracked outage. Still noise, still goes.
    if machine_by_contact and (signal.state != "unclassified" or _has_machine_shape(ticket, signal)):
        return TriageDecision(
            ACTION_MOVE_AND_CLOSE,
            destination=_destination(contact, ticket, routing),
            reason=signal.reason or f"machine output from '{contact}', not tracked as an outage",
            signal_state=signal.state,
        )

    # A shape that history says belongs on a machine board. This is what
    # catches the device alerts and vendor request tickets that carry no
    # contact and no outage wording.
    if learned_shape_board:
        return TriageDecision(
            ACTION_MOVE_AND_CLOSE,
            destination=learned_shape_board,
            reason=f"summary shape historically routes to {learned_shape_board.strip()}",
            signal_state=signal.state,
        )

    # Structure alone is enough when the contact is missing, but ONLY real
    # structure. A missing contact is not evidence of a machine.
    if not contact and signal.state in ("open", "clear", "retracted", "excluded", "informational") \
            and signal.stream != "infrastructure":
        return TriageDecision(
            ACTION_MOVE_AND_CLOSE,
            destination=_destination(contact, ticket, routing),
            reason=f"{signal.stream} structure with no contact; {signal.reason or 'not a tracked service'}",
            signal_state=signal.state,
        )

    return TriageDecision(
        ACTION_LEAVE,
        reason="no machine contact and no monitoring structure; treated as a human ticket",
        signal_state=signal.state,
    )


def _destination(contact: str, ticket: dict, routing: dict) -> str:
    learned = routing.get(contact)
    if learned:
        return learned
    summary = (ticket.get("summary") or "").lower()
    if any(marker in summary for marker in SECURITY_STRUCTURE_MARKERS):
        return BOARD_SOC
    return DEFAULT_MACHINE_BOARD


def _has_machine_shape(ticket: dict, signal) -> bool:
    """Guards the one case a learned contact cannot: a machine contact that
    also relays ordinary mail. Requires the summary to look like monitoring
    output rather than correspondence."""
    summary = (ticket.get("summary") or "").strip().lower()
    if not summary:
        return False
    if summary.startswith(("fw:", "fwd:", "re:")):
        return False
    return bool(signal.reason) or signal.state != "unclassified"


def _contact(ticket: dict) -> str:
    contact = ticket.get("contact")
    name = contact.get("name") if isinstance(contact, dict) else None
    return " ".join((name or "").lower().split())


def _looks_like_machine_contact(contact: str, known) -> bool:
    if not contact:
        return False
    if known and contact in {c.lower() for c in known}:
        return True
    return any(marker in contact for marker in MACHINE_CONTACT_MARKERS)
