"""Tests for cron/scheduler.py's `outage_routing` opt-in block.

Mirrors tests/cron/test_board_watch_delivery.py's TestBuildJobPromptStashes*
pattern: `_build_job_prompt` is the seam, `cron.outage_dry_run.run_routing`
is patched so nothing here touches ConnectWise or a live outage DB.
"""
from unittest.mock import patch

from cron.scheduler import _build_job_prompt, _is_fragment_response


def _job(**overrides):
    job = {
        "id": "triage-board-sweep",
        "name": "triage-board-sweep",
        "outage_routing": True,
        "prompt": "Report anything the board needs a human for.",
    }
    job.update(overrides)
    return job


def _rows(*, leave=5, non_leave=2):
    table = [
        {"ticket_id": 90000 + i, "summary": "noise", "action": "leave",
         "destination": None, "reason": "no signal match", "already_tracked_run_id": None,
         "would_write": [], "applied": True, "performed": [], "failed_step": "", "error": ""}
        for i in range(leave)
    ]
    for i in range(non_leave):
        table.append({
            "ticket_id": 96689 + i, "summary": "NOC alert",
            "action": "move_and_close", "destination": "Security Operations Center",
            "reason": "known signal", "already_tracked_run_id": None,
            "would_write": [], "applied": True,
            "performed": ["PATCH /service/tickets/{id}: move and close"],
            "failed_step": "", "error": "",
        })
    return table


class TestOutageRoutingPromptInjection:
    def test_non_leave_rows_are_listed_and_flagged_injected(self):
        job = _job()
        with patch("cron.outage_dry_run.run_routing", return_value=_rows(leave=5, non_leave=2)):
            prompt = _build_job_prompt(job)

        assert "## Board routing (done by Python this run)" in prompt
        assert "96689" in prompt
        assert "96690" in prompt
        # Leave rows never show up in the model-facing block.
        assert "90000" not in prompt

    def test_all_leave_rows_inject_nothing(self):
        job = _job()
        with patch("cron.outage_dry_run.run_routing", return_value=_rows(leave=5, non_leave=0)):
            prompt = _build_job_prompt(job)

        assert "## Board routing" not in prompt

    def test_run_routing_exception_is_swallowed_with_a_warning(self):
        job = _job()
        with patch("cron.outage_dry_run.run_routing", side_effect=RuntimeError("CW down")):
            # Must not raise - job still gets a usable prompt.
            prompt = _build_job_prompt(job)

        assert "## Board routing" not in prompt
        assert prompt  # still a real prompt, not blown away

    def test_job_without_outage_routing_flag_never_calls_run_routing(self):
        job = {
            "id": "plain-job",
            "name": "plain",
            "prompt": "Just answer a question.",
        }
        with patch("cron.outage_dry_run.run_routing") as mock_run:
            _build_job_prompt(job)

        mock_run.assert_not_called()


class TestFragmentResponseGuard:
    def test_truncated_silent_marker_is_a_fragment(self):
        assert _is_fragment_response("]")
        assert _is_fragment_response("[")
        assert _is_fragment_response("[]")

    def test_empty_and_whitespace_are_fragments(self):
        assert _is_fragment_response("")
        assert _is_fragment_response("   ")

    def test_real_silent_marker_is_not_a_fragment(self):
        # Real content, handled by _is_cron_silence_response instead.
        assert not _is_fragment_response("[SILENT]")

    def test_short_real_answers_are_not_fragments(self):
        assert not _is_fragment_response("ok")
        assert not _is_fragment_response("3 tickets moved")
