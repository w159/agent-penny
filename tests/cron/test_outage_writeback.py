#!/usr/bin/env python3
"""Tests for cron/outage_writeback.py - the only module that changes
production ConnectWise data.

Everything here is checked against a fake client that records calls and
performs none. Two properties carry the most weight:

  Dry run is the default. A caller that forgets to pass apply=True changes
  nothing, and the planned writes still come back so a human can read them.

  The plan is built and validated before any call is made. A ticket that is
  missing a board id, a status id, or an outage reference produces no writes
  at all rather than a half-applied ticket sitting between two boards.
"""
from __future__ import annotations

import pytest

from cron.outage_triage import (
    ACTION_LEAVE,
    ACTION_MOVE_AND_CLOSE,
    ACTION_MOVE_AND_TRACK,
    BOARD_NOC,
    BOARD_SOC,
    TriageDecision,
)
from cron.outage_writeback import (
    BOARD_IDS,
    CLOSED_STATUS_IDS,
    MAX_CONFIGS_PER_TICKET,
    TRIAGE_BOARD_ID,
    TRIAGE_REOPEN_STATUS_ID,
    apply_plan,
    plan_revert,
    plan_writeback,
)


class FakeCW:
    """Records writes instead of performing them."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def send(self, method, path, payload):
        self.calls.append((method, path, payload))
        if self.fail_on and self.fail_on in path:
            raise RuntimeError("connectwise rejected the write")
        return {}


def _ticket(ticket_id=96184, summary="SP1461217: SharePoint Online Service Health Incident (Service Degradation)"):
    return {"id": ticket_id, "summary": summary, "board": {"name": "Triage"},
            "_info": {"dateEntered": "2026-08-25T09:00:00Z"}}


def _track_decision(destination=BOARD_NOC):
    return TriageDecision(action=ACTION_MOVE_AND_TRACK, destination=destination,
                          reason="m365_service_health signal for tracked service SharePoint Online",
                          service_key="sharepoint_online", signal_state="open")


def _close_decision(destination=BOARD_NOC):
    return TriageDecision(action=ACTION_MOVE_AND_CLOSE, destination=destination,
                          reason="'Brivo' is not tracked: no active configuration",
                          signal_state="open")


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def test_a_tracked_move_plans_attach_note_and_close():
    plan = plan_writeback(_ticket(), _track_decision(), outage_id=42, config_ids=(131, 135))
    kinds = [step.kind for step in plan.steps]
    assert kinds == ["attach_configuration", "add_note", "move_and_close"]


def test_duplicate_configurations_attach_only_one_representative():
    """Office 365 holds four configuration rows for one logical service.
    Attaching all four clutters the ticket a tech reads without adding
    information; the lowest id keeps the choice deterministic."""
    plan = plan_writeback(_ticket(), _track_decision(), outage_id=42,
                          config_ids=(577, 131, 150, 135))
    attaches = [s for s in plan.steps if s.kind == "attach_configuration"]
    assert len(attaches) == MAX_CONFIGS_PER_TICKET == 1
    assert attaches[0].payload == {"id": 131}


def test_the_note_names_the_outage_record():
    plan = plan_writeback(_ticket(), _track_decision(), outage_id=42, config_ids=(131,))
    note = [s for s in plan.steps if s.kind == "add_note"][0]
    assert "42" in note.payload["text"]
    assert note.payload["internalAnalysisFlag"] is True
    assert note.payload["detailDescriptionFlag"] is False


def test_an_untracked_move_still_notes_and_closes_but_attaches_nothing():
    plan = plan_writeback(_ticket(96171, "Brivo outage"), _close_decision(), outage_id=None)
    kinds = [step.kind for step in plan.steps]
    assert kinds == ["add_note", "move_and_close"]
    note = [s for s in plan.steps if s.kind == "add_note"][0]
    assert "not tracked" in note.payload["text"].lower()


def test_the_move_uses_the_real_board_and_closed_status_ids():
    plan = plan_writeback(_ticket(), _track_decision(BOARD_SOC), outage_id=None)
    move = [s for s in plan.steps if s.kind == "move_and_close"][0]
    values = {op["path"]: op["value"] for op in move.payload}
    assert values["board/id"] == BOARD_IDS[BOARD_SOC]
    assert values["status/id"] == CLOSED_STATUS_IDS[BOARD_SOC]


def test_the_patch_uses_a_slash_path_with_a_scalar_value():
    """ConnectWise wants {"path": "board/id", "value": 23}, not
    {"path": "board", "value": {"id": 23}}. The nested form is the intuitive
    one and it is rejected. Working precedent in this repo:
    memories/ops/cw_assign_helper.py:104 patches "owner/identifier"."""
    plan = plan_writeback(_ticket(), _track_decision(), outage_id=None)
    move = [s for s in plan.steps if s.kind == "move_and_close"][0]
    for op in move.payload:
        assert "/" in op["path"], op
        assert not isinstance(op["value"], dict), op
        assert op["op"] == "replace"


def test_a_leave_decision_plans_nothing():
    plan = plan_writeback(_ticket(), TriageDecision(action=ACTION_LEAVE, reason="human ticket"),
                          outage_id=None)
    assert plan.steps == []
    assert "leave" in plan.skip_reason.lower()


def test_an_unknown_destination_board_plans_nothing():
    """Better to write nothing than to guess a board id."""
    decision = TriageDecision(action=ACTION_MOVE_AND_CLOSE, destination="Some New Board",
                              reason="machine noise")
    plan = plan_writeback(_ticket(), decision, outage_id=None)
    assert plan.steps == []
    assert "board id" in plan.skip_reason.lower()


def test_a_ticket_with_no_id_plans_nothing():
    plan = plan_writeback({"summary": "x", "board": {"name": "Triage"}}, _track_decision(),
                          outage_id=1)
    assert plan.steps == []


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------

def test_dry_run_is_the_default_and_writes_nothing():
    cw = FakeCW()
    plan = plan_writeback(_ticket(), _track_decision(), outage_id=42, config_ids=(131,))
    result = apply_plan(cw, plan)
    assert cw.calls == []
    assert result.applied is False
    assert len(result.would_write) == 3


def test_apply_true_performs_every_step_in_order():
    cw = FakeCW()
    plan = plan_writeback(_ticket(), _track_decision(), outage_id=42, config_ids=(131,))
    result = apply_plan(cw, plan, apply=True)
    assert result.applied is True
    assert [c[0] for c in cw.calls] == ["POST", "POST", "PATCH"]  # config, note, move
    assert cw.calls[0][1].endswith("/configurations")
    assert cw.calls[1][1].endswith("/notes")
    assert cw.calls[2][1] == "/service/tickets/96184"


def test_the_board_move_is_last_so_a_failure_leaves_the_ticket_where_humans_see_it():
    """If the note fails, the ticket must still be on Triage. Moving first
    would hide a ticket that never got its tracking note."""
    cw = FakeCW(fail_on="/notes")
    plan = plan_writeback(_ticket(), _track_decision(), outage_id=42, config_ids=(131,))
    result = apply_plan(cw, plan, apply=True)
    assert result.failed_step == "add_note"
    assert not any(c[0] == "PATCH" for c in cw.calls), "ticket was moved despite an earlier failure"


def test_a_failure_is_reported_not_swallowed():
    cw = FakeCW(fail_on="/notes")
    plan = plan_writeback(_ticket(), _track_decision(), outage_id=42, config_ids=(131,))
    result = apply_plan(cw, plan, apply=True)
    assert result.error
    assert "rejected" in result.error


def test_an_empty_plan_applies_cleanly():
    cw = FakeCW()
    plan = plan_writeback(_ticket(), TriageDecision(action=ACTION_LEAVE, reason="human"), outage_id=None)
    result = apply_plan(cw, plan, apply=True)
    assert cw.calls == []
    assert result.error == ""


def test_board_ids_match_the_live_tenant():
    """Read from the live tenant on 2026-08-25. A wrong id would move tickets
    onto the wrong board silently."""
    assert BOARD_IDS[BOARD_NOC] == 23
    assert BOARD_IDS[BOARD_SOC] == 22
    assert CLOSED_STATUS_IDS[BOARD_NOC] == 518
    assert CLOSED_STATUS_IDS[BOARD_SOC] == 515


# --------------------------------------------------------------------------
# reverting - the operator's undo for a wrong move (tickets 96179, 96190 on
# 2026-08-25 had to be reverted by hand because no such tool existed)
# --------------------------------------------------------------------------

def test_plan_revert_moves_back_to_triage_and_reopens():
    plan = plan_revert(96179, moved_reason="shape historically routes to NOC")
    kinds = [step.kind for step in plan.steps]
    assert kinds == ["move_and_reopen", "add_note"]
    move = [s for s in plan.steps if s.kind == "move_and_reopen"][0]
    values = {op["path"]: op["value"] for op in move.payload}
    assert values["board/id"] == TRIAGE_BOARD_ID == 1
    assert values["status/id"] == TRIAGE_REOPEN_STATUS_ID == 16


def test_plan_revert_move_is_first_not_last():
    """Unlike plan_writeback, the whole point of a revert is getting the
    ticket back in front of a human immediately - nothing later in the plan
    should stand between the ticket and Triage."""
    plan = plan_revert(96179, moved_reason="wrong")
    assert plan.steps[0].kind == "move_and_reopen"


def test_plan_revert_note_carries_the_original_move_reason():
    plan = plan_revert(96179, moved_reason="shape historically routes to NOC")
    note = [s for s in plan.steps if s.kind == "add_note"][0]
    assert "shape historically routes to NOC" in note.payload["text"]


def test_plan_revert_with_no_ticket_id_plans_nothing():
    plan = plan_revert(None, moved_reason="wrong")
    assert plan.steps == []


def test_plan_revert_can_be_applied_dry_run_by_default():
    cw = FakeCW()
    plan = plan_revert(96179, moved_reason="wrong")
    result = apply_plan(cw, plan)
    assert cw.calls == []
    assert len(result.would_write) == 2
