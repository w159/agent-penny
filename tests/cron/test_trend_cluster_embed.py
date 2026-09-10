#!/usr/bin/env python3
"""Tests for cron/trend_cluster_embed.py - the embedding-based Stage A merge
decision that replaced the old LLM merge prompt. Network is always mocked
(see tests/cron/conftest.py's autouse _fake_embeddings fixture)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cron.trend_cluster import Candidate
from cron.trend_cluster_embed import embed_and_cluster
from cron.trend_corpus import TicketDigest
from cron.trend_vectors import EmbeddingError, VectorStore

NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def _digest(id, date="2026-08-15", entities=None, summary="issue"):
    return TicketDigest(
        id=id,
        date=date,
        board="Triage",
        summary=summary,
        contact="Jane Smith",
        status="Open",
        priority="Priority 3 - Medium",
        issue=summary,
        resolution="",
        tech_notes=[],
        techs=[],
        devices=[],
        entities=set(entities or set()),
        is_automated=False,
    )


def _candidate(*digests, entities=None) -> Candidate:
    return Candidate(entities=set(entities or set()), digests=list(digests))


def test_near_duplicate_wording_merges_into_one_group():
    a = _candidate(_digest(1, summary="printer offline in accounting"), entities={"printer"})
    b = _candidate(_digest(2, summary="accounting floor printer will not print"), entities={"printer"})
    groups = embed_and_cluster([a, b], now=NOW, threshold=0.1)
    assert len(groups) == 1
    assert sorted(groups[0]) == [0, 1]


def test_distant_candidate_stays_separate():
    a = _candidate(_digest(1, summary="printer offline in accounting"), entities={"printer"})
    b = _candidate(_digest(2, summary="cannot connect to vpn from home"), entities={"vpn"})
    groups = embed_and_cluster([a, b], now=NOW, threshold=0.5)
    indices = sorted(tuple(sorted(g)) for g in groups)
    assert indices == [(0,), (1,)]


def test_identical_input_twice_yields_identical_grouping():
    a = _candidate(_digest(1, summary="printer offline"), entities={"printer"})
    b = _candidate(_digest(2, summary="printer will not print"), entities={"printer"})
    c = _candidate(_digest(3, summary="vpn connection drops"), entities={"vpn"})
    first = embed_and_cluster([a, b, c], now=NOW, threshold=0.4, store=VectorStore(path=None))
    second = embed_and_cluster([a, b, c], now=NOW, threshold=0.4, store=VectorStore(path=None))
    assert first == second


def test_threshold_is_honored_and_configurable():
    a = _candidate(_digest(1, summary="printer offline in accounting"), entities={"printer"})
    b = _candidate(_digest(2, summary="accounting floor printer will not print"), entities={"printer"})
    low = embed_and_cluster([a, b], now=NOW, threshold=0.05)
    high = embed_and_cluster([a, b], now=NOW, threshold=0.99)
    assert len(low) == 1
    assert len(high) == 2


def test_embedding_endpoint_failure_raises(monkeypatch):
    import cron.trend_cluster_embed as trend_cluster_embed

    def boom(texts, *, model="bge-m3", **_kwargs):
        raise EmbeddingError("connection refused")

    monkeypatch.setattr(trend_cluster_embed, "embed_texts", boom)
    a = _candidate(_digest(1, summary="printer offline"), entities={"printer"})
    b = _candidate(_digest(2, summary="vpn dropped"), entities={"vpn"})
    with pytest.raises(EmbeddingError):
        embed_and_cluster([a, b], now=NOW, threshold=0.5)


def test_cached_embeddings_are_reused_on_second_pass(monkeypatch, tmp_path):
    import cron.trend_cluster_embed as trend_cluster_embed

    calls = []
    real = trend_cluster_embed.embed_texts

    def counting(texts, *, model="bge-m3", **kwargs):
        calls.append(list(texts))
        return real(texts, model=model, **kwargs)

    monkeypatch.setattr(trend_cluster_embed, "embed_texts", counting)

    store = VectorStore(path=tmp_path / "vectors.json")
    a = _candidate(_digest(1, summary="printer offline"), entities={"printer"})
    b = _candidate(_digest(2, summary="vpn dropped"), entities={"vpn"})
    embed_and_cluster([a, b], now=NOW, threshold=0.5, store=store)
    assert len(calls) == 1
    assert len(calls[0]) == 2

    c = _candidate(_digest(3, summary="new ticket text"), entities={"other"})
    embed_and_cluster([a, b, c], now=NOW, threshold=0.5, store=store)
    # Only the new ticket (id 3) should be embedded on the second pass -
    # tickets 1 and 2 are already cached.
    assert len(calls) == 2
    # _digest_embed_text reads the TicketDigest's own entities, not the
    # wrapping Candidate's - this fixture only set the latter, so no
    # entity prefix is expected here.
    assert calls[1] == ["new ticket text. new ticket text"]


def test_prune_before_is_invoked_with_the_window_cutoff(monkeypatch, tmp_path):
    store = VectorStore(path=tmp_path / "vectors.json")
    calls = []
    real_prune = store.prune_before
    monkeypatch.setattr(store, "prune_before", lambda cutoff_iso: (calls.append(cutoff_iso), real_prune(cutoff_iso))[1])

    a = _candidate(_digest(1, summary="printer offline"), entities={"printer"})
    b = _candidate(_digest(2, summary="vpn dropped"), entities={"vpn"})
    embed_and_cluster([a, b], now=NOW, threshold=0.5, window_days=14, store=store)

    assert calls == ["2026-08-05"]


def test_single_candidate_never_calls_embed_texts(monkeypatch):
    import cron.trend_cluster_embed as trend_cluster_embed

    def boom(texts, *, model="bge-m3", **_kwargs):
        raise AssertionError("embed_texts must not be called for a single candidate")

    monkeypatch.setattr(trend_cluster_embed, "embed_texts", boom)
    a = _candidate(_digest(1, summary="printer offline"), entities={"printer"})
    groups = embed_and_cluster([a], now=NOW, threshold=0.5)
    assert groups == [[0]]
