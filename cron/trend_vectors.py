#!/usr/bin/env python3
"""
Embedding client and local vector store for semantic trend clustering.

cron/trend_cluster_semantic.py currently groups help-desk tickets with LLM
prompts, which is slow and inconsistent when two techs describe the same
underlying issue in different words. This module is the embedding layer
that replaces the LLM-prompt comparison with cosine similarity over
sentence embeddings -- it is deliberately standalone; wiring it into the
trend pipeline is a separate task.

Persists to memories/ops/trend_vectors.json, alongside the other ops
memory files (see cron/trend_state.py's OPS_DIR).

Standard library only: urllib.request, json, math -- matching cron/
cw_client.py's no-`requests` convention.
"""
from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Sequence

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

OPS_DIR = get_hermes_home() / "memories" / "ops"
VECTOR_STORE_FILE = OPS_DIR / "trend_vectors.json"

# Live-measured on this box against bge-m3 (2026-08-19, real 21-day/1444-
# ticket ConnectWise corpus): one /api/embed request holding a whole
# corpus never returns inside any sane timeout -- one text costs ~2.5-3.5s
# server-side with no batching speedup (16 texts = 56s, 32 = 82s, both
# ~N * per-text cost). embed_texts() chunks into DEFAULT_EMBED_BATCH_SIZE
# batches, each bounded by DEFAULT_EMBED_TIMEOUT instead of one
# whole-corpus timeout. Defaults for the not-yet-wired
# cron.trend.embed_batch_size / cron.trend.embed_timeout config keys --
# see this module's docstring on why config wiring is a separate task.
DEFAULT_EMBED_BATCH_SIZE = 16
DEFAULT_EMBED_TIMEOUT = 120.0

# Bounded retry for a single batch: covers a transient timeout/connection
# blip without masking a genuinely dead endpoint behind endless retries.
_EMBED_MAX_ATTEMPTS = 3
_EMBED_RETRY_BACKOFF_SECONDS = 2.0

# Overall wall-clock budget across every batch in one embed_texts() call.
# Each individual HTTP read is already bounded by DEFAULT_EMBED_TIMEOUT,
# but that bounds one recv, not the whole request -- a server that keeps
# a connection alive by trickling bytes slower than the socket timeout
# (or one that is simply overloaded and answers every batch just inside
# its own timeout) can still make a real-scale corpus (hundreds of
# unique tickets, dozens of batches) run for hours with no single call
# ever raising. Confirmed live: a trend_pass run on 2026-08-24 never
# reached its own completion log line and produced no exception in over
# 2.5 hours (see cron/trend_pass.py incident notes) -- this budget turns
# that silent, unbounded hang into a loud, bounded failure so the trend
# pass fails fast instead of leaking a thread forever.
DEFAULT_EMBED_TOTAL_BUDGET_SECONDS = 600.0


class EmbeddingError(Exception):
    """Raised on any embedding failure: HTTP error, timeout, malformed response.

    Never swallowed into a silently-wrong (e.g. all-zero) vector -- a
    caller comparing bad vectors would get plausible-looking but
    meaningless similarity scores.
    """


def embed_texts(
    texts: list[str],
    *,
    model: str = "bge-m3",
    base_url: str = "http://localhost:11434",
    batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    timeout: float = DEFAULT_EMBED_TIMEOUT,
    total_budget_seconds: float = DEFAULT_EMBED_TOTAL_BUDGET_SECONDS,
) -> list[list[float]]:
    """Embed texts via Ollama, in the same order as `texts`.

    Tries the modern batch endpoint (POST /api/embed, {"input": [...]} ->
    {"embeddings": [[...]]}) first, split into chunks of `batch_size` so a
    real-scale corpus doesn't sit behind one HTTP request that never
    returns (see DEFAULT_EMBED_BATCH_SIZE/DEFAULT_EMBED_TIMEOUT above for
    the measurement). Each chunk's vectors land at that chunk's original
    position in the output, so batching never reorders results relative to
    `texts`.

    `total_budget_seconds` bounds the whole call, not just one batch (see
    DEFAULT_EMBED_TOTAL_BUDGET_SECONDS docstring) -- checked before
    starting each new batch, so a run already past budget fails loud
    instead of starting one more multi-minute batch.

    Falls back to POST /api/embeddings (single "prompt" -> single
    "embedding", one call per text) only when the batch endpoint itself is
    unavailable (404/501), not on an unrelated transient failure, so a real
    outage still raises.
    """
    if not texts:
        return []
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")

    deadline = time.monotonic() + total_budget_seconds
    try:
        return _embed_in_batches(
            texts, model=model, base_url=base_url, batch_size=batch_size,
            timeout=timeout, deadline=deadline,
        )
    except EmbeddingError as batch_exc:
        if not _looks_like_missing_endpoint(batch_exc):
            raise
        logger.info("trend_vectors: /api/embed unavailable, falling back to /api/embeddings")
        return [_embed_single(text, model=model, base_url=base_url, timeout=timeout) for text in texts]


def _embed_in_batches(
    texts: list[str], *, model: str, base_url: str, batch_size: int, timeout: float, deadline: float,
) -> list[list[float]]:
    """Chunk `texts`, embed each chunk in order, concatenate the results --
    concatenation keeps output order identical to input order."""
    chunks = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
    total = len(chunks)
    vectors: list[list[float]] = []
    for batch_num, chunk in enumerate(chunks, start=1):
        if time.monotonic() > deadline:
            raise EmbeddingError(
                f"embedding aborted before batch {batch_num}/{total}: total wall-clock "
                f"budget exhausted ({len(vectors)}/{len(texts)} texts embedded so far)"
            )
        logger.info("trend_vectors: embedding batch %d of %d (%d texts)", batch_num, total, len(chunk))
        vectors.extend(
            _embed_batch_with_retry(chunk, model=model, base_url=base_url, timeout=timeout, batch_num=batch_num, total=total)
        )
    return vectors


