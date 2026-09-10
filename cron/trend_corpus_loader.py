#!/usr/bin/env python3
"""
load_corpus(): the only I/O in the trend-corpus feature, split out of
trend_corpus.py to keep that file under the house 300-line cap.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cron.cw_client import CWClient
from cron.cw_configurations import configurations_for_tickets
from cron.trend_corpus import build_digests
from cron.trend_enrichment_cache import EnrichmentCache


def load_corpus(
    days: int = 14, *, client: CWClient | None = None, cache: EnrichmentCache | None = None
) -> list:
    """
    Pulls tickets/notes/time-entries/configurations live from
    ConnectWise and hands them to build_digests(). A CW failure
    propagates as CWError - it must never be swallowed into an empty
    corpus, because "no trends found" and "the API is down" have to
    look different to whatever reads this.

    Notes and configurations are fetched only for ticket ids not already
    in `cache` (defaults to the shared on-disk EnrichmentCache), so a
    re-run over the same 90-day window doesn't refetch thousands of
    already-enriched tickets. Cache writes happen after each successful
    fetch, not just at the end, so a mid-run failure still keeps the
    work already done.
    """
    cw = client or CWClient()
    cache = cache if cache is not None else EnrichmentCache()
    since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    tickets = cw.tickets_since(since_iso)
    ticket_ids = [t["id"] for t in tickets if isinstance(t, dict) and "id" in t]

    missing_notes = cache.missing_ids("notes", ticket_ids)
    if missing_notes:
        cache.update("notes", cw.notes_for_tickets(missing_notes))
        cache.save()
    notes_by_id = cache.get_all("notes")

    missing_configs = cache.missing_ids("configurations", ticket_ids)
    if missing_configs:
        # ci_cache is mutated in place with newly-resolved CIs only
        # (see cw_configurations._resolve_ci_details) - a CI that
        # failed to resolve this run is not written back, so it's
        # retried next run instead of staying permanently unresolved.
        ci_cache = cache.get_all("ci")
        cache.update("configurations", configurations_for_tickets(cw, missing_configs, ci_cache=ci_cache))
        cache.update("ci", ci_cache)
        cache.save()
    configurations_by_id = cache.get_all("configurations")

    time_entries = cw.time_entries_since(since_iso)

    return build_digests(tickets, notes_by_id, time_entries, configurations_by_id)
