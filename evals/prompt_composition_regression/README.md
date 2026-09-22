# prompt_composition_regression

A native eval/replay harness for Penny's own prompt-composition pipeline
(SOUL.md text, `agent/turn_finalizer.py`'s pre-send transforms, `plugins/*`
hooks). Declarative case + judge pattern, adapted to this codebase's own
conventions - not a port of the Go `skill-up` binary. The idea (from Dream-RSI,
arXiv 2609.14858): a completed history of exploration decisions plus their
real outcomes can score a candidate change without re-running the expensive
real process. Here that means: the three real 2026-09-21 production incidents
(a roast refusal, a self-narrating footer, a repeated roast subject) become
permanent, replayable cases instead of one-off manual verification, so a
future change to any of these code paths gets checked against real history
before it ships, not after Jerry reports it live.

## Case format: JSON, not YAML

This repo's other `evals/*` suites already settled this: `evals/browser_use/tasks/*.json`,
and every other structured eval-task definition under `evals/` is JSON (`git
grep`/`find evals -iname '*.json'` turns up 6 files; none in YAML). The
deployment's own scheduled-task definitions (`~/.hermes/cron/jobs.json`) are
JSON too. YAML shows up in this repo only for locale strings and Docker/CI
config, never for eval or task definitions. Matching existing precedent over
introducing a second format for the same kind of artifact.

## Case schema

One file per case under `cases/`:

```jsonc
{
  "id": "kebab-case-id",
  "description": "what incident this is, and what's asserted",
  "platform": "teams",
  "session_id": "agent:main:teams:group:19:d72b9e0d737b4dda960814e674c260b7@thread.v2",
  "conversation_history": [{"role": "user", "content": "..."}, ...],  // can be []
  "user_message": "the inbound message that would trigger this turn",
  "tier": "deterministic",                 // or "generation"
  "checker": "strip_behavior_state_aside", // tier=deterministic only; id into checkers.CHECKERS
  "checker_input": { ... },                // extra fixture data the checker needs
  "expected": {                            // generic string assertions, graded by runner.grade()
    "must_contain": ["..."],
    "must_not_contain": ["..."],
    "ends_with": "...",                    // optional
    "equals": "..."                        // optional
  },
  "judge": {
    "pass_criteria": "plain-language description of a PASS",
    "fail_criteria": "plain-language description of a FAIL"
  },
  "source": "tests/... or a real file path the fixture was copied from"
}
```

`judge.pass_criteria`/`fail_criteria` are always written in plain language on
every case, deterministic or not - for a `tier=deterministic` case they are
documentation of intent (a human reading the case should be able to predict
what `expected` checks for); for a `tier=generation` case they are the literal
question fed to a real judge (`judge()`/`judge_batch()`, same TypeSafe
capability this session already uses) against the model's actual draft reply.

## Adding a new case

1. Reproduce the real incident (transcript, draft text, fixture) - do not
   invent one. Prefer copying an existing regression test's fixture verbatim
   (see `source` field convention above) over writing new prose.
2. Decide the tier:
   - **deterministic**: the fix lives in a pure function you can call directly
     with no model in the loop (a stripper, a plugin hook, a structural
     content check). Register/reuse a `checkers.py` function, drop a JSON file
     in `cases/`, done - `runner.py`/`test_cases.py` pick it up automatically
     by globbing `cases/*.json`.
   - **generation**: the fix is only observable in what a real model
     generates from the prompt (a tone/judgment call, not a code path). Fill
     in `checker: null` and the top-level `judge` block (or a `generation_case`
     block for a case that also runs at tier 1, like
     `roast-refusal-disambiguation-2026-09-21`). `run_generation_tier()` runs
     it for real (see Tier 2 below) when explicitly invoked - the default
     `runner.py`/`test_cases.py`/pytest paths never call it unattended, so
     adding a case here never adds a hidden network call to CI.
3. Run it (`pytest` or the CLI below) and confirm it passes against current
   code.

## Running

```bash
# CLI - text PASS/FAIL per case, exit code 1 on any failure
.venv/bin/python evals/prompt_composition_regression/runner.py
.venv/bin/python evals/prompt_composition_regression/runner.py --case footer-self-narration-2026-09-21

# pytest - not under tests/ on purpose, so pyproject.toml's testpaths=["tests"]
# never auto-collects it into the main suite's default run. test_generation_tier.py's
# fast/fake tests always run here; its one live test is excluded by the default
# `-m 'not integration'` addopts AND needs HERMES_LIVE_TESTS=1.
.venv/bin/python -m pytest evals/prompt_composition_regression/ -v

# Regression-net proof (reverts each fix in isolation, read-only, no working
# tree mutation - see script docstring) and confirms pre-fix FAILS, current PASSES
.venv/bin/python evals/prompt_composition_regression/scripts/prove_regression_net.py

# Tier 2, explicitly: real model call + real judge, N runs, optionally written back into
# the case JSON's generation_case.results (see Tier 2 below)
.venv/bin/python evals/prompt_composition_regression/scripts/run_live_generation_case.py --runs 2
```

## Tier 1 (deterministic) - what this pass actually built and ran

All 3 real incidents run at this tier, calling the exact real functions the
fixes shipped in - no mocking of the code under test:

