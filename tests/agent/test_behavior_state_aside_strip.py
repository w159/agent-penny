"""Tests for the structural closing-aside stripper in agent/turn_finalizer.py.

A prior fix relied entirely on prompt wording (SOUL.md's "Relevance rule (hard
rule)" plus _render_behavior_rules_section's "never mention a rule's id ... not
even as a closing aside"). Production proved that insufficient: a real Teams
message, generated after the gateway restart with both instructions live, still
ended with "(Humor parameters BEH-5/6/7 are still active-so while I'm blind, my
snark remains 20/20.)". _strip_behavior_state_aside is the code-level backstop
that runs on the model's actual draft text in the same pre-send pipeline as the
file-mutation footer, so the rule holds even when the model ignores the prompt.
"""
from __future__ import annotations

import logging

from agent.turn_finalizer import _strip_behavior_state_aside

logger = logging.getLogger("test_behavior_state_aside_strip")


class _FakeAgent:
    """_strip_behavior_state_aside doesn't read agent state; a bare stand-in
    matches the convention used for _append_file_mutation_footer's tests."""


# ---------------------------------------------------------------------------
# The exact reported production incident -- reconstructed verbatim, not
# paraphrased, as the regression test.
# ---------------------------------------------------------------------------


class TestStripsTheReportedIncident:
    def test_strips_the_exact_reported_closing_aside(self):
        draft = (
            "The vision tool just threw up its hands-gemma4:31b-cloud doesn't do "
            "image input, so the server kicked back a 400 'invalid image input' "
            "error. In short: I can't look at that picture right now. If you can "
            "describe what's in the image (ticket numbers, error messages, a "
            "weird calendar view, etc.), I'll happily dig into it with the tools "
            "I do have (ConnectWise, Graph schedule, terminal, etc.). Otherwise, "
            "you'd need to switch the vision provider/model to one that actually "
            "understands pictures (like a GPT-4-vision or Llava endpoint) and "
            "we'd update the vision: block in config.yaml first. (Humor "
            "parameters BEH-5/6/7 are still active-so while I'm blind, my snark "
            "remains 20/20.)"
        )
        cleaned = _strip_behavior_state_aside(_FakeAgent(), draft, "can you look at this image?", logger)

        assert "BEH-5" not in cleaned
        assert "Humor parameters" not in cleaned
        assert "snark" not in cleaned.lower()
        assert cleaned.endswith("config.yaml first.")
        # The substantive answer survives untouched.
        assert "I can't look at that picture right now" in cleaned

    def test_strips_a_non_parenthetical_trailing_sentence_variant(self):
        draft = (
            "The patch landed clean. Snark level remains 20/20 while BEH-6 is active."
        )
        cleaned = _strip_behavior_state_aside(_FakeAgent(), draft, "did the patch land?", logger)

        assert cleaned == "The patch landed clean."


# ---------------------------------------------------------------------------
# Legitimate content that superficially resembles the pattern must survive.
# ---------------------------------------------------------------------------


class TestLeavesLegitimateContentAlone:
    def test_direct_question_about_active_rules_is_answered_in_full(self):
        draft = "Right now BEH-5, BEH-6, and BEH-7 are active."
        cleaned = _strip_behavior_state_aside(
            _FakeAgent(), draft, "what behavior rules are active right now", logger
        )

        assert cleaned == draft

    def test_direct_question_with_lead_in_sentence_is_not_truncated(self):
        draft = "Sure -- here's the rundown. BEH-5, BEH-6, and BEH-7 are all active right now."
        cleaned = _strip_behavior_state_aside(
            _FakeAgent(), draft, "which humor parameters are on right now?", logger
        )

        assert cleaned == draft

    def test_reply_with_no_trailing_self_reference_is_untouched(self):
        draft = "Ticket #4821 is still open and assigned to the NOC queue."
        cleaned = _strip_behavior_state_aside(_FakeAgent(), draft, "status on 4821?", logger)

        assert cleaned == draft

    def test_embedded_midsentence_parenthetical_is_not_the_trailing_unit(self):
        draft = (
            "I'll dig into it with the tools I do have (ConnectWise, Graph "
            "schedule, terminal, etc.) and report back shortly."
        )
        cleaned = _strip_behavior_state_aside(_FakeAgent(), draft, "look into this", logger)

        assert cleaned == draft

    def test_stripping_the_whole_reply_would_leave_nothing_so_it_is_left_alone(self):
        # A degenerate draft that is ENTIRELY the aside: no substantive prefix to
        # keep, so stripping it would leave an empty message -- worse than sending
        # the unwanted aside. Left untouched rather than emptied.
        draft = "(Humor parameters BEH-5/6/7 are still active.)"
        cleaned = _strip_behavior_state_aside(_FakeAgent(), draft, "hi", logger)

        assert cleaned == draft

    def test_non_string_and_blank_input_pass_through(self):
        assert _strip_behavior_state_aside(_FakeAgent(), None, "hi", logger) is None
        assert _strip_behavior_state_aside(_FakeAgent(), "   ", "hi", logger) == "   "
