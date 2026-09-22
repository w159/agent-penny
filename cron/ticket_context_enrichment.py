#!/usr/bin/env python3
"""
Nightly per-ticket context enrichment for cron/cw_contact_index.py's local
ticket index: what was discussed, how it was resolved, how long it stayed
open, who owned it, and which configuration items were attached -- so Penny
can answer a ticket-history question from the local index instead of a live
CW pull, the same staleness trade cw_contact_index.py already makes for
contact/requester trend queries.

Every field here reuses code already verified correct against the real
ConnectWise tenant this session, rather than reinventing extraction:
  - issue/resolution text: cron/trend_corpus.py's _issue_and_resolution(),
    which already knows to prefer detailDescriptionFlag/resolutionFlag
    notes and fall back to the last non-issue note.
  - configuration items: cron/cw_configurations.py's
    configurations_for_tickets()/normalize_configurations(), which already
    handles the real link-record shape and resolves type/company/site.
  - owner, closedDate, dateResolved, parentTicketId, hasChildTicket,
    hasMergedChildTicketFlag: read straight off the base ticket object
    (GET /service/tickets/{id}, the same resource refresh_index() already
    pulls in bulk via tickets_since()) -- no new CW endpoint.

resolution_source is the one field with no reliable ground truth in this
tenant: resolutionFlag notes are essentially never set (verified live,
2026-09-22: 0 of 38+ sampled notes across several fully-closed tickets), so
_issue_and_resolution()'s fallback -- "the last note that isn't the issue
note" -- was confirmed unreliable on real data (e.g. ticket #90653's last
note is an internal escalation nudge two days before close, not a
resolution). resolution_text is therefore always carried with an explicit
resolution_source provenance marker ('resolutionFlag' | 'heuristic_last_note'
| 'none') rather than ever being presented as ground truth on its own.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

from cron.cw_client import CWClient
from cron.cw_configurations import configurations_for_tickets, normalize_configurations
from cron.cw_contact_index import connect
from cron.trend_corpus import _issue_and_resolution

logger = logging.getLogger(__name__)

# Same rationale as cron/cw_configurations.py's _DEFAULT_WORKERS: Henssler's
# CW tenant trips 429s well before higher concurrency, and this run adds a
# third parallel fetch (base tickets) alongside notes and configurations.
_DEFAULT_WORKERS = 2

# An enriched ticket is re-enriched after this many days rather than never
# again: an open ticket can gain its resolution note, close, or get
# relinked as a child at any point up to close, so a one-time enrichment
# would silently go stale for exactly the tickets most worth re-checking.
# 14 days is a deliberate middle ground -- long enough that a closed
# ticket (the common steady state) isn't re-pulled every night for no
# reason, short enough that a still-open ticket gets re-checked well
# within a typical resolution window.
_DEFAULT_STALE_AFTER_DAYS = 14

# Caps how many tickets one nightly run touches. The real index holds
# 5,722 rows; enriching all of them in one run means ~17,000 CW calls
# (base ticket + notes + configurations, per ticket) in a single night,
# which is exactly the kind of burst that trips the tenant's rate limiter
# (see cron/cw_client.py's _MAX_ATTEMPTS_RATE_LIMIT comment). Bounding a
# single run to this many tickets makes an initial backlog a multi-night
# backfill instead of a one-night incident; due_ticket_ids() always
# returns the oldest-enriched (or never-enriched) tickets first, so the
# backlog drains in a stable order across runs.
_DEFAULT_BATCH_LIMIT = 200

RESOLUTION_SOURCE_FLAG = "resolutionFlag"
RESOLUTION_SOURCE_HEURISTIC = "heuristic_last_note"
RESOLUTION_SOURCE_NONE = "none"

_UPDATE_SQL = """
    UPDATE tickets SET
        owner_id = :owner_id,
        owner_name = :owner_name,
        owner_identifier = :owner_identifier,
        contact_phone = :contact_phone,
        issue_text = :issue_text,
        resolution_text = :resolution_text,
        resolution_source = :resolution_source,
        closed_date = :closed_date,
        closed_flag = :closed_flag,
        date_resolved = :date_resolved,
        configurations = :configurations,
        parent_ticket_id = :parent_ticket_id,
        has_child_ticket = :has_child_ticket,
        has_merged_child_flag = :has_merged_child_flag,
        context_enriched_at = :context_enriched_at
    WHERE id = :id
