# Prompt-composition eval/replay harness

Status as of 2026-09-21. Evidence-gathering pass only - no SOUL.md or plugin
behavior changed by this work. See `evals/prompt_composition_regression/README.md`
for the case format, how to add a case, and the tier-1/tier-2 split.

## Why

Three real production incidents this session were each fixed only after Jerry
reported them live: a roast refusal from a conflated SOUL.md rule, a
self-narrating "(Humor parameters...)" footer with a trailing emoji, and a
repeated roast subject. Each got a proper regression test
(`tests/agent/test_behavior_state_aside_strip.py`,
`tests/plugins/test_roast_variety_context_plugin.py`) or a text fix
(`/home/yoda/.hermes/SOUL.md`), but those tests exercise the fix in isolation -
none of them are framed as a standing, replayable eval battery over Penny's
prompt-composition pipeline that a *future* change gets checked against
before shipping. `evals/prompt_composition_regression/` is that battery: a
declarative case (JSON) + judge-criteria pattern, scoped to this codebase's
existing `evals/*` conventions (JSON case files, one directory per suite,
`README.md` + runner + `results/` where relevant), not a port of any external
tool.

## Flow

```
evals/prompt_composition_regression/cases/*.json   (declarative case data)
  -> runner.load_cases()
  -> tier == "deterministic":
       runner.run_deterministic_case()
         -> checkers.run_checker()            (real function call, e.g.
                                                agent.turn_finalizer._strip_behavior_state_aside,
                                                plugins.roast_variety_context._on_pre_llm_call,
                                                agent.prompt_builder.load_soul_md)
         -> runner.grade()                    (generic must_contain / must_not_contain /
                                                ends_with / equals assertions)
  -> tier == "generation":
       runner.run_generation_tier()           (real implementation: system prompt via
                                                agent.system_prompt.build_system_prompt, live
                                                model call, agent.turn_finalizer.
                                                _strip_behavior_state_aside post-processing,
                                                judge_fn scoring - see README's Tier 2 section)
  -> test_cases.py (pytest-parametrized) and runner.py (CLI) both consume the
     same load_cases()/run_deterministic_case() so the two entry points can
     never silently drift.
```

`evals/` is not under `pyproject.toml`'s `testpaths = ["tests"]`, so this
suite never runs inside the main `pytest` invocation - it is invoked
explicitly.

## Regression-net evidence

The concern with any new eval battery is that its cases trivially pass no
matter what the code does. `evals/prompt_composition_regression/scripts/prove_regression_net.py`
answers that directly: for each of the 3 incident cases, it reconstructs the
pre-fix behavior - read-only, never touching the working tree - and confirms
the exact same grading logic (`runner.grade`) fails against it, then confirms
it passes against the current, fixed code.

- `footer-self-narration-2026-09-21`: the fix is an uncommitted change to
  `agent/turn_finalizer.py`; the pre-fix function body comes from
  `git show HEAD:agent/turn_finalizer.py` into an isolated temp module,
  never written back to the repo.
