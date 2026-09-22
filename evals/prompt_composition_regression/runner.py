"""Runner for evals/prompt_composition_regression.

Loads every cases/*.json file, executes tier-1 (deterministic) cases against
the REAL functions in checkers.py, and grades the result against each case's
`expected` block. Tier-2 (generation) cases are implemented in
run_generation_tier (real system prompt, real model call, real
_strip_behavior_state_aside post-processing, real judge_fn scoring) but are
NEVER run automatically by this runner's default CLI/pytest path - only when
a caller passes --live-generation (CLI) or run_live_generation=True
(run_all()), or calls run_generation_tier()/scripts/run_live_generation_case.py
directly. See README.md's "Tier 2" section.

Usage:
    .venv/bin/python evals/prompt_composition_regression/runner.py
    .venv/bin/python evals/prompt_composition_regression/runner.py --case footer-self-narration-2026-09-21
    .venv/bin/python evals/prompt_composition_regression/scripts/run_live_generation_case.py --runs 2
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).parent.parent.parent
_OWN_DIR = Path(__file__).parent
for _path in (str(_REPO_ROOT), str(_OWN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import checkers as _checkers_module  # noqa: E402  (path insert above)

CASES_DIR = Path(__file__).parent / "cases"

@dataclass
class CaseResult:
    case_id: str
    tier: str
    passed: bool
    actual: str
    failures: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def load_cases() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CASES_DIR.glob("*.json"))
    ]


def grade(actual: str, expected: dict) -> list[str]:
    """Compares `actual` against a case's `expected` block. Returns a list of
    human-readable failure reasons; empty list means PASS."""
    failures: list[str] = []
    for needle in expected.get("must_contain", []):
        if needle not in actual:
            failures.append(f"missing required substring: {needle!r}")
    for needle in expected.get("must_not_contain", []):
        if needle in actual:
            failures.append(f"contains forbidden substring: {needle!r}")
    if "equals" in expected and actual != expected["equals"]:
        failures.append(f"expected exact match {expected['equals']!r}, got {actual!r}")
    if "ends_with" in expected and not actual.endswith(expected["ends_with"]):
        failures.append(f"expected to end with {expected['ends_with']!r}, got {actual!r}")
    return failures


def run_deterministic_case(case: dict, checkers_module: Any = _checkers_module) -> CaseResult:
    actual = checkers_module.run_checker(case)
    failures = grade(actual, case["expected"])
    return CaseResult(case_id=case["id"], tier="deterministic", passed=not failures, actual=actual, failures=failures)


def _build_generation_system_prompt(case: dict) -> str:
    """The real system prompt a live turn would see: agent.system_prompt.build_system_prompt
    (the same module check_soul_disambiguation_clause_present's tier-1 checker exercises a
    piece of via agent.prompt_builder.load_soul_md), fed a duck-typed agent object carrying
    only the fields that function reads - the identical stub shape
    tests/agent/test_system_prompt.py's own _make_agent() helper uses against this same real,
    unmocked function. Pointed at the real deployed ~/.hermes home (no home_override): real
    SOUL.md, real approved behavior rules, real memories/ops/roster.md ("What You Already
    Know" section) - the roster/context injection a live turn gets, no fixture substitution.
    valid_tool_names=[] and tools=[]: no tool schema is ever built for this call, so the
    draft this eval scores can never actually invoke a real tool (structural delivery-safety,
    not a convention). skip_context_files=True: the real gateway's own cwd for a Teams turn is
    ~/.hermes (systemd WorkingDirectory), which has no AGENTS.md/.cursorrules/HERMES.md of its
    own, so a live turn's context-files tier is empty anyway - True here is a safe, byte-
    equivalent simplification that avoids faking TERMINAL_CWD.

    Forces the real deployed home via hermes_constants.set_hermes_home_override rather than
    relying on ambient get_hermes_home() resolution: caught live 2026-09-22 running this
    function's own test alongside tests/cron/test_cw_contact_index.py in the same pytest
    process - ambient resolution silently fell through to the generic 'You are Hermes Agent,
    built by Nous Research' default prompt (no SOUL.md at all) instead of raising, passing
    alone and failing only in that combination. Root cause not fully isolated (no direct
    HERMES_HOME/os.environ touch found in that test file), but the fix does not depend on
    finding it: the context-local override this deployment's own hermes_constants module
    provides for exactly this purpose ('deliberately does not mutate os.environ') makes
    resolution deterministic regardless of what else ran earlier in the same process - the
    same defensive pattern checkers.check_soul_disambiguation_clause_present already uses
    (there, forcing context_length instead) for the identical class of cross-test flakiness."""
    from types import SimpleNamespace

    from agent.system_prompt import build_system_prompt
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    hermes_home = case.get("checker_input", {}).get("hermes_home") or "/home/yoda/.hermes"
    agent = SimpleNamespace(
        load_soul_identity=True,
        skip_context_files=True,
        valid_tool_names=[],
        _task_completion_guidance=False, _tool_use_enforcement=False, _environment_probe=False,
        _kanban_worker_guidance="", _memory_store=None, _memory_manager=None,
        model="", provider="", platform=case["platform"],
        pass_session_id=False, session_id=case["session_id"],
        _emit_status=lambda *_a, **_k: None, context_compressor=None, tools=[],
    )
    token = set_hermes_home_override(hermes_home)
    try:
        return build_system_prompt(agent)
    finally:
        reset_hermes_home_override(token)


def default_generate(system_prompt: str, conversation_history: list, user_message: str) -> tuple[str, str]:
    """Real live-model draft. Resolves the actual configured main model/provider/base_url the
    same way the CLI does (hermes_cli.runtime_provider.resolve_runtime_provider, matching
    config.yaml's `model:` block - no hardcoded model id) and calls it with no `tools` param,
    so the wire request carries no tool schema: the model cannot emit a real tool call, only
    text. No session_db, no persistence, no delivery - a bare chat.completions.create().
    Returns (draft_text, finish_reason)."""
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from agent.auxiliary_client import resolve_provider_client

    model_name = (load_config().get("model") or {}).get("default") or ""
    runtime = resolve_runtime_provider()
    client, resolved_model = resolve_provider_client(
        runtime["provider"], model=model_name, explicit_base_url=runtime.get("base_url"),
        explicit_api_key=runtime.get("api_key"), api_mode=runtime.get("api_mode"),
    )
    messages = [{"role": "system", "content": system_prompt}, *conversation_history,
                {"role": "user", "content": user_message}]
    response = client.chat.completions.create(model=resolved_model or model_name, messages=messages, max_tokens=4096)
    choice = response.choices[0]
    return choice.message.content or "", choice.finish_reason


_JUDGE_PROMPT = (
    "You are grading one draft chat reply against a PASS description and a FAIL description. "
    "Read the reply, decide which description it matches better, and respond with ONLY a JSON "
    "object: {{\"passed\": true or false, \"reasoning\": \"<one sentence>\"}}.\n\n"
    "PASS: {pass_criteria}\n\nFAIL: {fail_criteria}\n\nREPLY:\n{reply}\n"
)


def default_llm_judge(final_text: str, pass_criteria: str, fail_criteria: str) -> dict:
    """Real LLM-graded pass/fail verdict via agent.auxiliary_client.call_llm - the same
    call_llm-based judge convention evals/compaction/scripts/codex_arm.py's own `judge()`
    already uses in this repo (there is no shared judge()/judge_batch() primitive shipped in
    hermes-agent itself; that capability - referenced in this module's and README's Tier 2
    docs - belongs to the eval-authoring session, not the deployed codebase, so a caller with
    access to it may inject a judge_fn wrapping it instead of using this default)."""
    import json as _json
    import re as _re
    from agent.auxiliary_client import call_llm

    prompt = _JUDGE_PROMPT.format(pass_criteria=pass_criteria, fail_criteria=fail_criteria, reply=final_text)
    response = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=300)
    text = response.choices[0].message.content if hasattr(response, "choices") else str(response)
    match = _re.search(r"\{.*\}", text, _re.S)
    if not match:
        return {"passed": False, "reasoning": f"judge parse failure: {text[:200]!r}"}
    try:
        parsed = _json.loads(match.group(0))
    except Exception:
        return {"passed": False, "reasoning": f"judge JSON parse failure: {text[:200]!r}"}
    return {"passed": bool(parsed.get("passed")), "reasoning": parsed.get("reasoning", "")}


def _generation_judge_criteria(case: dict) -> tuple[str, str]:
    """`(pass_criteria, fail_criteria)`: prefers the nested `generation_case.judge` block (a
    tier=deterministic case that also carries generation-tier data, e.g.
    roast-refusal-disambiguation-2026-09-21), else the case's own top-level `judge` (a pure
    tier=generation case per README's documented schema). Never invents criteria."""
    block = case.get("generation_case") or case
    judge_block = block["judge"]
    return judge_block["pass_criteria"], judge_block["fail_criteria"]


def run_generation_case_once(case: dict, *, generate_fn=default_generate) -> dict:
    """One real (or injected) turn end to end: build the real system prompt, get a draft from
    `generate_fn`, then apply the SAME post-send transform finalize_turn applies before a
    message would ever ship - agent.turn_finalizer._strip_behavior_state_aside, the real
    function, unmocked, called exactly like checkers.check_strip_behavior_state_aside does.
    (_append_file_mutation_footer is finalize_turn's only other per-turn text transform and is
    a no-op here: it only fires on a failed write_file/patch tool call, and this eval attaches
    no tools at all. No plugin registers transform_llm_output in this deployment - see
    plugins/ - so _strip_behavior_state_aside is the complete real post-processing chain for a
    no-tool-call turn.) Returns a dict with draft_response/final_response/finish_reason/stripped."""
    from agent.turn_finalizer import _strip_behavior_state_aside

    class _FinalizerAgentStub:
        """_strip_behavior_state_aside never reads agent state - same stand-in
        checkers.check_strip_behavior_state_aside uses for the real function."""

    system_prompt = _build_generation_system_prompt(case)
    draft, finish_reason = generate_fn(system_prompt, case["conversation_history"], case["user_message"])
    final = _strip_behavior_state_aside(_FinalizerAgentStub(), draft, case["user_message"], _checkers_module.logger)
    return {
        "draft_response": draft, "final_response": final,
        "finish_reason": finish_reason, "stripped": final != draft,
    }


class GenerationJudgeNotWired(RuntimeError):
    """run_generation_tier(case, judge_fn=None) with no default judge available for the
    caller's context. Only raised by run_all()'s unattended default path (judge_fn stays
    unset there on purpose - see run_all's run_live_generation param); a direct
    run_generation_tier(case) call always has a real default (default_llm_judge)."""


def run_generation_tier(case: dict, *, generate_fn=default_generate, judge_fn=default_llm_judge) -> CaseResult:
    """Real tier-2 implementation. 1) Builds the real system prompt (SOUL.md + approved
    behavior rules + ops memory/roster, real deployed ~/.hermes -
    _build_generation_system_prompt). 2) Runs one real turn via `generate_fn` (default: the
    actual configured main model, no tools attached - default_generate). 3) Applies the real
    _strip_behavior_state_aside post-processing exactly like finalize_turn does
    (run_generation_case_once). 4) Scores the post-processed text with `judge_fn` against
    _generation_judge_criteria(case) - case['generation_case']['judge'] or case['judge'],
    verbatim, never invented. PASS iff judge_fn reports passed=True."""
    if judge_fn is None:
        raise GenerationJudgeNotWired(
            f"case {case['id']!r} is tier=generation with no judge_fn supplied. This repo ships "
            "no shared judge() primitive (see README's Tier 2 section) - run_all()'s unattended "
            "default path deliberately leaves judge_fn unset rather than spend a live model call "
            "with no way to grade it. Call run_generation_tier(case) directly (real default: "
            "default_llm_judge) or run_all(run_live_generation=True, judge_fn=...)."
        )
    pass_criteria, fail_criteria = _generation_judge_criteria(case)
    run = run_generation_case_once(case, generate_fn=generate_fn)
    verdict = judge_fn(run["final_response"], pass_criteria, fail_criteria)
    passed = bool(verdict.get("passed"))
    failures = [] if passed else [
        f"judge scored FAIL ({verdict!r}) against pass_criteria={pass_criteria!r} / "
        f"fail_criteria={fail_criteria!r}"
    ]
    return CaseResult(
        case_id=case["id"], tier="generation", passed=passed, actual=run["final_response"],
        failures=failures, details={**run, "judge_verdict": verdict},
    )


def run_all(case_filter: str | None = None, *, run_live_generation: bool = False) -> list[CaseResult]:
    """`run_live_generation=False` (default, used by main()/pytest): tier != "deterministic"
    cases are reported as a documented not-run result WITHOUT calling run_generation_tier -
    free, fast, CI-safe, no network. `run_live_generation=True`: run_generation_tier(case) runs
    for real (real model call + real default_llm_judge) - only pass this explicitly."""
    results: list[CaseResult] = []
    for case in load_cases():
        if case_filter and case["id"] != case_filter:
            continue
        if case["tier"] == "deterministic":
            results.append(run_deterministic_case(case))
        elif run_live_generation:
            try:
                results.append(run_generation_tier(case, judge_fn=default_llm_judge))
            except (NotImplementedError, GenerationJudgeNotWired) as exc:
                results.append(CaseResult(case_id=case["id"], tier="generation", passed=False,
                                           actual="", failures=[str(exc)]))
        else:
            results.append(CaseResult(
                case_id=case["id"], tier="generation", passed=False, actual="",
                failures=[f"case {case['id']!r} is tier=generation; not run (pass --live-generation "
                          "to run it for real - see README's Tier 2 section)"],
            ))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default=None, help="run only this case id")
    parser.add_argument(
        "--live-generation", action="store_true",
        help="run tier=generation cases for real: a live model call + default_llm_judge (see README's Tier 2 section)",
    )
    args = parser.parse_args()

    results = run_all(case_filter=args.case, run_live_generation=args.live_generation)
    if not results:
        print(f"no cases matched (filter={args.case!r})", file=sys.stderr)
        return 2

    exit_code = 0
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.case_id} (tier={result.tier})")
        if not result.passed:
            exit_code = 1
            for reason in result.failures:
                print(f"    - {reason}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
