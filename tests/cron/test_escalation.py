"""
Unit tests for cron/escalation.py — dedup/rank/cap of stalled-ticket
escalations. Fixture StallFinding data only; no live CW calls, no Teams
sends.

The critical case is `test_same_ticket_unchanged_stays_silent`: a ticket
already escalated must not fire again on the next cycle unless it has
genuinely crossed into a worse severity band. Re-listing the same stalled
tickets every cycle is exactly the noise pattern that got a previous job
banned (see escalation.py's module docstring).
"""
from datetime import datetime, timezone

import pytest

from cron.escalation import (
    MAX_ESCALATIONS_PER_CYCLE,
    TEAMS_MESSAGE_TICKET_CAP,
    WATCHER_OWN_TICKET_BUDGET,
    build_prompt_block,
    enforce_ticket_cap,
    load_state,
    select_escalations,
)
from cron.trend_detection import StallFinding

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point escalation state at a scratch file so tests never touch the
    real memories/ops/escalation_state.json."""
    state_file = tmp_path / "escalation_state.json"
    monkeypatch.setattr("cron.escalation.OPS_DIR", tmp_path)
    monkeypatch.setattr("cron.escalation.STATE_FILE", state_file)
    return state_file


def _stall(ticket_id, priority="Priority 1 - Emergency", hours_stale=10.0,
           threshold_hours=4, contact="Nicole McFarland"):
    return StallFinding(
        ticket_id=ticket_id,
        contact=contact,
        priority=priority,
        hours_stale=hours_stale,
        threshold_hours=threshold_hours,
        owner_missing=True,
    )


class TestNewCantWorkTicket:
    def test_fires(self):
        result = select_escalations([_stall(93980)], NOW)
        assert not result.silent
        assert len(result.escalate_now) == 1
        assert result.escalate_now[0].ticket_id == 93980
        assert result.escalate_now[0].reason == "new"
        assert result.escalate_now[0].severity_band == "blocking"


class TestSameTicketUnchangedStaysSilent:
    def test_second_run_no_change_is_silent(self):
        """The critical dedup case: identical stall data on the next run
        must NOT fire again."""
        first = select_escalations([_stall(93980)], NOW)
        assert not first.silent

        later = NOW.replace(hour=13)  # an hour later, nothing else changed
        second = select_escalations([_stall(93980, hours_stale=11.0)], later)
        assert second.silent
        assert second.escalate_now == []

    def test_state_persists_across_calls(self, isolated_state):
        select_escalations([_stall(93980)], NOW)
        state = load_state()
        assert "93980" in state
        assert state["93980"]["severity_band"] == "blocking"


class TestWorsenedSeverityFiresAgain:
    def test_crossing_into_worse_band_fires(self):
        # First seen as a merely-stale Priority 3 ticket.
        first = select_escalations(
            [_stall(94016, priority="Priority 3 - Medium", hours_stale=30.0, threshold_hours=24)],
            NOW,
        )
        assert not first.silent
        assert first.escalate_now[0].severity_band == "stale"

        # Second run: still stalled, still no re-fire (same band).
        second = select_escalations(
            [_stall(94016, priority="Priority 3 - Medium", hours_stale=40.0, threshold_hours=24)],
            NOW,
        )
        assert second.silent

        # Third run: priority escalated to Emergency — genuinely worse.
        third = select_escalations(
            [_stall(94016, priority="Priority 1 - Emergency", hours_stale=45.0, threshold_hours=4)],
            NOW,
        )
        assert not third.silent
        assert third.escalate_now[0].reason == "worsened"
        assert third.escalate_now[0].severity_band == "blocking"


class TestQuietPeriod:
    def test_no_stalled_tickets_is_silent(self):
        result = select_escalations([], NOW)
        assert result.silent
        assert result.escalate_now == []
        assert result.total_open_stalled == 0
        assert build_prompt_block(result) is None


class TestCapEnforced:
    def test_more_than_cap_is_capped_and_prioritized(self):
        # More blocking (can't-work) tickets than the cap by itself, plus
        # some merely-stale tickets on top — generalized over the cap value
        # (MAX_ESCALATIONS_PER_CYCLE is now derived from
        # TEAMS_MESSAGE_TICKET_CAP - WATCHER_OWN_TICKET_BUDGET, not a bare 6).
        n_blocking = MAX_ESCALATIONS_PER_CYCLE + 2
        blocking = [
            _stall(i, priority="Priority 1 - Emergency", hours_stale=4.0 + i, threshold_hours=4)
            for i in range(1, n_blocking + 1)
        ]
        stale = [
            _stall(100 + i, priority="Priority 3 - Medium", hours_stale=24.0 + i, threshold_hours=24)
            for i in range(1, 5)
        ]
        result = select_escalations(blocking + stale, NOW)

        assert len(result.escalate_now) == MAX_ESCALATIONS_PER_CYCLE
        assert result.total_open_stalled == n_blocking + 4
        assert result.deferred_count == (n_blocking + 4) - MAX_ESCALATIONS_PER_CYCLE

        # Blocking tickets alone already exceed the cap, so no stale ticket
        # should make it in — blocking ranks strictly before stale.
        selected_bands = {c.severity_band for c in result.escalate_now}
        assert selected_bands == {"blocking"}

    def test_reconciliation_line_present_when_capped(self):
        blocking = [
            _stall(i, priority="Priority 1 - Emergency", hours_stale=4.0 + i, threshold_hours=4)
            for i in range(1, 8)
        ]
        result = select_escalations(blocking, NOW)
        block = build_prompt_block(result)
        assert block is not None
        assert str(result.deferred_count) in block
        assert "roll-up" in block


class TestResolvedTicketDropsFromState:
    def test_ticket_no_longer_stalled_is_pruned_and_refires_if_stalled_again(self):
        select_escalations([_stall(93616)], NOW)
        state_after_first = load_state()
        assert "93616" in state_after_first

        # Ticket picked up / resolved: absent from this cycle's findings.
        select_escalations([], NOW)
        state_after_gone = load_state()
        assert "93616" not in state_after_gone

        # Stalls again later — treated as newly stalled, fires.
        result = select_escalations([_stall(93616)], NOW)
        assert not result.silent
        assert result.escalate_now[0].reason == "new"


class TestCombinedCapArithmetic:
    """DEFECT 1: the escalation cap and the board-watcher job's own
    "max 3 tickets" prompt instruction were two independent, additive
    prompt injections — nothing in Python unified them, so a single
    delivered message could carry up to MAX_ESCALATIONS_PER_CYCLE (old: 6)
    + 3 = 9 tickets. Fix: MAX_ESCALATIONS_PER_CYCLE is now derived so the
    two additions can never together exceed TEAMS_MESSAGE_TICKET_CAP.
    """

    def test_budget_arithmetic_is_named_not_magic(self):
        assert MAX_ESCALATIONS_PER_CYCLE == TEAMS_MESSAGE_TICKET_CAP - WATCHER_OWN_TICKET_BUDGET

    def test_worst_case_selection_plus_watcher_max_never_exceeds_shared_cap(self):
        # Selection at full capacity...
        blocking = [
            _stall(i, priority="Priority 1 - Emergency", hours_stale=4.0 + i, threshold_hours=4)
            for i in range(1, MAX_ESCALATIONS_PER_CYCLE + 1)
        ]
        result = select_escalations(blocking, NOW)
        assert len(result.escalate_now) == MAX_ESCALATIONS_PER_CYCLE

        # ...plus the watcher's own worst case (its full separate budget)
        # must still fit inside the shared cap.
        assert len(result.escalate_now) + WATCHER_OWN_TICKET_BUDGET <= TEAMS_MESSAGE_TICKET_CAP


class TestEnforceTicketCap:
    """DEFECT 1's delivery-time backstop: enforce_ticket_cap() sees the
    fully composed Teams message (whatever produced it) and truncates with
    a roll-up line if it references more than the cap's worth of tickets.
    """

    def _message(self, n, prefix="ticket"):
        lines = [f"- #{10000 + i} {prefix} stuff about it" for i in range(n)]
        return "\n".join(lines)

    def test_exactly_at_cap_is_untouched(self):
        text = self._message(TEAMS_MESSAGE_TICKET_CAP)
        assert enforce_ticket_cap(text) == text

    def test_one_over_cap_is_truncated_with_rollup(self):
        text = self._message(TEAMS_MESSAGE_TICKET_CAP + 1)
        result = enforce_ticket_cap(text)
        assert result != text
        assert "1 more ticket(s) omitted" in result
        # Only cap-worth of ticket lines survive.
        import re
        kept_refs = set(re.findall(r"#(\d+)", result.split("[")[0]))
        assert len(kept_refs) == TEAMS_MESSAGE_TICKET_CAP

    def test_repeated_ticket_reference_does_not_spend_two_slots(self):
        # Cap-worth of unique tickets, but the first one is mentioned twice
        # (e.g. named again in a closing summary) — should not truncate.
        lines = [f"- #{10000 + i} stuff" for i in range(TEAMS_MESSAGE_TICKET_CAP)]
        lines.append("Recap: #10000 is the worst one.")
        text = "\n".join(lines)
        assert enforce_ticket_cap(text) == text

    def test_worst_case_watcher_plus_full_escalation_selection_survives_backstop(self):
        # Simulates the actual worst case: MAX_ESCALATIONS_PER_CYCLE
        # escalation-block tickets plus WATCHER_OWN_TICKET_BUDGET distinct
        # tickets the watcher found on its own — exactly at the shared cap.
        blocking = [
            _stall(i, priority="Priority 1 - Emergency", hours_stale=4.0 + i, threshold_hours=4)
            for i in range(1, MAX_ESCALATIONS_PER_CYCLE + 1)
        ]
        result = select_escalations(blocking, NOW)
        block = build_prompt_block(result)
        watcher_own = "\n".join(
            f"- #{90000 + i} watcher-found delta ticket" for i in range(WATCHER_OWN_TICKET_BUDGET)
        )
        composed = block + "\n" + watcher_own
        assert enforce_ticket_cap(composed) == composed  # exactly at cap, not touched

    def test_no_ticket_references_passes_through(self):
        text = "Nothing stalled this cycle. All quiet on the board."
        assert enforce_ticket_cap(text) == text
