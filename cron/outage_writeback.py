#!/usr/bin/env python3
"""
The only module that changes production ConnectWise data.

When Penny takes an outage-signal ticket into tracking, four things happen to
that ticket: the relevant configuration is attached, an internal note records
that she now holds it, the ticket moves to the destination board, and it
closes. Everything else in this system reads.

Three deliberate constraints, because this is the part that can do damage.

Dry run is the default. `apply_plan` writes nothing unless `apply=True` is
passed explicitly. A caller that forgets still gets the full list of intended
writes back, which is what the review script prints.

The plan is built and validated before any call goes out. A ticket missing an
id, a board id, or a closed-status id produces zero writes rather than a
half-applied ticket stranded between two boards.

The board move is LAST. If the note fails, the ticket stays on Triage where a
human will see it. Moving first would hide a ticket that never got its
tracking note, and a hidden ticket is worse than an untidy queue.

Board and status ids are read from the live tenant and asserted in tests,
because a wrong id moves tickets somewhere nobody is looking.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from cron.outage_triage import ACTION_MOVE_AND_TRACK, BOARD_NOC, BOARD_SOC

__all__ = ["BOARD_IDS", "CLOSED_STATUS_IDS", "TRIAGE_BOARD_ID", "TRIAGE_REOPEN_STATUS_ID",
           "WritePlan", "WriteStep", "apply_plan", "plan_revert", "plan_writeback"]

# Read from the live tenant on 2026-08-25. The NOC board name begins with a
# space; that is not a typo here or anywhere else in this package.
BOARD_IDS = {
    BOARD_NOC: 23,
    BOARD_SOC: 22,
}

# How many configuration rows to attach. One, deliberately.
#
# A service often holds several duplicate configurations - Office 365 has four,
# ShareFile three, PasswordBoss four - because CW hygiene lags reality. They
# describe ONE logical service, so attaching all of them clutters the ticket a
# tech reads without adding information. The lowest id is used so the choice is
# deterministic and the same service always points at the same row.
MAX_CONFIGS_PER_TICKET = 1

# The "Closed" status is per-board in ConnectWise; there is no global one.
CLOSED_STATUS_IDS = {
    BOARD_NOC: 518,
    BOARD_SOC: 515,
}

# Read from the live tenant on 2026-08-28. Used by plan_revert to put a
# wrongly-moved ticket back where a human would have found it: on Triage,
# in the status a fresh ticket would land in ("New"), not silently reopened
# into whatever status it happened to hold on the board it got moved to.
TRIAGE_BOARD_ID = 1
TRIAGE_REOPEN_STATUS_ID = 16

NOTE_TRACKED = (
    "Agent Penny has taken this alert into NOC outage tracking.\n"
    "Outage record: {outage_id}\n"
    "Service: {service}\n"
    "Reason: {reason}\n"
    "This ticket is closed because the outage is now held in Penny's durable "
    "store, which survives the ticket. Ask Penny for current outage status "
    "rather than reopening this."
)

NOTE_UNTRACKED = (
    "Agent Penny moved this machine-generated ticket off the Triage board.\n"
    "Reason: {reason}\n"
    "No outage record was created, so nothing about this is being tracked."
)


@dataclass
class WriteStep:
    kind: str
    method: str
    path: str
    payload: object
    describe: str = ""


@dataclass
class WritePlan:
    ticket_id: object = None
    steps: list = field(default_factory=list)
    skip_reason: str = ""


@dataclass
class WriteResult:
    applied: bool = False
    would_write: list = field(default_factory=list)
    performed: list = field(default_factory=list)
    failed_step: str = ""
    error: str = ""


def plan_writeback(ticket: dict, decision, outage_id=None, config_ids=()) -> WritePlan:
    """Build the exact set of writes for one ticket. Performs none of them."""
    ticket = ticket if isinstance(ticket, dict) else {}
    ticket_id = ticket.get("id")

    if getattr(decision, "action", None) not in (ACTION_MOVE_AND_TRACK, "move_and_close"):
        return WritePlan(ticket_id, [], "decision is leave; nothing to write")

    if ticket_id is None:
        return WritePlan(None, [], "ticket has no id")

    destination = getattr(decision, "destination", "") or ""
    board_id = BOARD_IDS.get(destination)
    status_id = CLOSED_STATUS_IDS.get(destination)
    if board_id is None or status_id is None:
        return WritePlan(ticket_id, [], f"no board id known for destination {destination!r}")

    steps = []
    tracked = decision.action == ACTION_MOVE_AND_TRACK

    # Configurations first: they are additive and harmless on their own, and
    # attaching them before the note means the note is never the odd one out.
    if tracked:
        for config_id in sorted(config_ids or ())[:MAX_CONFIGS_PER_TICKET]:
            steps.append(WriteStep(
                kind="attach_configuration",
                method="POST",
                path=f"/service/tickets/{ticket_id}/configurations",
                payload={"id": config_id},
                describe=f"attach configuration {config_id}",
            ))

    text = (NOTE_TRACKED.format(outage_id=outage_id if outage_id is not None else "(pending)",
                                service=decision.service_key or "unknown",
                                reason=decision.reason)
            if tracked else NOTE_UNTRACKED.format(reason=decision.reason))
    steps.append(WriteStep(
        kind="add_note",
        method="POST",
        path=f"/service/tickets/{ticket_id}/notes",
        payload={"text": text, "internalAnalysisFlag": True, "detailDescriptionFlag": False,
                 "resolutionFlag": False},
        describe="add internal tracking note",
    ))

    # Last on purpose. A failure before this leaves the ticket on Triage.
    #
    # Path form matters. ConnectWise wants a slash path with a SCALAR value
    # ("board/id": 23), not a nested object ("board": {"id": 23}). The nested
    # form is the intuitive one and it is wrong; the working precedent in this
    # repo is memories/ops/cw_assign_helper.py:104, which patches
    # "owner/identifier" the same way.
    steps.append(WriteStep(
        kind="move_and_close",
        method="PATCH",
        path=f"/service/tickets/{ticket_id}",
        payload=[
            {"op": "replace", "path": "board/id", "value": board_id},
            {"op": "replace", "path": "status/id", "value": status_id},
        ],
        describe=f"move to {destination.strip()} and close",
    ))

    return WritePlan(ticket_id, steps)


def plan_revert(ticket_id: object, moved_reason: str = "") -> WritePlan:
    """Build the writes that undo a wrong move: back to Triage, status New.

    This is the operator's undo button for route_ticket() getting it wrong -
    the 2026-08-25 NOC Checks and ThreatLocker misroutes (tickets 96179,
    96190) were reverted by hand because no such tool existed. Board move
    first here, unlike plan_writeback: the whole point is getting the ticket
    back in front of a human immediately, so there is nothing later in the
    plan for a note failure to hide behind.
    """
    if ticket_id is None:
        return WritePlan(None, [], "ticket has no id")

    steps = [
        WriteStep(
            kind="move_and_reopen",
            method="PATCH",
            path=f"/service/tickets/{ticket_id}",
            payload=[
                {"op": "replace", "path": "board/id", "value": TRIAGE_BOARD_ID},
                {"op": "replace", "path": "status/id", "value": TRIAGE_REOPEN_STATUS_ID},
            ],
            describe="move back to Triage and reopen",
        ),
        WriteStep(
            kind="add_note",
            method="POST",
            path=f"/service/tickets/{ticket_id}/notes",
            payload={
                "text": (
                    "Agent Penny reverted an earlier automatic move of this ticket.\n"
                    f"Original reason given for the move: {moved_reason or '(not recorded)'}\n"
                    "This ticket is back on Triage for a human to work."
                ),
                "internalAnalysisFlag": True, "detailDescriptionFlag": False, "resolutionFlag": False,
            },
            describe="add internal revert note",
        ),
    ]
    return WritePlan(ticket_id, steps)


def apply_plan(cw, plan: WritePlan, apply: bool = False) -> WriteResult:
    """Execute a plan. Writes NOTHING unless apply=True.

    Stops at the first failure and reports it. Partial application is possible
    by design - a configuration attached without the board move is harmless
    and visible, whereas rolling back would need writes of its own.
    """
    result = WriteResult(applied=bool(apply))
    if not plan.steps:
        return result

    if not apply:
        result.would_write = [f"{s.method} {s.path}: {s.describe}" for s in plan.steps]
        return result

    for step in plan.steps:
        try:
            cw.send(step.method, step.path, step.payload)
        except Exception as exc:  # noqa: BLE001 - the caller needs the reason, not a traceback
            result.failed_step = step.kind
            result.error = str(exc)
            return result
        result.performed.append(f"{step.method} {step.path}: {step.describe}")

    return result