"""


def due_ticket_ids(
    *, stale_after_days: int = _DEFAULT_STALE_AFTER_DAYS, limit: Optional[int] = _DEFAULT_BATCH_LIMIT,
    db_path: Optional[Path] = None,
) -> list[int]:
    """Ticket ids due for enrichment: never enriched, or enriched more than
    `stale_after_days` ago. Oldest-entered first, so a multi-night backfill
    works through the real ticket history in a stable, resumable order
    rather than an arbitrary one."""
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=stale_after_days)).isoformat()
    conn = connect(db_path)
    try:
        query = (
            "SELECT id FROM tickets "
            "WHERE context_enriched_at IS NULL OR context_enriched_at < ? "
            "ORDER BY date_entered ASC"
        )
        params: list = [cutoff_iso]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        return [row["id"] for row in conn.execute(query, params)]
    finally:
        conn.close()


def _fetch_base_tickets(client: CWClient, ticket_ids: Sequence[int], *, workers: int = _DEFAULT_WORKERS) -> dict[int, dict]:
    """Base ticket objects for `ticket_ids`, keyed by id. This is the exact
    /service/tickets/{id} resource refresh_index() already pulls in bulk via
    tickets_since() -- narrowed to one id at a time here because an
    enrichment run's due list is picked by staleness, not a contiguous date
    window, so a single bulk conditions= pull can't target it. A failure on
    one ticket raises via future.result() (matches CWClient.notes_for_tickets'
    contract): partial data must surface as an error, never a silently-empty
    result mistaken for "no owner / never closed"."""
    tickets_by_id: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_id = {executor.submit(client.get, f"/service/tickets/{tid}"): tid for tid in ticket_ids}
        for future in as_completed(future_to_id):
            tid = future_to_id[future]
            tickets_by_id[tid] = future.result()
    return tickets_by_id


def _resolution_source(notes: list, resolution_text: str) -> str:
    """Provenance marker for resolution_text: CW's own resolutionFlag note
    if one exists, else the heuristic fallback _issue_and_resolution()
    already used to produce resolution_text, else nothing at all. Kept
    distinct from _issue_and_resolution() itself so that function's
    return value never has to double as its own confidence marker."""
    has_resolution_flag = any(isinstance(note, dict) and note.get("resolutionFlag") for note in notes or [])
    if has_resolution_flag:
        return RESOLUTION_SOURCE_FLAG
    if resolution_text:
        return RESOLUTION_SOURCE_HEURISTIC
    return RESOLUTION_SOURCE_NONE


def _build_ticket_update(ticket_id: int, base_ticket: dict, notes: list, raw_configs: list) -> dict:
    """Pure transform: one ticket's base/notes/configuration data -> the
    column values for _UPDATE_SQL. No I/O, so this is unit-testable without
    a CW client or a database."""
    owner = base_ticket.get("owner") or {}
    issue_text, resolution_text = _issue_and_resolution(notes)
    return {
        "id": ticket_id,
        "owner_id": owner.get("id"),
        "owner_name": owner.get("name") or "",
        "owner_identifier": owner.get("identifier") or "",
        "contact_phone": base_ticket.get("contactPhoneNumber") or "",
        "issue_text": issue_text,
        "resolution_text": resolution_text,
        "resolution_source": _resolution_source(notes, resolution_text),
        "closed_date": base_ticket.get("closedDate"),
        "closed_flag": 1 if base_ticket.get("closedFlag") else 0,
        "date_resolved": base_ticket.get("dateResolved"),
        "configurations": json.dumps(normalize_configurations(raw_configs)),
        "parent_ticket_id": base_ticket.get("parentTicketId"),
        "has_child_ticket": 1 if base_ticket.get("hasChildTicket") else 0,
        "has_merged_child_flag": 1 if base_ticket.get("hasMergedChildTicketFlag") else 0,
        "context_enriched_at": datetime.now(timezone.utc).isoformat(),
    }


def enrich_tickets(
    ticket_ids: Sequence[int], *, client: Optional[CWClient] = None, db_path: Optional[Path] = None,
) -> dict:
    """Pull notes/configurations/base-ticket data for `ticket_ids` and
    UPDATE (never INSERT OR REPLACE) their local index rows with it -- an
    UPDATE never clobbers the base contact/company/status columns
    refresh_index() owns, and a ticket id with no existing row (never seen
    by refresh_index) is counted, not silently dropped or errored on.

    A CW failure propagates (matches refresh_index's contract): a partial
    enrichment run must surface as an error, never as "0 tickets enriched
    today" indistinguishable from a genuinely empty due-list.
    """
    ticket_ids = list(dict.fromkeys(int(tid) for tid in ticket_ids))
    if not ticket_ids:
        return {
            "tickets_requested": 0,
            "tickets_enriched": 0,
            "tickets_not_found_in_index": 0,
            "resolution_source_counts": {},
            "enriched_at": None,
        }

    cw = client or CWClient()
    base_tickets = _fetch_base_tickets(cw, ticket_ids)
    notes_by_id = cw.notes_for_tickets(ticket_ids)
    configs_by_id = configurations_for_tickets(cw, ticket_ids)

    updates = [
        _build_ticket_update(tid, base_tickets.get(tid) or {}, notes_by_id.get(tid, []), configs_by_id.get(tid, []))
        for tid in ticket_ids
    ]

    conn = connect(db_path)
    try:
        enriched_ids: list[int] = []
        not_found_ids: list[int] = []
        resolution_source_counts = {RESOLUTION_SOURCE_FLAG: 0, RESOLUTION_SOURCE_HEURISTIC: 0, RESOLUTION_SOURCE_NONE: 0}
        for update in updates:
            cursor = conn.execute(_UPDATE_SQL, update)
            if cursor.rowcount:
                enriched_ids.append(update["id"])
                resolution_source_counts[update["resolution_source"]] += 1
            else:
                not_found_ids.append(update["id"])
    finally:
        conn.close()

    if not_found_ids:
        logger.warning(
            "ticket_context_enrichment: %d ticket id(s) had no existing index row "
            "(never pulled by refresh_index): %s", len(not_found_ids), not_found_ids,
        )

    summary = {
        "tickets_requested": len(ticket_ids),
        "tickets_enriched": len(enriched_ids),
        "tickets_not_found_in_index": len(not_found_ids),
        "resolution_source_counts": resolution_source_counts,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("ticket_context_enrichment: run complete: %s", summary)
    return summary


def run_nightly_enrichment(
    *, stale_after_days: int = _DEFAULT_STALE_AFTER_DAYS, batch_limit: Optional[int] = _DEFAULT_BATCH_LIMIT,
    client: Optional[CWClient] = None, db_path: Optional[Path] = None,
) -> dict:
    """The nightly job's entrypoint: pick the due tickets, enrich them.
    Split from enrich_tickets so a caller (or a test) can enrich an
    explicit id list -- e.g. a real verified cluster -- without going
    through the staleness selection."""
    ticket_ids = due_ticket_ids(stale_after_days=stale_after_days, limit=batch_limit, db_path=db_path)
    return enrich_tickets(ticket_ids, client=client, db_path=db_path)
