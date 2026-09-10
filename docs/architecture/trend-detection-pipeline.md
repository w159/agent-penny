# Trend detection and escalation pipeline

Status as of 2026-08-20. See `docs/ROADMAP.md` for what is still open and
`docs/CHANGELOG.md` for the entries that shipped this.

## Flow

```
scheduler tick (cron/scheduler.py)
  -> maybe_run_trend_pass()
       gated on cron.trend.enabled and _trend_pass_due()
       (config-driven cadence: cron.trend.{days,hour,minute,timezone})
  -> _dispatch_trend_pass()
  -> run_trend_pass()                      (cron/trend_pass.py)
       corpus pull -> embed -> cluster -> name/explain (Stage B)
  -> select_alerts()                       (cron/trend_escalation.py)
       compares current clusters against cron/trend_state.py TrendAlertState
       decides alert kind: new | update | escalation
  -> _deliver_alerts()                     (cron/trend_pass.py)
       -> Teams card (plugins/platforms/teams/ticket_card.py build_trend_card)
       -> escalation email, only when kind == "escalation"
          (cron/trend_email.py build_trend_email -> tools/graph_mail.py send_mail)
```

## Scheduler cadence

`_trend_pass_due()` / `_mark_trend_pass_ran()` in `cron/scheduler.py` read
`cron.trend.{days,hour,minute,timezone}` from config rather than gating on a
fixed daily check. The shipped config runs Mon/Wed/Fri at 07:00
America/New_York, computed via `zoneinfo` so it stays correct across the
DST transition. The pass fires at most once per scheduled day.

Before this shipped, `cron.trend` had no block in
`/home/yoda/.hermes/config.yaml` at all, so `enabled` fell back to its
`False` default and `maybe_run_trend_pass()` returned before ever running.
No state file was written, so the escalation ladder had nothing to read
and an unacknowledged trend could sit indefinitely.

## Escalation ladder and business-hours semantics

`cron/trend_escalation.py` measures elapsed time in business hours only:
Monday through Friday, 08:00 to 17:00, in the same timezone as the
schedule. A trend raised Friday at 16:00 does not accrue escalation time
over the weekend; the clock resumes Monday at 08:00. The rungs are 4, 12,
and 24 business hours since the trend was first raised (or, if it was
previously acknowledged and reopened by growth, since the reopen).

Each alert carries a `kind`:

- `new` - first time this trend has been seen.
- `update` - an acknowledged trend whose blast radius grew (more tickets
  or more distinct devices than its recorded peak). The acknowledgement
  is cleared and the ladder timer restarts from the growth event.
- `escalation` - an unacknowledged trend has crossed a ladder rung.

`TrendAlertState` (`cron/trend_state.py`) tracks `peak_ticket_count`,
`peak_device_count`, `last_growth_at`, and `retired_at` in addition to the
original fields, and stays backward compatible with state files written
before this change. `growth_delta()` measures the change since the peak;
`is_quiet()` reports whether an acknowledged trend has gone
`QUIET_PERIOD_DAYS` (7) with no growth, which retires it. Renewed growth
on a retired trend un-retires it.

## Acknowledge control

The Teams card's ACKNOWLEDGE TREND button is an `Action.Submit`. Before
this shipped, nothing on the receiving side consumed that submit action,
so acknowledging a card had no effect. `plugins/platforms/teams/adapter.py`
now routes `ack_trend` submissions to `record_acknowledgement()`, which
writes `acknowledged_at` and `acknowledged_by` onto the trend's state.
Acknowledgement is not gated by `TEAMS_ALLOWED_USERS` - any member of the
channel the card was posted to can acknowledge it.

## Teams card and the mention constraint

`build_trend_card()` (`plugins/platforms/teams/ticket_card.py`) takes
`kind`, `hours_since_raised`, and `mention_upns`. The card's visual layout
is unchanged; these are new data inputs, not a redesign.

Microsoft documents that channel and team mentions are not supported in
bot-authored messages
(https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-format).
An initial implementation that tried a channel-wide mention was removed
for that reason. The card instead mentions individual users by UPN
(`cron.trend.mention_upns` in config), which is the documented way to
trigger a per-user activity-feed notification. Teams tag mentions (mentioning
a team-defined tag rather than individual users) are the other Microsoft-
supported option and remain a possible future alternative if per-user lists
become unwieldy.

## Escalation email

`cron/trend_email.py` `build_trend_email()` renders a Henssler-branded
(`#154734` dark green, `#C49A22` gold), table-layout HTML email with all
CSS inline for email client compatibility. Every ticket-derived value is
HTML-escaped before insertion; the ticket table is capped at 25 rows.

`tools/graph_mail.py`, backed by `send_mail()` in
`tools/microsoft_graph_client.py`, sends it via Microsoft Graph
`POST /users/{sender}/sendMail` using app-only client-credentials auth.
This requires the `Mail.Send` APPLICATION permission (not delegated) with
admin consent granted to the app registration behind `TEAMS_CLIENT_ID`.

`cron/trend_pass.py` sends the email only when the alert `kind` is
`escalation` - `new` and `update` alerts go to Teams only. An email send
failure is logged and does not abort Teams delivery.

## Configuration

`cron.trend` in `/home/yoda/.hermes/config.yaml`:

| Key | Value shipped | Note |
|---|---|---|
| `enabled` | see config | gates the scheduler check |
| `days` | `[mon, wed, fri]` | |
| `hour` / `minute` | `7` / `0` | local to `timezone` |
| `timezone` | `America/New_York` | DST-correct via `zoneinfo` |
| `mention_upns` | `[jmorgan@henssler.com]` | per-user Teams mention list |
| `email_enabled` | `true` | |
| `email_to` | `[itreg@henssler.com]` | |
| `email_sender` | `""` | blank on purpose - see Open items below |
| `dashboard_url` | `https://jm-dev.tail80802c.ts.net/` | linked from card/email |

## Known gaps (not yet closed)

- `Mail.Send` application permission and admin consent are not granted on
  the `TEAMS_CLIENT_ID` app registration, so Graph mail send will fail
  until that lands.
- `email_sender` is blank; no sending mailbox has been chosen. The
  pipeline skips the email with a log line until both this and the Graph
  permission are in place.
- The UPN mention has not been confirmed against a live Teams tenant to
  verify the activity-feed notification actually fires.
- The gateway process has not been restarted, so the new cadence and
  escalation logic in this document are not yet running live.

Full detail on these is tracked in `docs/ROADMAP.md`.
