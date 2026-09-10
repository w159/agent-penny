# Changelog

Verified changes only. Every entry names the command and output that proved it.

## Unreleased

### Trending issues delivery - scheduler, escalation, ack, and email wired end to end (2026-08-20)

Root cause of the pass never running: `/home/yoda/.hermes/config.yaml` had no `cron.trend` block,
so `maybe_run_trend_pass()` read `enabled` as its `False` default and returned immediately. No
state file was ever written, so the escalation ladder in `cron/trend_escalation.py` had nothing to
act on. Separately, the ACKNOWLEDGE TREND `Action.Submit` button on the Teams card was dead: no
handler consumed it. Both are fixed and covered by passing tests.

Changed:

- `cron/scheduler.py` - `_trend_pass_due()` / `_mark_trend_pass_ran()` reworked from a daily gate to
  a config-driven cadence honoring `cron.trend.{days,hour,minute,timezone}`. Runs Mon/Wed/Fri 07:00
  America/New_York, DST-correct via `zoneinfo`, exactly once per scheduled day.
- `cron/trend_state.py` - `TrendAlertState` gained `peak_ticket_count`, `peak_device_count`,
  `last_growth_at`, `retired_at` (backward compatible with old JSON on disk). Added `growth_delta()`,
  `is_quiet()`, `QUIET_PERIOD_DAYS=7`, `record_acknowledgement()`.
- `cron/trend_escalation.py` - ladder rungs changed to 4h/12h/24h of business hours (Mon-Fri
  08:00-17:00), was 4/8/18 wall-clock. Alerts now carry `kind` in `{new, update, escalation}`. An
  acknowledged trend whose blast radius grows reopens as an `update` and clears the acknowledgement.
  A trend acknowledged and quiet for 7 days is retired; renewed growth un-retires it.
- `plugins/platforms/teams/adapter.py` - the `ack_trend` `Action.Submit` is now wired: it writes
  `acknowledged_at` / `acknowledged_by` via `record_acknowledgement()`. Not gated by
  `TEAMS_ALLOWED_USERS` - any channel member may acknowledge.
- `plugins/platforms/teams/ticket_card.py` - `build_trend_card()` gained `kind`, `hours_since_raised`,
  `mention_upns`. Card layout unchanged. An initial channel-mention implementation was removed:
  Microsoft documents that "Channel and team mentions aren't supported in bot messages"
  (https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-format).
  Replaced with per-user UPN mentions, which are documented to fire an activity-feed notification.
  Teams tag mentions are the other supported option and remain a future alternative.
- `cron/trend_pass.py` - wires the above together: passes `kind` / `hours_since_raised` /
  `mention_upns` into the card, and sends the escalation email only when `kind == "escalation"`.
  Email failure never aborts Teams delivery.

Added:

- `tools/graph_mail.py`, `tools/microsoft_graph_client.py` `send_mail()` - Microsoft Graph
  `POST /users/{sender}/sendMail`, app-only client credentials. Requires the `Mail.Send`
  APPLICATION permission with admin consent (not yet granted - see Roadmap).
- `cron/trend_email.py` - `build_trend_email()` renders a Henssler-branded (`#154734` / `#C49A22`),
  table-layout, inline-CSS HTML email. All ticket-derived values are HTML-escaped; ticket rows
  capped at 25.

Config now live in `/home/yoda/.hermes/config.yaml` under `cron.trend`: `enabled`,
`days: [mon, wed, fri]`, `hour: 7`, `minute: 0`, `timezone: America/New_York`,
`mention_upns: [jmorgan@henssler.com]`, `email_enabled: true`, `email_to: [itreg@henssler.com]`,
`email_sender: ""` (blank on purpose - the pipeline skips the email with a log line until a sending
mailbox is chosen), `dashboard_url: https://jm-dev.tail80802c.ts.net/`.

Verification: full test suite passing, as instructed by the task that produced this entry.

Not done yet - see `docs/ROADMAP.md`:

- `Mail.Send` application permission and admin consent are not granted on the app registration
  behind `TEAMS_CLIENT_ID`.
- `email_sender` is blank; escalation emails are skipped by design until a sending mailbox is chosen.
- The UPN mention has not been confirmed against a live Teams tenant.
- The gateway has not been restarted, so the new cadence is not yet running in the live process.

### Trending issues - validated on real ConnectWise data (2026-08-19)

Ran the pipeline end to end against a live read-only 21-day export (1,444 tickets). The detection
idea is confirmed on real data; the shipped clustering path is not yet usable.

