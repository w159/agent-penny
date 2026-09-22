"""Regression tests for runner.py's tier-2 (run_generation_tier).

Fast/fake by default (no network, no model call) so a future edit to the composition or
scoring wiring is caught without a live model, exactly the CI-safety contract
test_cases.py's tier-1 tests already give the deterministic checkers. The one live class at
the bottom exercises the real default_generate/default_llm_judge path end to end (a live
model call), gated the same double way tests/run_agent/test_fireworks_live.py gates its own
live call: HERMES_LIVE_TESTS=1 AND pytest.mark.integration (excluded from this repo's default
`pytest -m 'not integration'` run).

Not under tests/ on purpose, matching this directory's own test_cases.py convention:
pyproject.toml's testpaths=["tests"] never auto-collects this file into the main suite.

    .venv/bin/python -m pytest evals/prompt_composition_regression/ -v
    HERMES_LIVE_TESTS=1 .venv/bin/python -m pytest evals/prompt_composition_regression/ -v -m integration
"""
from __future__ import annotations

import os

import pytest

from runner import (
    GenerationJudgeNotWired,
    _build_generation_system_prompt,
    load_cases,
    run_generation_case_once,
    run_generation_tier,
)

_CASE = next(c for c in load_cases() if c["id"] == "roast-refusal-disambiguation-2026-09-21")

# Same self-narrating-footer shape as cases/footer_self_narration_2026_09_21.json's real
# fixture — proves the REAL _strip_behavior_state_aside actually runs on the fake draft,
# not just that the wiring plumbs a string through unchanged.
_FAKE_DRAFT = (
    "Sure — Jarvis Williams' backlog is aging like forgotten yogurt in the break fridge. "
    "(Humor parameters: MAX. Useful: still baked in.) \U0001F604"
)


def _fake_generate(system_prompt, conversation_history, user_message):
    assert system_prompt  # composed for real — see TestGenerationSystemPromptComposition
    return _FAKE_DRAFT, "stop"


def _fake_judge(passed):
    def _judge(final_text, pass_criteria, fail_criteria):
        assert pass_criteria and fail_criteria  # criteria always come from the case, never blank
        return {"passed": passed, "reasoning": "fake"}
    return _judge


class TestGenerationSystemPromptComposition:
    """run_generation_tier feeds the model the REAL, unmocked
    agent.system_prompt.build_system_prompt output for the real deployed SOUL.md — not a
    fixture string — so a prompt-assembly regression here breaks this test the same way it
    would break a live turn."""

    def test_real_soul_md_disambiguation_text_reaches_the_prompt(self):
        prompt = _build_generation_system_prompt(_CASE)
        normalized = " ".join(prompt.split())
        assert "CUSTOMER / END-USER COMMUNICATION" in prompt
        assert "does not touch it" in normalized
        assert "does not override VOICE & PERSONALITY's internal-only end-user roast budget" in normalized

    def test_composes_without_crashing_for_the_case_shape(self):
        prompt = _build_generation_system_prompt(_CASE)
        assert isinstance(prompt, str) and len(prompt) > 1000


class TestRunGenerationCaseOnceWiring:
    """No live model call (generate_fn injected) — pins that the REAL
    _strip_behavior_state_aside still runs on whatever draft comes back."""

    def test_strip_behavior_state_aside_runs_on_the_draft(self):
        result = run_generation_case_once(_CASE, generate_fn=_fake_generate)
        assert result["draft_response"] == _FAKE_DRAFT
        assert "Humor parameters" not in result["final_response"]
        assert "\U0001F604" not in result["final_response"]
        assert "Jarvis Williams" in result["final_response"]
        assert result["stripped"] is True
        assert result["finish_reason"] == "stop"


class TestRunGenerationTierScoring:
    """PASS/FAIL comes from judge_fn's verdict, not from string-matching the draft — pinned
    here with fake generate/judge so a future refactor can't silently start grading on
    substring presence instead."""

    def test_passes_when_judge_reports_pass(self):
        result = run_generation_tier(_CASE, generate_fn=_fake_generate, judge_fn=_fake_judge(True))
        assert result.passed is True
        assert result.tier == "generation"
        assert result.case_id == _CASE["id"]
        assert result.details["judge_verdict"]["passed"] is True

    def test_fails_when_judge_reports_fail(self):
        result = run_generation_tier(_CASE, generate_fn=_fake_generate, judge_fn=_fake_judge(False))
        assert result.passed is False
        assert result.failures
        assert "pass_criteria=" in result.failures[0]

    def test_uses_the_case_generation_block_criteria_verbatim(self):
        expected_pass = _CASE["generation_case"]["judge"]["pass_criteria"]
        expected_fail = _CASE["generation_case"]["judge"]["fail_criteria"]
        captured = {}

        def _capturing_judge(final_text, pass_criteria, fail_criteria):
            captured["pass_criteria"], captured["fail_criteria"] = pass_criteria, fail_criteria
            return {"passed": True}

        run_generation_tier(_CASE, generate_fn=_fake_generate, judge_fn=_capturing_judge)
        assert captured["pass_criteria"] == expected_pass
        assert captured["fail_criteria"] == expected_fail

    def test_raises_named_error_with_no_judge_fn(self):
        with pytest.raises(GenerationJudgeNotWired):
            run_generation_tier(_CASE, generate_fn=_fake_generate, judge_fn=None)


_LIVE = os.environ.get("HERMES_LIVE_TESTS") == "1"


@pytest.mark.skipif(not _LIVE, reason="live-only: set HERMES_LIVE_TESTS=1")
@pytest.mark.integration
def test_default_generate_and_judge_are_real_and_reach_a_verdict():
    """Opt-in only: a real model call (default_generate) and a real judge call
    (default_llm_judge) through the actual shipped defaults, no injection. Excluded from the
    default `pytest -m 'not integration'` run and from a bare `-m integration` run without
    HERMES_LIVE_TESTS=1 — the same double gate tests/run_agent/test_fireworks_live.py uses
    for its own live model call."""
    result = run_generation_tier(_CASE)
    assert result.tier == "generation"
    assert result.details["draft_response"]
    assert result.details["finish_reason"]
    assert isinstance(result.passed, bool)
