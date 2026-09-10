#!/usr/bin/env python3
"""
The accuracy gate: what route_ticket() would do to the LIVE Triage board,
decided but never written.

This is the missing read-side caller for cron/outage_store.active_outages().
Before this script, active_outages() had zero production callers - tracked
outages were written to the store and never read back, so nothing ever used
them. Here they gate MOVE_AND_TRACK: a ticket whose signal pairs to a service
that already has an active tracked run is annotated as a continuation, not a
fresh open, so a reviewer reading the table can tell "new outage" from
"more of the one we already know about" instead of re-deriving it by hand.

Board-move-last and dry-run-by-default, same as cron/outage_writeback.py:
this script calls plan_writeback()/apply_plan() but NEVER passes apply=True.
Nothing it does writes to ConnectWise. Run it, read the table, decide.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from cron.cw_client import CWClient
from cron.outage_db import connect as connect_outage_db
from cron.outage_inventory import build_registry
from cron.outage_store import active_outages
from cron.outage_triage import BOARD_TRIAGE, learn_routing, learn_shape_routing, route_ticket
from cron.outage_writeback import apply_plan, plan_writeback

# How far back board history is read to learn routing. Matches the 90-day
# window outage_triage.py's docstring cites for its own learned-routing
# figures, so a rerun of this script reasons about the same population.
HISTORY_DAYS = 90


def _fetch_history(cw: CWClient, days: int = HISTORY_DAYS) -> list[dict]:
    since = datetime.now(timezone.utc).timestamp() - days * 86400
    since_iso = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return cw.paged(
        "/service/tickets",
        f"dateEntered>[{since_iso}] AND closedFlag=true",
        page_size=1000,
    )


def _fetch_open_triage(cw: CWClient) -> list[dict]:
    return cw.paged("/service/tickets", f'board/name="{BOARD_TRIAGE}" AND closedFlag=false', page_size=1000)


def run_routing(apply: bool = False) -> list[dict]:
    """Build the decision table, optionally writing it to ConnectWise.

    Split out from ``run()`` (which is hard-locked to dry-run below for
    CLI/REPL safety) so cron's ``outage_routing`` block has a seam to call
    with ``apply=True``. The owner reviewed today's dry-run table
    (2026-09-01) and approved letting Python apply move_and_close /
    move_and_track writes for machine-generated NOC/SOC tickets
    automatically - the model is never trusted to flip this, only a caller
    that explicitly passes ``apply=True`` does.
    """
    cw = CWClient()
    history = _fetch_history(cw)
    routing = learn_routing(history)
    shape_routing = learn_shape_routing(history)

    configurations = cw.paged("/company/configurations", "", page_size=1000)
    registry = build_registry(configurations)

    db = connect_outage_db()
    try:
        active = {row["service_key"]: row for row in active_outages(db) if row.get("service_key")}
    finally:
        db.close()

    open_tickets = _fetch_open_triage(cw)

    table = []
    for ticket in open_tickets:
        decision = route_ticket(ticket, registry, routing=routing, shape_routing=shape_routing)
        plan = plan_writeback(ticket, decision, outage_id=None)
        result = apply_plan(cw, plan, apply=apply)

        continuation = active.get(decision.service_key) if decision.service_key else None
        table.append({
            "ticket_id": ticket.get("id"),
            "summary": (ticket.get("summary") or "")[:100],
            "action": decision.action,
            "destination": decision.destination,
            "reason": decision.reason,
            "already_tracked_run_id": continuation.get("id") if continuation else None,
            "would_write": result.would_write,
            "applied": result.applied,
            "performed": result.performed,
            "failed_step": result.failed_step,
            "error": result.error,
        })
    return table


def run(apply: bool = False) -> list[dict]:
    """Build the decision table. Always dry-run (apply is accepted only so
    a human calling this from a REPL cannot silently no-op the writeback
    validation, never to actually write - see the assert immediately
    below). Use :func:`run_routing` for a real, opt-in write."""
    assert apply is False, "outage_dry_run never writes; use run_routing(apply=True) for that"
    return run_routing(apply=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the table as JSON instead of a text grid")
    args = parser.parse_args()

    table = run()

    if args.json:
        print(json.dumps(table, indent=2))
        return 0

    print(f"{'id':>7}  {'action':<16}  {'dest':<28}  {'summary':<60}  reason")
    for row in table:
        print(f"{row['ticket_id']:>7}  {row['action']:<16}  {(row['destination'] or '-'):<28}  "
              f"{row['summary']:<60}  {row['reason']}")
    print(f"\n{len(table)} open Triage tickets evaluated. Zero writes performed (dry run only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
