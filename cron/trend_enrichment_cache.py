#!/usr/bin/env python3
"""
Resumable per-ticket cache for cron/trend_corpus.py's CW enrichment fetch.

A 90-day window can be thousands of tickets, each needing a notes call
and a configurations call. Without a cache, re-running the exporter or a
retried cron pass refetches every one of those calls from scratch. This
module stores raw notes/configurations per ticket id, keyed by id, so a
second pass only fetches tickets not already on disk.

Also holds a third kind, "ci", keyed by ConnectWise configuration-item
id rather than ticket id: cw_configurations.py resolves each distinct
CI's type/company/site once (CIs repeat heavily across tickets) and
this cache is where that resolution persists across runs. Only
successful CI resolutions ever land here - see
cw_configurations._resolve_ci_details, which never writes an
unresolved lookup back into the dict it's handed, so a CI that failed
to resolve is retried next run instead of staying permanently blank.

Persists to memories/ops/trend_enrichment_cache.json, alongside
trend_vectors.py's VECTOR_STORE_FILE (see that module's docstring for
the OPS_DIR convention). Standard library only.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

OPS_DIR = get_hermes_home() / "memories" / "ops"
CACHE_FILE = OPS_DIR / "trend_enrichment_cache.json"


_KINDS = ("notes", "configurations", "ci")


class EnrichmentCache:
    """JSON-file cache of per-ticket notes/configurations plus per-CI
    detail, keyed by ticket id (notes, configurations) or CI id (ci).

    Ids can never collide across kinds because each kind gets its own
    top-level dict, so a ticket cached for notes but not yet for
    configurations is fetched only for the missing kind.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or CACHE_FILE
        self._data: dict[str, dict[str, object]] = {kind: {} for kind in _KINDS}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt cache file must not crash the enrichment pass -
            # treat it as empty and let every ticket refetch.
            logger.error("trend_enrichment_cache: failed to load %s, starting empty: %s", self._path, exc)
            return
        if isinstance(raw, dict):
            for kind in _KINDS:
                self._data[kind].update(raw.get(kind) or {})

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data))

    def missing_ids(self, kind: str, ticket_ids: list[int]) -> list[int]:
        """Ids in `ticket_ids` not yet cached for `kind` ("notes", "configurations", or "ci")."""
        cached = self._data[kind]
        return [tid for tid in ticket_ids if str(tid) not in cached]

    def get_all(self, kind: str) -> dict[int, object]:
        return {int(k): v for k, v in self._data[kind].items()}

    def update(self, kind: str, fresh: dict[int, object]) -> None:
        for tid, value in fresh.items():
            self._data[kind][str(tid)] = value