- `repeated-roast-subject-2026-09-21`: `plugins/roast_variety_context/__init__.py`
  has no HEAD history at all (`git show HEAD:...` exits 128, confirmed in the
  script's own output below) - it is a brand-new file, so pre-fix behavior is
  modeled as "the hook does not exist", i.e. always returns no context.
- `roast-refusal-disambiguation-2026-09-21`: `/home/yoda/.hermes/SOUL.md` is not
  under version control (`~/.hermes` is not a git repository), so there is no
  commit to diff. The pre-fix snapshot is reconstructed by splicing the exact
  two disambiguating sentences back out of the real current file's text, in
  memory only.

Actual output, `.venv/bin/python evals/prompt_composition_regression/scripts/prove_regression_net.py`:

```
=== footer-self-narration-2026-09-21 ===
pre-fix (git show HEAD:agent/turn_finalizer.py) actual output:
  "Can't roast end users\u2014it's a hard rule. Comms must stay internal-only (regulatory firm, plus SOUL.md \u00a7CUSTOMER/END-USER COMMUNICATION overrides everything). No names, no implications, nada.\nBut since you asked nicely: Jarvis Williams' \u201cWaiting Client Response\u201d pile is aging like forgotten yogurt in the break fridge.\nWant me to dig into Erica Martin's tickets, or shall we roast the actual stall-patterns keeping tickets stuck? (Humor parameters: MAX. Useful: still baked in.) \U0001F604"
pre-fix grade: FAIL -> ["contains forbidden substring: 'Humor parameters'", "contains forbidden substring: '\U0001F604'", "expected to end with 'keeping tickets stuck?', got \"...still baked in.) \U0001F604\""]
current (working tree, fixed) grade: PASS -> []

=== repeated-roast-subject-2026-09-21 ===
git show HEAD:plugins/roast_variety_context/__init__.py -> exit 128 (confirms file has no pre-fix version at all)
pre-fix (no hook) actual output: ''
pre-fix grade: FAIL -> ["missing required substring: 'Jarvis Williams'", "missing required substring: 'your last message'"]
current (working tree, fixed) grade: PASS -> []

=== roast-refusal-disambiguation-2026-09-21 ===
pre-fix (real SOUL.md text with the 2 disambiguating sentences spliced out) grade: FAIL -> ['missing required substring: "does not override VOICE & PERSONALITY\'s internal-only end-user roast budget"', "missing required substring: 'It is NOT about whether you can discuss, analyze, or joke about an end user internally'", "missing required substring: 'this rule does not touch it'"]
current (real deployed SOUL.md, fixed) grade: PASS -> []

=== SUMMARY ===
OK: footer-self-narration-2026-09-21 (pre-fix correctly failed: True, current correctly passed: True)
OK: repeated-roast-subject-2026-09-21 (pre-fix correctly failed: True, current correctly passed: True)
OK: roast-refusal-disambiguation-2026-09-21 (pre-fix correctly failed: True, current correctly passed: True)
```

All 3 cases: pre-fix state fails the exact same assertions the current code
passes. This is the measurable evidence Theory D predicted - a candidate
change to any of these three code paths (the strip regex, the plugin hook,
the SOUL.md wording) is now checked against real incident history before it
ships, not caught live in production again.

`.venv/bin/python -m pytest evals/prompt_composition_regression/ -v` (current code):

```
evals/prompt_composition_regression/test_cases.py::test_deterministic_case_passes[footer-self-narration-2026-09-21] PASSED
evals/prompt_composition_regression/test_cases.py::test_deterministic_case_passes[repeated-roast-subject-2026-09-21] PASSED
evals/prompt_composition_regression/test_cases.py::test_deterministic_case_passes[roast-refusal-disambiguation-2026-09-21] PASSED
evals/prompt_composition_regression/test_cases.py::test_all_three_2026_09_21_incidents_are_covered PASSED
4 passed in 0.25s
```

## Tier 2 - implemented and executed 2026-09-22

Real model-call + judge scoring (`runner.run_generation_tier`) is implemented,
not a stub. `roast-refusal-disambiguation-2026-09-21`'s tier-1 check can only
pin that the corrective SOUL.md wording is present - it cannot prove a model
actually complies with it. Tier 2 answers that: 3 real runs against the live
deployment (all three 2026-09-21 fixes running), scored by two independent
judges (the eval-authoring session's own judge tool and this repo's own
`runner.default_llm_judge`) that agreed on every run - 2/3 PASS, 0/3
reproduced the original conflation bug. Full per-run drafts, post-processed
text, and both judges' verdicts:
`evals/prompt_composition_regression/cases/roast_refusal_disambiguation_2026_09_21.json`'s
`generation_case.results` / `results_summary`. Reasoning, the exact composition
pipeline, and the CI-safety contract (never an unattended live call - see
`GenerationJudgeNotWired`, `run_live_generation`, `--live-generation`):
`evals/prompt_composition_regression/README.md`'s "Tier 2" section. Fast,
fake-injected wiring regression coverage (no network per run):
`evals/prompt_composition_regression/test_generation_tier.py`.
