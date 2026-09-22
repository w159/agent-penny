"""Proves evals/prompt_composition_regression is a real regression net, not a
wrapper that trivially always passes.

For each of the 3 incident cases, runs the SAME grading logic (checkers.grade)
against the pre-fix behavior and confirms it FAILS, then confirms the current
(fixed) code PASSES the same case. Never mutates the working tree:

- footer-self-narration: the fix is an uncommitted change to
  agent/turn_finalizer.py. The pre-fix function body is fetched read-only via
  `git show HEAD:agent/turn_finalizer.py` (HEAD predates this session's fix)
  into a temp file, imported as an isolated module, and never written back.
- repeated-roast-subject: the fix is a brand-new untracked file
  (plugins/roast_variety_context/__init__.py has no HEAD history at all -
  `git show HEAD:...` on it fails, confirmed below). Pre-fix behavior is "no
  structural backstop exists", modeled as a hook that always returns None.
- roast-refusal-disambiguation: SOUL.md lives in ~/.hermes, which is not a git
  repository, so there is no commit to diff against. The pre-fix snapshot is
  reconstructed by removing the exact two disambiguating sentences from the
  REAL current file's text (read once, spliced in memory, never written to
  disk) - the same sentences the case's `expected.must_contain` checks for.

Run: .venv/bin/python evals/prompt_composition_regression/scripts/prove_regression_net.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SUITE_DIR = Path(__file__).parent.parent
for _path in (str(_REPO_ROOT), str(_SUITE_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from runner import grade, load_cases  # noqa: E402


def _case(case_id: str) -> dict:
    for case in load_cases():
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


def _git_show(rel_path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"], cwd=_REPO_ROOT,
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _load_module_from_source(source: str, module_name: str):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(source)
        temp_path = fh.name
    spec = importlib.util.spec_from_file_location(module_name, temp_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def prove_footer_self_narration() -> tuple[bool, bool]:
    """Returns (prefix_failed, current_passed)."""
    case = _case("footer-self-narration-2026-09-21")

    prefix_source = _git_show("agent/turn_finalizer.py")
    prefix_module = _load_module_from_source(prefix_source, "_prefix_turn_finalizer")

    class _FakeAgent:
        pass

    import logging
    logger = logging.getLogger("prove_regression_net.prefix")

    prefix_actual = prefix_module._strip_behavior_state_aside(
        _FakeAgent(), case["checker_input"]["draft_response"], case["user_message"], logger,
    )
    prefix_failures = grade(prefix_actual, case["expected"])

    from runner import run_deterministic_case
    current_result = run_deterministic_case(case)

    print(f"\n=== {case['id']} ===")
    print(f"pre-fix (git show HEAD:agent/turn_finalizer.py) actual output:\n  {prefix_actual!r}")
    print(f"pre-fix grade: {'FAIL' if prefix_failures else 'PASS'} -> {prefix_failures}")
    print(f"current (working tree, fixed) grade: {'PASS' if current_result.passed else 'FAIL'} -> {current_result.failures}")
    return (bool(prefix_failures), current_result.passed)


def prove_repeated_roast_subject() -> tuple[bool, bool]:
    case = _case("repeated-roast-subject-2026-09-21")

    # Confirm the fix genuinely has no HEAD history (i.e. it did not exist before).
    result = subprocess.run(
        ["git", "show", "HEAD:plugins/roast_variety_context/__init__.py"], cwd=_REPO_ROOT,
        capture_output=True, text=True,
    )
    assert result.returncode != 0, "expected no HEAD history for the new plugin file"

    def _prefix_hook(**_kwargs):
        return None  # no structural backstop existed before this fix

    prefix_actual = _prefix_hook(
        session_id=case["session_id"], platform=case["platform"],
        conversation_history=case["conversation_history"],
    )
    prefix_actual = "" if prefix_actual is None else prefix_actual["context"]
    prefix_failures = grade(prefix_actual, case["expected"])

    from runner import run_deterministic_case
    current_result = run_deterministic_case(case)

    print(f"\n=== {case['id']} ===")
    print(f"git show HEAD:plugins/roast_variety_context/__init__.py -> exit {result.returncode} "
          f"(confirms file has no pre-fix version at all)")
    print(f"pre-fix (no hook) actual output: {prefix_actual!r}")
    print(f"pre-fix grade: {'FAIL' if prefix_failures else 'PASS'} -> {prefix_failures}")
    print(f"current (working tree, fixed) grade: {'PASS' if current_result.passed else 'FAIL'} -> {current_result.failures}")
    return (bool(prefix_failures), current_result.passed)


def prove_roast_refusal_disambiguation() -> tuple[bool, bool]:
    case = _case("roast-refusal-disambiguation-2026-09-21")

    from checkers import check_soul_disambiguation_clause_present, _normalize

    current_actual = check_soul_disambiguation_clause_present(case)

    # Splice: remove exactly the two disambiguating sentences the current fix
    # added, from the REAL current text - reconstructing the pre-fix wording
    # without ever touching /home/yoda/.hermes/SOUL.md on disk. SOUL.md is not
    # under version control, so this splice (not `git show`) is the only way
    # to get a faithful pre-fix snapshot.
    removed_sentences = [
        "It is NOT about whether you can discuss, analyze, or joke about an end user "
        "internally to Jerry or the team - that is VOICE & PERSONALITY's end-user roast "
        "budget, a separate internal-only exception, and this rule does not touch it.",
        "It does not override VOICE & PERSONALITY's internal-only end-user roast budget - "
        "that never sends anything to the end user, so there is nothing here for it to "
        "override.",
    ]
    prefix_actual = current_actual
    for sentence in removed_sentences:
        assert sentence in prefix_actual, (
            f"expected fix sentence not found in real SOUL.md - splice target drifted: {sentence!r}"
        )
        prefix_actual = prefix_actual.replace(sentence, "").strip()
    prefix_actual = _normalize(prefix_actual)
    prefix_failures = grade(prefix_actual, case["expected"])

    from runner import run_deterministic_case
    current_result = run_deterministic_case(case)

    print(f"\n=== {case['id']} ===")
    print("pre-fix (real SOUL.md text with the 2 disambiguating sentences spliced out) grade: "
          f"{'FAIL' if prefix_failures else 'PASS'} -> {prefix_failures}")
    print(f"current (real deployed SOUL.md, fixed) grade: {'PASS' if current_result.passed else 'FAIL'} -> {current_result.failures}")
    return (bool(prefix_failures), current_result.passed)


def main() -> int:
    outcomes = {
        "footer-self-narration-2026-09-21": prove_footer_self_narration(),
        "repeated-roast-subject-2026-09-21": prove_repeated_roast_subject(),
        "roast-refusal-disambiguation-2026-09-21": prove_roast_refusal_disambiguation(),
    }
    print("\n=== SUMMARY ===")
    all_ok = True
    for case_id, (prefix_failed, current_passed) in outcomes.items():
        ok = prefix_failed and current_passed
        all_ok = all_ok and ok
        print(f"{'OK' if ok else 'BROKEN'}: {case_id} "
              f"(pre-fix correctly failed: {prefix_failed}, current correctly passed: {current_passed})")
    print(json.dumps({k: {"prefix_failed": v[0], "current_passed": v[1]} for k, v in outcomes.items()}, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
