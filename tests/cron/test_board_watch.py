"""
Unit tests for cron/board_watch.py — dedup of board-watcher-001's own
delta findings, fixture Ticket data only, no live CW calls.

This poller now fires on ONE transition: a ticket crossing into the
blocking priority band. "New" and "reopened" were removed on 2026-08-11
because the ConnectWise callback lane announces both in real time, and
this job re-announced the same ticket up to 15 minutes later — two Teams
cards for one ticket.

The critical case is `test_second_run_no_change_is_silent`: a ticket
already reported must not fire again on the next 15-minute cycle unless
it has genuinely changed. This is the exact defect that made ticket
#94689 post 7 times between 08:46 and 10:35 ET on 2026-08-04 — see
board_watch.py's module docstring for the root cause.
"""
from datetime import datetime, timedelta, timezone

import pytest

from cron.board_watch import (
    MAX_DELTAS_PER_CYCLE,
    build_board_prompt_block,
    load_state,
    select_board_deltas,
)
from cron.trend_detection import Ticket, _significant_tokens

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point board_watch state at a scratch file so tests never touch the
    real memories/ops/board_watch_state.json."""
    state_file = tmp_path / "board_watch_state.json"
    monkeypatch.setattr("cron.board_watch.OPS_DIR", tmp_path)
    monkeypatch.setattr("cron.board_watch.STATE_FILE", state_file)
    return state_file


def _ticket(id, priority="Priority 1 - Emergency", status="New", closed=False,
            board="Triage", contact="Nicole McFarland", summary="Cannot log in"):
    return Ticket(
        id=id,
        summary=summary,
        contact=contact,
        status=status,
        closed=closed,
        priority=priority,
        entered_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(minutes=5),
        tokens=_significant_tokens(summary),
        board=board,
    )


class TestNewTicketIsLeftToTheCallbackLane:
    """The webhook route already posted it; a second card is the bug."""

    def test_first_sighting_is_silent(self):
        result = select_board_deltas([_ticket(94689)], NOW)
        assert result.silent
        assert result.fired == []

    def test_first_sighting_is_still_recorded_in_state(self):
        """Silent, but not forgotten: the snapshot is what later diffs against."""
        select_board_deltas([_ticket(94689)], NOW)
        assert "94689" in load_state()

    def test_new_ticket_off_triage_board_does_not_fire(self):
        result = select_board_deltas([_ticket(94691, board="NOC")], NOW)
        assert result.silent


class TestSameTicketUnchangedStaysSilent:
    """THE WHOLE POINT: a ticket reported once must not be reported again
    on the next run when nothing about it has changed."""

    def test_second_run_no_change_is_silent(self):
        select_board_deltas([_ticket(94689)], NOW)

        later = NOW + timedelta(minutes=15)
        second = select_board_deltas([_ticket(94689)], later)
        assert second.silent
        assert second.fired == []

    def test_many_quiet_cycles_stay_silent(self):
        select_board_deltas([_ticket(94689)], NOW)
        for i in range(1, 8):
            t = NOW + timedelta(minutes=15 * i)
            result = select_board_deltas([_ticket(94689)], t)
            assert result.silent, f"re-fired on quiet cycle {i}"

    def test_state_persists_across_calls(self, isolated_state):
        select_board_deltas([_ticket(94689)], NOW)
        state = load_state()
        assert "94689" in state
        assert state["94689"]["severity_band"] == "blocking"


class TestGenuineChangeFiresAgain:
    def test_reopen_after_close_is_left_to_the_callback_lane(self):
        """CW fires a callback on reopen, and that lane posts the card."""
        select_board_deltas([_ticket(94689, closed=True)], NOW)

        later = NOW + timedelta(minutes=15)
        result = select_board_deltas([_ticket(94689, closed=False)], later)
        assert result.silent

    def test_priority_escalation_fires(self):
        select_board_deltas([_ticket(94689, priority="Priority 3 - Medium")], NOW)

        later = NOW + timedelta(minutes=15)
        # still not "new" (already known), still open — but priority
        # crossed into the blocking band, so this must fire.
        result = select_board_deltas(
            [_ticket(94689, priority="Priority 1 - Emergency")], later
        )
        assert not result.silent
        assert result.fired[0].reason == "priority_escalated"

    def test_priority_worsening_within_blocking_band_does_not_refire(self):
        select_board_deltas([_ticket(94689, priority="Priority 2 - High")], NOW)
        later = NOW + timedelta(minutes=15)
        result = select_board_deltas(
            [_ticket(94689, priority="Priority 1 - Emergency")], later
        )
        assert result.silent  # already blocking, still blocking — not new news


class TestNothingToReportIsSilent:
    def test_no_tickets_is_silent(self):
        result = select_board_deltas([], NOW)
        assert result.silent
        assert result.fired == []
        assert result.total_open == 0
        assert build_board_prompt_block(result) is None

    def test_only_unchanged_tickets_is_silent(self):
        select_board_deltas([_ticket(1), _ticket(2)], NOW)
        later = NOW + timedelta(minutes=15)
        result = select_board_deltas([_ticket(1), _ticket(2)], later)
        assert result.silent
        assert build_board_prompt_block(result) is None


def _escalate(count):
    """Take `count` tickets from medium into the blocking band, which is now
    the only transition that fires."""
    tickets = [_ticket(90000 + i, priority="Priority 3 - Medium") for i in range(count)]
    select_board_deltas(tickets, NOW)
    later = NOW + timedelta(minutes=15)
    escalated = [_ticket(90000 + i, priority="Priority 1 - Emergency") for i in range(count)]
    return select_board_deltas(escalated, later)


class TestCapEnforced:
    def test_more_than_budget_is_capped(self):
        result = _escalate(MAX_DELTAS_PER_CYCLE + 3)
        assert len(result.fired) == MAX_DELTAS_PER_CYCLE
        assert result.deferred_count == 3

    def test_prompt_block_names_deferred_count(self):
        result = _escalate(MAX_DELTAS_PER_CYCLE + 2)
        block = build_board_prompt_block(result)
        assert block is not None
        assert str(result.deferred_count) in block


class TestTicketDropsFromState:
    def test_ticket_gone_then_seen_again_is_a_fresh_first_sighting(self):
        select_board_deltas([_ticket(94689)], NOW)
        assert "94689" in load_state()

        # Ticket ages out of the log / gets closed and pruned elsewhere.
        select_board_deltas([], NOW)
        assert "94689" not in load_state()

        # Seen again means no prior snapshot, which is a first sighting, and
        # first sightings belong to the callback lane.
        later = NOW + timedelta(minutes=15)
        result = select_board_deltas([_ticket(94689)], later)
        assert result.silent
        assert "94689" in load_state()