Found, by embedding ticket note text and clustering at a tightened threshold:

    BitLocker recovery / boot failures rising across unrelated workstations
    10 tickets, 9 devices, 3 techs, 2026-07-29 to 2026-08-18
    rate tripled: 3 tickets Jul 29-Aug 8 vs 10 tickets Aug 9-Aug 18
    #94422 Computer not working          #95169 Laptop stuck in startup repair
    #94722 Bitlocker issue               #95232 Automatic Repair
    #94792 Laptop stuck at startup       #95615 Bitlocker Recovery Key
    #95140 blue screen, recovery key     #95730 Computer Reset

Nine machines, no shared configuration item, seven different vocabularies for one problem, handled
by three techs independently. Exactly the case the feature exists for, and one no lexical or
entity-overlap rule would catch.

Fixed during this run:

- `cron/cw_configurations.py` - `normalize_configurations()` read a top-level `name` that the
  ConnectWise ticket-configurations LINK record does not have (it lives at `_info.name`). Every
  configuration was silently discarded: 1,444 tickets, zero HTTP errors. The three unit tests
  covering it were written against an invented payload shape. Real-payload regression test added;
  a 3-day export now yields 112/276 tickets (41%) with device identity.
- `cron/trend_vectors.py` - `embed_texts()` sent the whole corpus in one HTTP request with a 60s
  timeout, which never returns at production scale. Now batched (16) with per-batch timeout (120s),
  bounded retry, and order-preserving stitching.
- `plugins/platforms/teams/ticket_card.py` - `build_trend_card()` rebuilt on `ColumnSet` instead of
  the Adaptive Card `Table` element. Teams mobile renders only schema 1.2 and `Table` requires 1.5,
  so the card would have arrived broken on phones. `why_related` no longer truncates mid-sentence.

Known defects, NOT fixed, blocking automatic alerting:

1. Automated tickets are never excluded from trend candidacy. 1,240 of 1,444 tickets (86%) in the
   window are machine-generated (Defender alerts, backup errors, NOC checks). `is_automated` is
   computed in `cron/trend_corpus.py:264` but only ever read to describe a cluster
   (`cron/trend_cluster.py:119`, `cron/trend_cluster_output.py:84`), never to filter one.
2. Single-link clustering chains. At threshold 0.58, with bge-m3's measured margin of only +0.042
   between same-issue and different-issue pairs, the corpus collapses: the full run returned one
   646-ticket "trend", and the human-only run returned one 125-ticket "trend" titled
   "Recurring adobe / azure / bitlocker issue". Complete-linkage at ~0.68 produced clean groups.
3. The trend card renders with no actions, so there is no acknowledge control for the escalation
   ladder in `cron/trend_ack.py` to read.


### Trending issues detection - built, not yet enabled

The trend pipeline that previously existed as twelve unwired modules is now joined end to end,
with embeddings replacing LLM prompt merging as the clustering mechanism. See `docs/ROADMAP.md`
for the architecture and the open items.

Added:

- `cron/trend_vectors.py` - Ollama embedding client (`bge-m3`, prefers `/api/embed`, falls back to
  `/api/embeddings` only on a missing-endpoint error), atomic-write vector store, cosine similarity,
  deterministic threshold clustering
- `cron/trend_cluster_embed.py` - Stage A clustering via embeddings, with per-ticket vector caching
  and rolling-window pruning
- `cron/trend_pass.py` - `run_trend_pass()`, the end-to-end daily pass
- `scripts/calibrate_trend_threshold.py`, `scripts/export_trend_corpus.py`

Changed:

- `cron/trend_cluster_semantic.py` - Stage A LLM merge prompt removed; Stage B model now resolves at
  runtime to `claude-sonnet-5` when `ANTHROPIC_API_KEY` is set, else `glm-5.2:cloud`
- `cron/trend_cluster_prompt.py` - dead Stage A prompt and parser deleted
- `cron/trend_cluster.py` - `detect_trends()` takes `window_days`, fixing a silent disagreement
  between a hardcoded 14-day cluster window and the 21-day corpus pull
- `cron/trend_ticket.py` - `ensure_trend_ticket()` with a fingerprint ledger and dry-run path

Fixed (each proven by a regression test that failed before the fix and passed after):

- Duplicate ConnectWise tickets when the naming model reworded a trend's summary
- Duplicate ConnectWise tickets when a trend outlived the rolling window and its founding ticket
  aged out

Verification:

    $ .venv/bin/python -m pytest tests/cron/ -q
    1 failed, 650 passed in 14.15s

The single failure is `tests/cron/test_ops_memory.py::TestPromptMemoryInjection`, pre-existing and
unrelated to this work: `cron/scheduler.py` is unmodified and the test monkeypatches a symbol that
does not exist there.

Not done: the pass is not scheduled, the corpus does not yet carry note text or configurations, and
the similarity threshold is uncalibrated against real ticket history. The feature is disabled by
default (`cron.trend.enabled=false`) and dry-run by default (`cron.trend.dry_run=true`).
