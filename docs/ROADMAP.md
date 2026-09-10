# Roadmap

Status of work in flight. Shipped items move to `docs/CHANGELOG.md` with the evidence that proved them.

## In flight

### Trending issues detection (Agent Penny / ConnectWise)

Goal: surface emerging issue trends across a rolling window of new, open, and closed tickets,
even when techs describe the same problem in different words. Help desk staff work remotely and
independently, so the same incident gets logged under many different descriptions and the pattern
goes unnoticed. Matching wording is unreliable; matching meaning is the point.

#### Architecture (decided 2026-08-19)

Clustering is embeddings plus cosine similarity, not LLM prompt merging. Deterministic, cheap to
re-run, and it does not depend on a model agreeing with itself twice.

- Embedding model: `bge-m3` via local Ollama, 1024 dims, `POST /api/embed`.
- Primary clustering signal: issue notes, resolution notes, and time entry notes. The summary line
  alone is too thin (see the calibration finding below).
- Attached ConnectWise configurations are a corroborating BOOST, never a requirement. A shared CI,
  company, or site lets a cluster form at a lower cosine. Their absence never blocks a cluster,
  because real trends occur across many different devices with no configuration item in common.
- Stage B (naming and explaining a cluster) uses an LLM: `claude-sonnet-5` when `ANTHROPIC_API_KEY`
  is present, otherwise `glm-5.2:cloud`. Resolution is at runtime, so no code change is needed to
  switch once a key exists.
- Delivery: a Teams Adaptive Card plus an auto-created ConnectWise parent ticket, guarded against
  duplicates by a fingerprint ledger.
- Cadence: daily pass, rolling 21-day window. Calibration corpus: 90 days.

#### Built and verified

- `cron/trend_vectors.py` - embedding client, vector store, cosine, deterministic clustering
- `cron/trend_cluster_embed.py` - Stage A, embeddings replacing the LLM merge prompt
- `cron/trend_ticket.py` - `ensure_trend_ticket()`, fingerprint ledger, dry-run path
- `cron/trend_pass.py` - `run_trend_pass()`, the end-to-end pipeline entry point
- `cron/trend_cluster_semantic.py` - Stage B model resolution, Anthropic-ready
- `scripts/calibrate_trend_threshold.py` - offline threshold calibration

Evidence: `.venv/bin/python -m pytest tests/cron/ -q` -> `1 failed, 650 passed` (2026-08-19).
The single failure is `tests/cron/test_ops_memory.py::TestPromptMemoryInjection`, pre-existing and
unrelated: it monkeypatches `cron.scheduler._warn_if_escalations_flag_missing`, which does not
exist, and `cron/scheduler.py` is untouched by this work.

#### Defects found and fixed during the run

- Fingerprint hashed the LLM-authored summary, so a reworded title opened a duplicate ticket.
  Regression test written, proven failing, then fixed.
- Fingerprint then hashed the earliest ticket id, which fails when a trend outlives the 21-day
  window and its founding ticket ages out. Window-slide test written, proven failing, then fixed.
  Fingerprint is now the top-5 normalized entity/symptom signature from raw ticket text.
- `detect_trends()` had a hardcoded 14-day internal window while the corpus pull is 21 days.
  Now takes `window_days`.

#### Delivery pipeline shipped (2026-08-20)

The acknowledge control and the scheduling gap are resolved; see `docs/CHANGELOG.md` for evidence
and `docs/architecture/trend-detection-pipeline.md` for the wired flow.

- `cron.trend` is now configured in `/home/yoda/.hermes/config.yaml`: Mon/Wed/Fri 07:00
  America/New_York cadence, `mention_upns`, `email_enabled`, `email_to`, `dashboard_url`.
- Escalation ladder rungs are 4h/12h/24h of business hours, with `new`/`update`/`escalation` alert
  kinds, growth-reopen, and 7-day quiet retirement (`cron/trend_state.py`, `cron/trend_escalation.py`).
- ACKNOWLEDGE TREND on the Teams card is wired to `record_acknowledgement()`
  (`plugins/platforms/teams/adapter.py`).
- Escalation email path built: `cron/trend_email.py` + `tools/graph_mail.py` via Microsoft Graph
  `sendMail`, gated to `kind == "escalation"` only.

#### Open

Still blocking clean automatic alerting, carried over from 2026-08-19 and not touched by this run:

1. Exclude `is_automated` tickets from trend candidacy. 86% of a 21-day window is machine-generated
   (Defender alerts, backup errors, NOC checks) and currently floods detection.
2. Replace single-link clustering with complete-link at a threshold near 0.68. At 0.58 with
   single-linkage the corpus chains into one component.

Blocking full go-live of the delivery pipeline shipped 2026-08-20, in priority order:

3. Grant the `Mail.Send` APPLICATION permission with admin consent on the app registration behind
   `TEAMS_CLIENT_ID`. Without it, `tools/graph_mail.py` cannot send.
4. Choose a sending mailbox and set `email_sender` in `cron.trend` config. It is blank on purpose;
   the pipeline skips the email with a log line until this is set.
5. Confirm the per-user UPN mention fires an activity-feed notification against a live Teams
   tenant. Not yet visually verified.
6. Restart the gateway service so the new `cron.trend` cadence and escalation logic take effect in
   the running process. Not yet restarted as of this entry.

Lower priority:

- Rebuild enrichment cache entries written before the configurations fix; they hold the raw link
  record without `type`/`company`/`site` and self-heal only on a cache miss.
- Verify ConnectWise fetch concurrency above workers=2 at full 21-day scale; only 276 tickets were
  exercised clean.
- Calibrate the similarity threshold against a 90-day export via
  `scripts/calibrate_trend_threshold.py`. The 0.58 default is provisional and measured too low.

Config default remains `cron.trend.enabled` as set in `/home/yoda/.hermes/config.yaml` (now
populated - see `docs/CHANGELOG.md` 2026-08-20). The pass is registered in `cron/scheduler.py` via
`maybe_run_trend_pass()`.

#### Operational hazard

`hermes update` checks out `main`, leaves the repo there, and autostashes in-flight tracked changes.
It did this mid-session on 2026-08-19 at 17:51, which made `plugins/platforms/teams/ticket_card.py`
appear to vanish (it does not exist on `main`). Recovery: `git checkout <branch>` then
`git stash apply` the `hermes-update-autostash-*` entry. Use apply, not pop.
