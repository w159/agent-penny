"""Tier-1 (deterministic) checker adapters for evals/prompt_composition_regression.

Each function here calls a REAL function from the shipped codebase - the same
functions tests/agent/test_behavior_state_aside_strip.py and
tests/plugins/test_roast_variety_context_plugin.py already exercise - and returns
the single string the case's `expected` block is graded against. No network
calls, no model calls, no mutation of repo/deployment state. Registered in
CHECKERS below by the case JSON's `checker` field.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("evals.prompt_composition_regression")

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Collapse whitespace so a must_contain substring survives SOUL.md's
    hand-wrapped line breaks without the case having to encode them."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def check_strip_behavior_state_aside(case: dict) -> str:
    """Runs the real agent/turn_finalizer._strip_behavior_state_aside on the
    case's draft_response, exactly as production runs it on a model's draft
    before send."""
    from agent.turn_finalizer import _strip_behavior_state_aside

    class _FakeAgent:
        """_strip_behavior_state_aside never reads agent state; matches the
        stand-in used by its own test suite."""

    draft = case["checker_input"]["draft_response"]
    return _strip_behavior_state_aside(_FakeAgent(), draft, case["user_message"], logger)


def check_roast_variety_context(case: dict, roster_names: list[str] | None = None) -> str:
    """Runs the real plugins.roast_variety_context pre_llm_call hook against the
    case's conversation_history. roster_names substitutes for a real
    memories/ops/roster.md read (same technique the plugin's own tests use) -
    pass an override to swap in a reconstructed pre-fix module instead."""
    import plugins.roast_variety_context as plugin

    names = roster_names if roster_names is not None else case["checker_input"]["roster_names"]
    original_roster_fn = plugin._roster_names
    plugin._roster_names = lambda: names
    try:
        result = plugin._on_pre_llm_call(
            session_id=case["session_id"],
            platform=case["platform"],
            conversation_history=case["conversation_history"],
        )
    finally:
        plugin._roster_names = original_roster_fn
    return "" if result is None else result["context"]


def check_soul_disambiguation_clause_present(case: dict) -> str:
    """Loads the REAL deployed SOUL.md (default /home/yoda/.hermes, override via
    checker_input.hermes_home) through the real agent.prompt_builder.load_soul_md
    entry point and returns it whitespace-normalized so a must_contain sentence
    survives the file's hand-wrapped line breaks.

    Passes an explicit, generous ``context_length`` so this presence-check is
    immune to ambient truncation-budget resolution: ``load_soul_md`` reads
    ``config.yaml``'s ``context_file_max_chars`` via ``get_hermes_home()``, which
    tests/conftest.py globally redirects to an empty tempdir for the whole pytest
    session (see its session-start HERMES_HOME swap) - that tempdir has no
    ``context_file_max_chars`` override, so without an explicit context_length
    this checker silently fell back to a 20K-char floor and truncated the real
    33K+-char deployed SOUL.md mid-file, producing a false FAIL that depended on
    which other test files ran in the same pytest process (caught live 2026-09-21
    running this exact case alongside tests/tools/test_cw_contact_tool.py - passed
    alone, failed combined). Whether the REAL production truncation budget still
    covers the real file is a separate, legitimate question - see
    check_soul_md_within_configured_budget below, which checks that directly
    against the real config.yaml rather than through ambient resolution."""
    from agent.prompt_builder import load_soul_md

    home = Path(case["checker_input"]["hermes_home"])
    text = load_soul_md(home_override=home, context_length=2_000_000) or ""
    return _normalize(text)


def check_soul_md_within_configured_budget(case: dict) -> str:
    """Real file size of the deployed SOUL.md vs. the real configured
    ``context_file_max_chars`` in config.yaml (read directly, not via ambient
    ``get_hermes_home()`` resolution, which tests/conftest.py redirects for the
    whole pytest session - see check_soul_disambiguation_clause_present's
    docstring for why that matters here). SOUL.md keeps growing as real
    production incidents get fixed in it; if it ever exceeds the configured
    budget, load_soul_md silently truncates it in production and whatever
    landed at the end of the file (right now: the CUSTOMER/END-USER
    disambiguation and the repeat-nudge cap) stops reaching the model at all.
    Returns a description string; the case's ``expected.must_contain`` checks
    for the word 'within-budget'."""
    import yaml

    hermes_home = Path(case["checker_input"]["hermes_home"])
    soul_path = hermes_home / "SOUL.md"
    config_path = hermes_home / "config.yaml"
    soul_len = len(soul_path.read_text(encoding="utf-8"))
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    budget = config.get("context_file_max_chars")
    if not isinstance(budget, (int, float)) or budget <= 0:
        budget = 20000  # the real flat floor load_soul_md falls back to with no override
    margin = int(budget) - soul_len
    status = "within-budget" if margin > 0 else "OVER-BUDGET"
    return f"SOUL.md is {soul_len} chars against a configured budget of {int(budget)} chars ({status}, margin {margin})."


CHECKERS: dict[str, Callable[[dict], str]] = {
    "strip_behavior_state_aside": check_strip_behavior_state_aside,
    "roast_variety_context": check_roast_variety_context,
    "soul_disambiguation_clause_present": check_soul_disambiguation_clause_present,
    "soul_md_within_configured_budget": check_soul_md_within_configured_budget,
}


def run_checker(case: dict, **overrides: Any) -> str:
    checker_id = case["checker"]
    if checker_id not in CHECKERS:
        raise KeyError(f"no tier-1 checker registered for id {checker_id!r}")
    return CHECKERS[checker_id](case, **overrides) if overrides else CHECKERS[checker_id](case)