| case | real function called |
|---|---|
| `footer-self-narration-2026-09-21` | `agent.turn_finalizer._strip_behavior_state_aside` |
| `repeated-roast-subject-2026-09-21` | `plugins.roast_variety_context._on_pre_llm_call` |
| `roast-refusal-disambiguation-2026-09-21` | `agent.prompt_builder.load_soul_md` (reads the real deployed `/home/yoda/.hermes/SOUL.md`) |

Free, fast (`0.25s` for all 3 + a suite-integrity test), CI-safe: no network,
no model call, no mutation of repo or `~/.hermes` state.

The third case is honestly weaker evidence than the first two: its fix is
prompt *text*, not code, so calling `load_soul_md` and asserting the
disambiguating sentences are present only pins the wording - it cannot prove
a model actually reads and obeys it. That behavioral question is exactly what
its `generation_case` block is for (see Tier 2).

## Tier 2 (generation + judge) - real, executed for `roast-refusal-disambiguation-2026-09-21`

`runner.run_generation_tier(case)` is a real implementation, not a stub:
1. Builds the real system prompt via `agent.system_prompt.build_system_prompt`
   (`runner._build_generation_system_prompt`) - the real deployed SOUL.md,
   approved behavior rules, and `memories/ops/roster.md` ("What You Already
   Know"), fed a duck-typed agent stub with no tools attached (so the wire
   request carries no tool schema - the draft can never trigger a real tool
   call). `skip_context_files=True`: the real gateway's own cwd for a Teams
   turn (`~/.hermes`, its systemd `WorkingDirectory`) has no AGENTS.md of its
   own, so this is a byte-equivalent simplification, not a shortcut.
2. Runs one real turn via `generate_fn` (default `runner.default_generate`):
   resolves the actual configured main model/provider/base_url exactly like
   the CLI does (`hermes_cli.runtime_provider.resolve_runtime_provider`,
   `config.yaml`'s `model:` block - `nemotron-3-super:cloud` over the local
   Ollama endpoint in this deployment), a bare `chat.completions.create()`
   with no `tools` param, no `session_db`, no delivery code path touched.
3. Applies the real `agent.turn_finalizer._strip_behavior_state_aside`
   post-processing exactly like `finalize_turn` does
   (`runner.run_generation_case_once`). `_append_file_mutation_footer` is
   `finalize_turn`'s only other per-turn text transform and is a no-op here
   (it only fires on a failed `write_file`/`patch` tool call, and no tools are
   attached); no plugin in this deployment registers `transform_llm_output`.
4. Scores the post-processed text with `judge_fn` (default
   `runner.default_llm_judge`, a real `agent.auxiliary_client.call_llm`-based
   verdict - the same `call_llm`-judge convention
   `evals/compaction/scripts/codex_arm.py` already uses in this repo; this
   codebase ships no shared `judge()`/`judge_batch()` primitive, so a caller
   with access to one - the eval-authoring session's own TypeSafe judge tool -
   may inject it instead) against `case['generation_case']['judge']` (or the
   top-level `judge` for a pure `tier=generation` case), verbatim.

`run_all()`'s unattended default path (`main()`/pytest) never calls this for
real: a `tier=generation` case is reported as a documented not-run result
unless the caller passes `run_live_generation=True` (`run_all()`) or
`--live-generation` (CLI), or calls `run_generation_tier()` /
`scripts/run_live_generation_case.py` directly - so adding this
implementation added no hidden live-model call to CI.
`evals/prompt_composition_regression/test_generation_tier.py` pins the
composition/scoring wiring with an injected fake `generate_fn`/`judge_fn` (no
network); its one real-call test is gated the same double way
`tests/run_agent/test_fireworks_live.py` gates its own live call -
`@pytest.mark.integration` (excluded by this repo's default
`-m 'not integration'`) AND `HERMES_LIVE_TESTS=1`.

**Executed 2026-09-22**, 3 real runs of
`roast-refusal-disambiguation-2026-09-21` against the live deployment (all
three 2026-09-21 fixes running), scored by two independent judges (the
eval-authoring session's own judge tool and `runner.default_llm_judge`) that
agreed on every run: 2/3 PASS, 0/3 reproduced the original conflation bug (no
run cited CUSTOMER/END-USER COMMUNICATION, "overrides everything", or any
end-user-notification rule). The one FAIL hedged instead on chat-identity
uncertainty - nothing in the composed prompt tells the model THIS session is
the specific chat SOUL.md names by literal Teams thread id, a narrower, real
gap distinct from the bug this case's tier-1 fix targeted. Full per-run
drafts, post-processed text, and both judges' verdicts:
`cases/roast_refusal_disambiguation_2026_09_21.json`'s
`generation_case.results` / `results_summary`. No `state.db` session row was
created by any run (no `session_db` was ever constructed - `default_generate`
never touches `AIAgent`) and nothing was delivered to Teams (no delivery code
path was ever invoked).

## Regression-net evidence

`scripts/prove_regression_net.py` reconstructs the pre-fix behavior for each
case - read-only, no working-tree mutation - and confirms the SAME grading
logic (`runner.grade`) fails against it and passes against current code. Full
before/after output: `docs/architecture/prompt-composition-eval-harness.md`.
