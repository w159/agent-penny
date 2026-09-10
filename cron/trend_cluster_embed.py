#!/usr/bin/env python3
"""
Stage A of cron/trend_cluster_semantic.py's pipeline: the merge decision.

Used to be a single LLM prompt shown every promoted candidate at once (see
git history / the old build_stage_a_prompt in cron/trend_cluster_prompt.py).
That was nondeterministic (a re-run of the same corpus could merge
differently) and expensive (one big prompt per cycle). This module replaces
it with cosine similarity over cron/trend_vectors.py sentence embeddings --
deterministic and re-runnable: two candidates merge purely on how close
their ticket text embeds, never on a model's mood that run.

Embeds by TICKET, not by candidate, and caches vectors in VectorStore keyed
by ticket id. Candidates are re-formed fresh every cron cycle as new
tickets enter the rolling window, but a given ticket's own text never
changes, so a daily re-run only pays to embed newly-seen tickets.

Split out to keep trend_cluster_semantic.py under the house 300-line cap,
the same pattern as trend_cluster_prompt.py/trend_cluster_output.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from cron.trend_cluster_output import _date_only, best_evidence
from cron.trend_vectors import VectorStore, cluster_by_similarity, embed_texts

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.58
DEFAULT_EMBEDDING_MODEL = "bge-m3"

# Live-measured on this box against bge-m3 (1024 dims): a genuine paraphrase
# pair ("printer offline in accounting" / "accounting floor printer will not
# print") scores 0.7092; an unrelated pair ("printer offline in accounting" /
# "cannot connect to VPN from home") scores 0.3845. 0.58 sits in the gap
# between the two, closer to the paraphrase score so real duplicates merge
# without pulling in the unrelated pair.
assert 0.3845 < DEFAULT_SIMILARITY_THRESHOLD < 0.7092

# One ticket's embed text budget. Entities carry the normalized signal (see
# trend_entities.py); summary/evidence add the wording variance an
# embedding model needs to actually place two differently-worded
# descriptions of the same fault close together. Never the raw note body --
# trend_corpus.py has already stripped HTML/signature noise out of summary,
# issue, resolution, and tech_notes upstream of this module.
_DIGEST_TEXT_CHARS = 600


def _digest_embed_text(d) -> str:
    """Normalized embed text for one ticket: its entities, summary, and
    best evidence line -- the same fields the old Stage A prompt showed the
    model, just handed to an embedding model instead of a chat model."""
    entities = " ".join(sorted(e.replace("_", " ") for e in d.entities))
    summary = (d.summary or "").strip()
    evidence = best_evidence(d).strip()
    text = ". ".join(part for part in (entities, summary, evidence) if part)
    return text[:_DIGEST_TEXT_CHARS]


def _window_cutoff_iso(now: datetime, window_days: int) -> str:
    return _date_only((now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _unique_digests(candidates: list) -> list:
    """Every digest across all candidates, deduped by ticket id -- a ticket
    can appear in more than one seed candidate before Stage A runs."""
    seen: set = set()
    out = []
    for c in candidates:
        for d in c.digests:
            if d.id not in seen:
                seen.add(d.id)
                out.append(d)
    return out


def _ensure_vectors(digests: list, store: VectorStore, *, model: str) -> dict:
    """Returns {ticket_id: vector}. Only embeds tickets missing from
    `store`; every newly-embedded vector is cached before returning so the
    next cycle reuses it instead of re-embedding an unchanged ticket."""
    cached: dict = {}
    missing = []
    for d in digests:
        entry = store.get(str(d.id))
        if entry is not None:
            cached[d.id] = entry["vector"]
        else:
            missing.append(d)

    if missing:
        vectors = embed_texts([_digest_embed_text(d) for d in missing], model=model)
        for d, vector in zip(missing, vectors):
            store.upsert(str(d.id), vector, {"timestamp": _date_only(d.date)})
            cached[d.id] = vector

    return cached


def _candidate_vector(candidate, vectors_by_ticket: dict) -> list:
    """Centroid (mean) of the candidate's member ticket vectors. A simple
    average is enough here -- cluster_by_similarity only needs a consistent,
    deterministic ordering of pairwise scores, not a precise geometric
    center."""
    members = [vectors_by_ticket[d.id] for d in candidate.digests if d.id in vectors_by_ticket]
    if not members:
        return []
    dims = len(members[0])
    return [sum(v[i] for v in members) / len(members) for i in range(dims)]


def embed_and_cluster(
    candidates: list,
    *,
    now: datetime,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    model: str = DEFAULT_EMBEDDING_MODEL,
    window_days: int = 14,
    store: Optional[VectorStore] = None,
) -> list:
    """Returns groups of candidate indices (see
    trend_vectors.cluster_by_similarity) -- candidates whose ticket text
    embeds close together by cosine similarity describe the same
    underlying issue and merge into one group.

    A single candidate skips embedding entirely: there is no merge decision
    to make, so the overwhelmingly common one-candidate case never touches
    the network. Any EmbeddingError from cron/trend_vectors.py propagates
    uncaught -- a broken embedding endpoint must look like a broken
    pipeline (loud failure), never like "no trends found" (silent, and
    indistinguishable from a genuinely quiet cycle).
    """
    if len(candidates) <= 1:
        return [[i] for i in range(len(candidates))]

    store = store or VectorStore()
    digests = _unique_digests(candidates)
    vectors_by_ticket = _ensure_vectors(digests, store, model=model)
    candidate_vectors = [_candidate_vector(c, vectors_by_ticket) for c in candidates]

    pruned = store.prune_before(_window_cutoff_iso(now, window_days))
    if pruned:
        logger.info(
            "trend_cluster_embed: pruned %d stale ticket embedding(s) outside the %dd window",
            pruned,
            window_days,
        )

    return cluster_by_similarity(candidates, candidate_vectors, threshold=threshold)
