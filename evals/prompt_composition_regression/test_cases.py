"""pytest entry point for evals/prompt_composition_regression's tier-1 cases.

Not under tests/ on purpose: pyproject.toml's testpaths=["tests"] means the
main suite never auto-collects this file, so it never adds runtime to the
default `pytest` invocation. Run explicitly:

    .venv/bin/python -m pytest evals/prompt_composition_regression/ -v

Each case calls the SAME real functions the fixes shipped in
(agent/turn_finalizer._strip_behavior_state_aside,
plugins.roast_variety_context._on_pre_llm_call,
agent.prompt_builder.load_soul_md) - no mocking of the code under test, only
of the roster.md read (matching the plugin's own test suite's convention).
"""
from __future__ import annotations

import pytest

from runner import load_cases, run_deterministic_case

_DETERMINISTIC_CASES = [c for c in load_cases() if c["tier"] == "deterministic"]
_GENERATION_CASES = [c for c in load_cases() if c["tier"] != "deterministic"]


@pytest.mark.parametrize("case", _DETERMINISTIC_CASES, ids=[c["id"] for c in _DETERMINISTIC_CASES])
def test_deterministic_case_passes(case):
    result = run_deterministic_case(case)
    assert result.passed, "; ".join(result.failures)


def test_all_three_2026_09_21_incidents_are_covered():
    """Guards the eval suite itself: every incident this batch was built to cover
    must have a case on disk, tier-1 or tier-2."""
    all_ids = {c["id"] for c in _DETERMINISTIC_CASES + _GENERATION_CASES}
    assert "footer-self-narration-2026-09-21" in all_ids
    assert "repeated-roast-subject-2026-09-21" in all_ids
    assert "roast-refusal-disambiguation-2026-09-21" in all_ids
