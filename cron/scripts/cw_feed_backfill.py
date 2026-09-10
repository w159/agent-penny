#!/usr/bin/env python3
"""Poll ConnectWise directly to close gaps in the callback log.

Every Penny scheduled duty (board watcher, escalations, trend pass) reads
ticket state from ONE place: logs/cw_callback_payloads.jsonl, via
cron.trend_detection.load_tickets_from_cw_log(). That log is normally fed by
the ConnectWise callback, not by polling - see cron/cw_client.py's module
docstring for why a live client exists at all.

The callback was deactivated by ConnectWise from 2026-08-26T03:02:43Z to
2026-09-01T21:49Z (reactivated). Every ticket add/update on the Triage board
during that window never landed in the log, so the watchers, escalations,
and trend corpus were all blind to six days of activity. This script is the
poll-based fallback that closes that gap: it fetches tickets updated since
the log's last recorded timestamp and appends them in the same record shape
a real callback would have written, so load_tickets_from_cw_log() picks them
up exactly as if the callback had never gone down.

It is also meant to run again in the future if the callback drops out again
(a watchdog can shell out to this script), which is why --since is
overridable rather than hardcoded to this specific outage.

Idempotent: a ticket already represented in the log at least as recently as
CW reports is skipped, so running this repeatedly (or overlapping windows)
never duplicates a record.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from cron.cw_client import CWClient, CWError
from cron.trend_detection import CW_LOG

DEFAULT_BOARD = "Triage"
_SINCE_LOOKBACK = timedelta(minutes=5)
_BACKFILL_SOURCE = "cw_feed_backfill"


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z'. None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_ts(dt: datetime) -> str:
    """Render a datetime as the Z-suffixed UTC ISO-8601 string CW conditions expect."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_default_since(path: Path = CW_LOG) -> datetime:
    """Newest 'ts' in the jsonl minus a lookback buffer, so the poll window
    overlaps the log's last known state instead of leaving a seam at the
    exact cutover second. Unparseable lines are skipped, not fatal.
    """
    newest: Optional[datetime] = None
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts and (newest is None or ts > newest):
                    newest = ts
    if newest is None:
        # Empty or missing log: fall back to "an hour ago" rather than
        # fetching CW's entire ticket history.
        newest = datetime.now(timezone.utc) - timedelta(hours=1)
    return newest - _SINCE_LOOKBACK


def load_latest_updated_by_id(path: Path = CW_LOG) -> dict[int, datetime]:
    """id -> newest known Entity._info.lastUpdated, read straight from the
    jsonl (not via load_tickets_from_cw_log, which drops the timestamp).
    Used only to decide what to skip; never rewrites the log.
    """
    latest: dict[int, datetime] = {}
    if not path.exists():
        return latest
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                entity = json.loads(rec["payload"]["Entity"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            tid = entity.get("id")
            if tid is None:
                continue
            updated = _parse_ts((entity.get("_info") or {}).get("lastUpdated"))
            if updated is None:
                continue
            if tid not in latest or updated > latest[tid]:
                latest[tid] = updated
    return latest


def fetch_board_tickets(client: CWClient, board: str, since: datetime) -> list[dict[str, Any]]:
    """All tickets on `board` with _info.lastUpdated >= since, any pages."""
    conditions = f'board/name="{board}" and lastUpdated>=[{_fmt_ts(since)}]'
    return client.paged("/service/tickets", conditions)


def build_backfill_record(ticket: dict[str, Any]) -> dict[str, Any]:
    """One jsonl record in the same shape a real CW callback writes, marked
    as a backfill via Action="backfill" and Metadata.source so it stays
    distinguishable from a genuine callback.
    """
    info = ticket.get("_info") or {}
    last_updated = info.get("lastUpdated")
    ts = last_updated or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "MessageId": None,
        "FromUrl": "na.myconnectwise.net",
        "CompanyId": "henssler",
        "MemberId": "AgentPenny",
        "Action": "backfill",
        "Type": "ticket",
        "ID": ticket.get("id"),
        "ProductInstanceId": None,
        "PartnerId": None,
        "Entity": json.dumps(ticket),
        "Metadata": {"source": _BACKFILL_SOURCE},
    }
    return {"ts": ts, "payload": payload}


def select_new_or_updated(
    tickets: list[dict[str, Any]], latest_by_id: dict[int, datetime]
) -> list[dict[str, Any]]:
    """Drop any ticket the log already holds at least as recent a
    lastUpdated for - the idempotency guarantee.
    """
    out = []
    for ticket in tickets:
        tid = ticket.get("id")
        if tid is None:
            continue
        fetched_updated = _parse_ts((ticket.get("_info") or {}).get("lastUpdated"))
        known = latest_by_id.get(tid)
        if known is not None and fetched_updated is not None and known >= fetched_updated:
            continue
        out.append(ticket)
    return out


def backup_log(path: Path) -> Path:
    """Timestamped copy before the first append, matching the existing
    cw_callback_payloads.jsonl.bak.<UTC compact ts> convention in logs/.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def append_records(path: Path, records: list[dict[str, Any]]) -> None:
    """Append one line per record, flushing each write. Never rewrites or
    reorders existing lines.
    """
    with path.open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record))
            f.write("\n")
            f.flush()


def run(board: str, since: datetime, apply: bool, path: Path = CW_LOG) -> int:
    try:
        client = CWClient()
        tickets = fetch_board_tickets(client, board, since)
    except CWError as exc:
        print(f"backfill: ConnectWise fetch failed: {exc}", file=sys.stderr)
        return 1

    latest_by_id = load_latest_updated_by_id(path)
    to_append = select_new_or_updated(tickets, latest_by_id)
    records = [build_backfill_record(t) for t in to_append]

    if not apply:
        print(
            f"backfill: dry run, would append {len(records)} record(s) "
            f"(fetched {len(tickets)} ticket(s) on board={board!r} since={_fmt_ts(since)})"
        )
        return 0

    if records:
        if path.exists():
            backup_path = backup_log(path)
            print(f"backfill: backed up log to {backup_path}", file=sys.stderr)
        append_records(path, records)

    newest_ts = max((r["ts"] for r in records), default=_fmt_ts(since))
    print(f"backfill: appended {len(records)} record(s), newest_ts={newest_ts}, since={_fmt_ts(since)}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default=DEFAULT_BOARD, help="CW board name (default: Triage)")
    parser.add_argument("--since", default=None, help="ISO-8601 UTC lower bound; default is derived from the log")
    parser.add_argument("--apply", action="store_true", help="Actually append records (default: dry run)")
    args = parser.parse_args(argv)

    since = _parse_ts(args.since) if args.since else compute_default_since()
    if args.since and since is None:
        print(f"backfill: could not parse --since value {args.since!r}", file=sys.stderr)
        return 1

    return run(args.board, since, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
