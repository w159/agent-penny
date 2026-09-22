"""Explicit live-model runner for one evals/prompt_composition_regression tier-2 case.

Never invoked by runner.py's default CLI, run_all(), or pytest (see runner.py's and
README.md's Tier 2 sections) — this is the one place a live model call is unconditional.
Runs the case N times (temperature/model variance means one pass is weak evidence), scores
each with runner.default_llm_judge (the repo's own call_llm-based judge — no external
dependency), and prints the real draft/post-processed/verdict for every run.

Usage:
    .venv/bin/python evals/prompt_composition_regression/scripts/run_live_generation_case.py
    .venv/bin/python evals/prompt_composition_regression/scripts/run_live_generation_case.py \\
        --case roast-refusal-disambiguation-2026-09-21 --runs 3 --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SUITE_DIR = Path(__file__).parent.parent
for _path in (str(_REPO_ROOT), str(_SUITE_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from runner import CASES_DIR, default_llm_judge, load_cases, run_generation_tier  # noqa: E402


def _case(case_id: str) -> dict:
    for case in load_cases():
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


def _run_once(case: dict) -> dict:
    result = run_generation_tier(case, judge_fn=default_llm_judge)
    return {
        "passed": result.passed,
        "draft_response": result.details["draft_response"],
        "final_response": result.details["final_response"],
        "finish_reason": result.details["finish_reason"],
        "stripped": result.details["stripped"],
        "judge_verdict": result.details["judge_verdict"],
    }


def _write_results(case_id: str, runs: list[dict]) -> Path:
    """Merge `runs` into cases/<case_id>.json's `generation_case.results`, preserving any
    prior entries (never overwrites a previous run's evidence)."""
    path = CASES_DIR / f"{case_id.replace('-', '_')}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    existing = data.setdefault("generation_case", {}).setdefault("results", [])
    existing.extend(runs)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="roast-refusal-disambiguation-2026-09-21")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--write", action="store_true", help="append results into the case JSON")
    args = parser.parse_args()

    case = _case(args.case)
    runs = []
    for i in range(1, args.runs + 1):
        print(f"=== run {i}/{args.runs}: {case['id']} ===", flush=True)
        outcome = _run_once(case)
        runs.append(outcome)
        print(f"finish_reason={outcome['finish_reason']!r} stripped={outcome['stripped']}")
        print("--- draft ---")
        print(outcome["draft_response"])
        print("--- final (post-processed) ---")
        print(outcome["final_response"])
        print(f"--- judge: passed={outcome['passed']} verdict={outcome['judge_verdict']} ---\n")

    n_passed = sum(1 for r in runs if r["passed"])
    print(f"SUMMARY: {n_passed}/{len(runs)} runs passed default_llm_judge for case {case['id']!r}")

    if args.write:
        path = _write_results(args.case, runs)
        print(f"wrote {len(runs)} run(s) into {path}")

    return 0 if n_passed == len(runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