def _embed_batch_with_retry(
    chunk: list[str], *, model: str, base_url: str, timeout: float, batch_num: int, total: int
) -> list[list[float]]:
    """One batch, retried with bounded backoff on a transient failure. A
    missing-endpoint error propagates on the first attempt (not retried)
    so `embed_texts` can fall back to /api/embeddings immediately."""
    last_exc: Optional[EmbeddingError] = None
    for attempt in range(1, _EMBED_MAX_ATTEMPTS + 1):
        try:
            return _embed_batch(chunk, model=model, base_url=base_url, timeout=timeout)
        except EmbeddingError as exc:
            if _looks_like_missing_endpoint(exc):
                raise
            last_exc = exc
            if not _is_transient(exc) or attempt == _EMBED_MAX_ATTEMPTS:
                break
            backoff = _EMBED_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "trend_vectors: batch %d/%d attempt %d/%d failed (%s), retrying in %.1fs",
                batch_num, total, attempt, _EMBED_MAX_ATTEMPTS, exc, backoff,
            )
            time.sleep(backoff)

    # Never substitute a partial/zero-filled result for a batch that never
    # succeeded -- a missing embedding must fail loud, not masquerade as
    # a real (if degraded) vector.
    raise EmbeddingError(
        f"embedding batch {batch_num}/{total} ({len(chunk)} texts) failed after "
        f"{_EMBED_MAX_ATTEMPTS} attempt(s): {last_exc}"
    ) from last_exc


def _is_transient(exc: EmbeddingError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in ("timed out", "timeout", "connection reset", "connection refused"))


def _looks_like_missing_endpoint(exc: EmbeddingError) -> bool:
    message = str(exc)
    return "404" in message or "501" in message or "not found" in message.lower()


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise EmbeddingError(f"POST {url} failed: {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EmbeddingError(f"POST {url} failed: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EmbeddingError(f"POST {url} returned malformed JSON: {raw[:200]!r}") from exc


def _embed_batch(texts: list[str], *, model: str, base_url: str, timeout: float) -> list[list[float]]:
    data = _post_json(f"{base_url}/api/embed", {"model": model, "input": texts}, timeout)
    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise EmbeddingError(f"/api/embed returned unexpected shape: {data!r}")
    return vectors


def _embed_single(text: str, *, model: str, base_url: str, timeout: float) -> list[float]:
    data = _post_json(f"{base_url}/api/embeddings", {"model": model, "prompt": text}, timeout)
    vector = data.get("embedding")
    if not isinstance(vector, list):
        raise EmbeddingError(f"/api/embeddings returned unexpected shape: {data!r}")
    return vector


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors. A zero-magnitude vector yields 0.0
    rather than raising ZeroDivisionError -- callers rank many pairs at
    once and one degenerate embedding shouldn't crash the whole pass."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


class VectorStore:
    """Local persisted store of {key -> (vector, metadata)}, atomic on write.

    Mirrors cron/trend_state.py's load/save pattern: a missing or corrupt
    file starts empty rather than crashing the cron job.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or VECTOR_STORE_FILE
        self._entries: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("trend_vectors: store file unreadable (%s), starting fresh", e)
            return {}
        if not isinstance(raw, dict):
            logger.warning("trend_vectors: store file not a dict, starting fresh")
            return {}
        return raw

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._entries, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def upsert(self, key: str, vector: list[float], metadata: dict) -> None:
        self._entries[key] = {"vector": vector, "metadata": metadata}
        self._save()

    def get(self, key: str) -> Optional[dict]:
        return self._entries.get(key)

    def all_entries(self) -> list[dict]:
        return [{"key": key, **entry} for key, entry in self._entries.items()]

    def prune_before(self, cutoff_iso: str) -> int:
        """Drop entries whose metadata "timestamp" predates `cutoff_iso`.

        An entry with no timestamp (or an unparseable one) is treated as
        stale and dropped -- it can't be proven to still be in the rolling
        window, so keeping it would silently grow the store unbounded.
        """
        stale_keys = [
            key
            for key, entry in self._entries.items()
            if _timestamp_before(entry.get("metadata", {}).get("timestamp"), cutoff_iso)
        ]
        for key in stale_keys:
            del self._entries[key]
        if stale_keys:
            self._save()
        return len(stale_keys)

    def nearest(self, vector: Sequence[float], *, threshold: float, limit: int = 50) -> list[tuple[str, float]]:
        """Cosine-ranked keys at or above `threshold`, descending by score."""
        scored = [
            (key, cosine_similarity(vector, entry["vector"]))
            for key, entry in self._entries.items()
        ]
        scored = [(key, score) for key, score in scored if score >= threshold]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:limit]


def _timestamp_before(timestamp: Optional[str], cutoff_iso: str) -> bool:
    if not timestamp or not isinstance(timestamp, str):
        return True
    return timestamp < cutoff_iso


def cluster_by_similarity(items: list[dict], vectors: list[list[float]], *, threshold: float) -> list[list[int]]:
    """Deterministic agglomerative-style grouping of `items` by index.

    Each item joins the first existing group where it scores >= threshold
    against every current member of that group (complete-link), else it
    starts a new group. Groups and members are visited in index order so
    the same input always yields the same output regardless of hashing or
    set-iteration order.
    """
    groups: list[list[int]] = []
    for idx, vector in enumerate(vectors):
        placed = False
        for group in groups:
            if all(cosine_similarity(vector, vectors[member]) >= threshold for member in group):
                group.append(idx)
                placed = True
                break
        if not placed:
            groups.append([idx])
    return groups
