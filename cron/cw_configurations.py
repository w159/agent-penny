#!/usr/bin/env python3
"""
Read-only ConnectWise configuration-item (CI) fetch, split out of
cw_client.py to keep that file under the house 300-line cap.

Attached configurations are a CORROBORATING signal for trend_corpus.py,
never a gate: the owner's own framing is that a real, currently-active
trend can span many different devices with no shared configuration item
at all, identifiable only from note text. So a ticket with zero
configurations is a normal, expected case here - not an error and not a
signal to penalize, and normalize_configurations() below returns an
empty list for it rather than raising.

Confirmed live 2026-08-19 against real CW data: GET
/service/tickets/{id}/configurations returns a LINK record, not a full
CI record - it carries only `id`, `deviceIdentifier`, and
`_info.name`. There is no top-level `name`, `type`, `company`, or
`site` on it. The earlier docstring here (and the tests it was built
against) assumed a full record shape that was never verified live, and
normalize_configurations() dropped every real link record as a result
because it looked for a top-level `name` that doesn't exist. The
richer fields live on the full CI record at GET
/company/configurations/{id}. CIs repeat heavily across tickets (a
5-ticket sample already carried the same 2 CIs on 4 of them), so
configurations_for_tickets() resolves each distinct CI id once and
caches it rather than once per ticket - this also relieves 429
pressure on the tenant.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Sequence

from cron.cw_client import CWClient

logger = logging.getLogger(__name__)

# Henssler's CW tenant trips 429s well before the previous default of 4
# concurrent requests - see cron/cw_client.py's rate-limit constants and
# report.md for the measured numbers. Kept conservative here since this
# fetch runs both the per-ticket link list and, far less often because
# CIs repeat, the CI detail resolution.
_DEFAULT_WORKERS = 2


def _resolve_ci_details(
    client: CWClient,
    config_ids: set[int],
    *,
    known: dict[int, dict],
    workers: int,
) -> tuple[dict[int, dict], dict[int, dict]]:
    """type/company/site for each distinct CI id, resolved once and reused.

    Returns (merged, newly_resolved). `merged` is `known` plus every id
    just attempted - including ids that failed, so this batch's ticket
    records still get whatever was resolved. `newly_resolved` holds
    ONLY the successful new lookups: that is the one safe to persist,
    because caching a failed lookup as if it were resolved would mean
    it's silently never retried. A CI lookup failure degrades to an
    unresolved entry rather than raising - losing type/company/site
    must never cost the device identity already known from the link
    record (id, deviceIdentifier, _info.name), which is the actual
    corroborating signal trend_corpus.py relies on.
    """
    merged = dict(known)
    newly_resolved: dict[int, dict] = {}
    missing = [cid for cid in config_ids if cid is not None and cid not in merged]
    if not missing:
        return merged, newly_resolved

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_id = {
            executor.submit(client.get, f"/company/configurations/{cid}"): cid for cid in missing
        }
        for future in as_completed(future_to_id):
            cid = future_to_id[future]
            try:
                result = future.result() or {}
            except Exception as exc:  # noqa: BLE001 - degrade this one CI, don't fail the batch
                logger.warning(
                    "CW GET /company/configurations/%s failed, leaving type/company/site unresolved: %s",
                    cid, exc,
                )
                result = {}
            merged[cid] = result
            if result:
                newly_resolved[cid] = result
    return merged, newly_resolved


def configurations_for_tickets(
    client: CWClient,
    ticket_ids: Sequence[int],
    *,
    workers: int = _DEFAULT_WORKERS,
    ci_cache: dict[int, dict] | None = None,
) -> dict[int, list[dict]]:
    """Fetch attached configurations for many tickets in parallel, keyed by ticket id.

    Mirrors CWClient.notes_for_tickets: a failure on one ticket's
    configurations call raises (via future.result()) rather than being
    absorbed into an empty list that would look identical to "this
    ticket genuinely has no configurations attached" - and because the
    raise happens before this function returns, the caller's cache is
    never updated with a failed batch, so a failure can't be mistaken
    for (and cached as) a real empty result.

    Each returned link record is enriched in place with type/company/
    site resolved from the full CI record (see _resolve_ci_details).
    `ci_cache`, if given, is mutated with newly-resolved CIs so a
    caller can persist it across runs.
    """
    configs_by_ticket: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_id = {
            executor.submit(client.get, f"/service/tickets/{tid}/configurations", pageSize=200): tid
            for tid in ticket_ids
        }
        for future in as_completed(future_to_id):
            tid = future_to_id[future]
            configs_by_ticket[tid] = future.result() or []

    config_ids = {
        cfg.get("id")
        for records in configs_by_ticket.values()
        for cfg in records
        if isinstance(cfg, dict)
    }
    ci_cache = ci_cache if ci_cache is not None else {}
    merged, newly_resolved = _resolve_ci_details(client, config_ids, known=ci_cache, workers=workers)
    ci_cache.update(newly_resolved)

    for records in configs_by_ticket.values():
        for cfg in records:
            if not isinstance(cfg, dict):
                continue
            detail = merged.get(cfg.get("id")) or {}
            if not cfg.get("type"):
                cfg["type"] = detail.get("type")
            if not cfg.get("company"):
                cfg["company"] = detail.get("company")
            if not cfg.get("site"):
                cfg["site"] = detail.get("site")

    return configs_by_ticket


def normalize_configurations(raw_configs: list) -> list[dict]:
    """Raw CW configuration objects -> normalized {id, name, type, company, site, device_identifier} dicts.

    Malformed entries are skipped, never raised on - a bad configuration
    record must not take down the whole digest for a ticket whose notes
    are otherwise fine. `name` falls back to `_info.name` because the
    ticket-configurations link record never carries a top-level name -
    only the resolved full CI record does, and configurations_for_tickets
    doesn't backfill a top-level `name` onto the link record, so this
    stays defensive for any caller (tests included) passing a raw link
    record straight through. An entry with neither a name nor a device
    identifier carries no usable identity and is dropped; one with
    either is kept, even if type/company/site never resolved.
    """
    normalized = []
    for cfg in raw_configs or []:
        if not isinstance(cfg, dict):
            continue
        name = cfg.get("name") or (cfg.get("_info") or {}).get("name")
        device_identifier = cfg.get("deviceIdentifier") or ""
        if not name and not device_identifier:
            continue
        normalized.append(
            {
                "id": cfg.get("id"),
                "name": name or "",
                "type": (cfg.get("type") or {}).get("name") or "",
                "company": (cfg.get("company") or {}).get("name") or "",
                "site": (cfg.get("site") or {}).get("name") or "",
                "device_identifier": device_identifier,
            }
        )
    return normalized
