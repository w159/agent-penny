# Triage card slimming - evidence

Run date: 2026-08-11
Change: `plugins/platforms/teams/ticket_card.py` - slim the ConnectWise Triage
adaptive card after the user rejected it as a "mile-long message".

## Status: PARTIAL - live delivery NOT observed

Code verified, deployed, and rendering correctly offline. No slim card has been
delivered to Teams yet, so end-to-end behavior is unproven. See "Outstanding".

## Gate output (re-run at close of session)

```
cd /home/yoda/.hermes/hermes-agent && venv/bin/python -m pytest \
  tests/plugins/test_teams_triage_card_real_failures.py \
  tests/plugins/test_teams_triage_card.py \
  tests/gateway/test_webhook_route_card.py \
  tests/gateway/test_teams_card_fence_hardening.py -q

129 passed, 1 warning in 2.37s
```

Full-suite `pytest tests/ -q` did NOT complete: 26,000+ tests collected, outran
both a 300s and a 180s budget. Pre-existing collection errors (`tests/acp/*`,
`tests/acp_adapter/*`, `test_webhook_subject_suppression.py` missing
`_suppression_session_key`) were confirmed present with the diff stashed, so they
are unrelated to this change. The rest of the suite is UNVERIFIED.

## Rendered card, real #95173 content

```
Blocked - New Triage Ticket
#95173 - Updates Message

Issue
For the past three weeks, I have received the messages below when turning on my
laptop in the morning after it completed updates while shutting down the day
before.

This happens on one random day each week, and the process takes about 30 minutes
to cycle through. The first message comes up short...

Contact    Belinda Johnson
Priority   Priority 4 - Low
Owner      **UNASSIGNED**

[ Open in ConnectWise ]   [ I've got it ]
```

Measured: issue block 300 chars (was up to 700), `image markup present: False`,
`fences: 1`, total payload 1889 chars (mostly Adaptive Card JSON keys).

Caveat: the ticket dict was reconstructed from the user's pasted card content.
Stored payloads in `logs/cw_callback_payloads.jsonl` hold only the raw CW POST;
the issue note and contact are added later by API enrichment in
`scripts/cw_callback_handler.py`. The code path is real, the input is rebuilt.

## Independent verification (atlas:verifier, fresh context)

CONFIRMED: `_ISSUE_MAX_CHARS = 300` applied at `ticket_card.py:270`; image regex
`_MD_IMAGE_RE` (`:233`) handles the nested-bracket `![[Image.png]](url)` form,
proven by execution, and runs at `:266` BEFORE the cap; no caller anywhere passes
`note=`; FactSet is exactly Contact/Priority/Owner at `:359-363`;
`build_ticket_card` (cron digest lane) untouched with its Status fact intact;
`render_triage_message` (`:451-477`) has a single return path so prose cannot
escape the fence; `build_triage_card({})` returns placeholders without raising.
Test assertions were updated, not weakened (git diff on
`test_teams_card_fence_hardening.py`).

## Deploy

```
systemctl --user restart hermes-gateway.service
ActiveState=active  SubState=running  MainPID=848730

13:51:09  gateway.run: Starting Hermes Gateway...
13:51:09  [webhook] Listening on 127.0.0.1:8644 - routes: cw-cb-9f4c...
13:51:20  [teams] Webhook server listening on :3978/api/messages
```

Note: the unit actually serving Teams is `hermes-gateway.service` (enabled, owns
the live PID). `agent-penny-gateway.service` is `inactive dead`. A stored ops note
claims the latter is canonical - that note is wrong and needs correcting.

## Misdiagnosis worth recording

Four post-restart callbacks logged `response=8 chars` with no delivery. This was
initially read as a regression from removing the card's note block. It is not.
The 8 chars are the `[SILENT]` marker (`gateway/response_filters.py:1`), and
`gateway/run.py:18120` blanks the turn. The route prompt in `config.yaml`
explicitly says "When you have nothing worth saying, reply with exactly [SILENT]
... Most events should end this way." A callback on the OLD code (13:22:02) was
also silent. Silence is designed behavior, decided from ticket content, upstream
of and independent from the card builder.

Structural note surfaced by this: the card is authored in Python so the model
cannot malform it, but the model can still veto delivery entirely by going
silent. That was invisible while its prose was the message body. Not a bug today
- worth a decision later on whether a route with card metadata should be exempt
from silence suppression (`gateway/run.py:18119` is the intervention point).

## Live delivery chain CONFIRMED (14:17-14:18)

```
14:17:59,419  response ready: platform=webhook ... response=165 chars
14:17:59,451  [Webhook] Sending response (165 chars) to webhook:cw-cb-...
14:18:00,236  POST .../teams/v3/conversations/19:d72b9e0d.../activities -> 201
```

One send, one activity, one 201. The `N chars` in "Sending response" is the
MODEL's reply length, logged BEFORE `_apply_route_card` attaches the card - proven
by the #95173 delivery at 13:24:54 which logged 228 chars yet arrived in Teams as
a full multi-block adaptive card. So a small N here is expected, not a warning.

## Second misdiagnosis, refuted

An explorer reported a FAULT: "CW_MANAGE credentials missing from the restarted
gateway, prompt_len dropped 969 tokens, enrichment dead, briefs degraded."
REFUTED on both legs:

- prompt_len across today ranges 25257-28867 PRE-restart and 26085-27089
  POST-restart. The claimed drop compared two cherry-picked samples (27317 vs
  26348). Post-restart values exceed several pre-restart ones. Normal variation.
- Enrichment tested live against ticket 95186: returns 401 chars of issue note
  plus contact/email/phone. `creds present: True`. The handler subprocess loads
  /home/yoda/.hermes/.env itself; the gateway's own environ lacking CW_MANAGE is
  expected and not evidence of anything.

Zero "no issue note on the ticket" occurrences and zero CW 401/403s in today's logs.

Lesson: an absent environment variable in a parent process is NOT evidence that a
child process cannot see it. Test the behavior, do not infer from the environment.

## Silences were correct behavior

Six consecutive [SILENT] replies post-restart were four automated Auvik alerts, an
outage notification, and an update to an existing assigned ticket - exactly the
classes the route prompt says to stay quiet about. A seventh callback (14:17:59)
spoke and delivered a card, confirming the lane is not stuck.

## Outstanding

1. Observe one real slim card delivered to Teams. Verify with:
   `grep -E "Sending response|conversations/.*activities" /home/yoda/.hermes/logs/agent.log`
   Expect exactly one `[Webhook] Sending response` and one `.../activities` -> 201
   per callback, card showing 3 fact rows and no image markup.
2. Full test suite has never completed. Needs `timeout 1200` or a per-directory split.
3. Unrelated, pre-existing: `msgraph_webhook` refuses to start (41 occurrences today,
   first 10:54) - binding 0.0.0.0 without `extra.allowed_source_cidrs`.
