"""
Unit tests for cron/trend_vectors.py.

All embedding HTTP calls are mocked -- no network access, and no
dependency on `ollama pull bge-m3` having finished (see the module
docstring's TRAP note in cron/trend_vectors.py for the two response
shapes this covers).
"""
import json
from unittest.mock import patch

import pytest

from cron.trend_vectors import (
    EmbeddingError,
    VectorStore,
    cluster_by_similarity,
    cosine_similarity,
    embed_texts,
)


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors_is_one(self):
        assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_is_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector_returns_zero_not_exception(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0
        assert cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0
        assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# cluster_by_similarity
# ---------------------------------------------------------------------------

class TestClusterBySimilarity:
    def test_near_vectors_group_distant_stays_separate(self):
        items = [{"id": 0}, {"id": 1}, {"id": 2}]
        vectors = [
            [1.0, 0.0],
            [0.99, 0.01],  # near vector 0 -- same cluster
            [0.0, 1.0],  # orthogonal -- own cluster
        ]
        groups = cluster_by_similarity(items, vectors, threshold=0.9)
        assert [0, 1] in groups
        assert [2] in groups
        assert len(groups) == 2

    def test_deterministic_across_repeated_calls(self):
        items = [{"id": i} for i in range(5)]
        vectors = [
            [1.0, 0.0],
            [0.98, 0.02],
            [0.0, 1.0],
            [0.01, 0.99],
            [0.97, 0.03],
        ]
        first = cluster_by_similarity(items, vectors, threshold=0.9)
        second = cluster_by_similarity(items, vectors, threshold=0.9)
        assert first == second

    def test_singletons_returned_as_one_element_groups(self):
        items = [{"id": 0}]
        vectors = [[1.0, 0.0]]
        assert cluster_by_similarity(items, vectors, threshold=0.9) == [[0]]


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------

class TestVectorStore:
    def test_upsert_get_round_trip(self, tmp_path):
        store = VectorStore(tmp_path / "vectors.json")
        store.upsert("k1", [1.0, 2.0], {"timestamp": "2026-08-01T00:00:00"})
        entry = store.get("k1")
        assert entry["vector"] == [1.0, 2.0]
        assert entry["metadata"]["timestamp"] == "2026-08-01T00:00:00"

    def test_get_missing_key_returns_none(self, tmp_path):
        store = VectorStore(tmp_path / "vectors.json")
        assert store.get("nope") is None

    def test_atomic_write_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / "vectors.json"
        store = VectorStore(path)
        store.upsert("k1", [1.0], {"timestamp": "2026-08-01T00:00:00"})
        assert path.exists()
        assert not path.with_suffix(".json.tmp").exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_corrupt_store_file_starts_empty(self, tmp_path):
        path = tmp_path / "vectors.json"
        path.write_text("{not valid json", encoding="utf-8")
        store = VectorStore(path)
        assert store.all_entries() == []

    def test_missing_store_file_starts_empty(self, tmp_path):
        store = VectorStore(tmp_path / "does_not_exist.json")
        assert store.all_entries() == []

    def test_prune_before_drops_only_old_entries_and_returns_count(self, tmp_path):
        store = VectorStore(tmp_path / "vectors.json")
        store.upsert("old", [1.0], {"timestamp": "2026-07-01T00:00:00"})
        store.upsert("new", [2.0], {"timestamp": "2026-08-15T00:00:00"})
        removed = store.prune_before("2026-08-01T00:00:00")
        assert removed == 1
        assert store.get("old") is None
        assert store.get("new") is not None

    def test_prune_before_drops_entries_missing_timestamp(self, tmp_path):
        store = VectorStore(tmp_path / "vectors.json")
        store.upsert("no_ts", [1.0], {})
        removed = store.prune_before("2026-08-01T00:00:00")
        assert removed == 1

    def test_nearest_respects_threshold_and_ordering(self, tmp_path):
        store = VectorStore(tmp_path / "vectors.json")
        store.upsert("a", [1.0, 0.0], {"timestamp": "2026-08-01T00:00:00"})
        store.upsert("b", [0.99, 0.01], {"timestamp": "2026-08-01T00:00:00"})
        store.upsert("c", [0.0, 1.0], {"timestamp": "2026-08-01T00:00:00"})
        results = store.nearest([1.0, 0.0], threshold=0.9, limit=50)
        keys = [key for key, _ in results]
        assert keys[0] == "a"
        assert "b" in keys
        assert "c" not in keys
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_nearest_respects_limit(self, tmp_path):
        store = VectorStore(tmp_path / "vectors.json")
        for i in range(5):
            store.upsert(f"k{i}", [1.0, 0.0], {"timestamp": "2026-08-01T00:00:00"})
        results = store.nearest([1.0, 0.0], threshold=0.0, limit=2)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# embed_texts
# ---------------------------------------------------------------------------

def _fake_response(payload: dict):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *exc):
            return False

        def read(self_inner):
            return json.dumps(payload).encode("utf-8")

    return _Resp()


class TestEmbedTexts:
    def test_empty_input_returns_empty_list_no_network(self):
        with patch("cron.trend_vectors.urllib.request.urlopen") as mock_urlopen:
            assert embed_texts([]) == []
            mock_urlopen.assert_not_called()

    def test_modern_batch_shape_success(self):
        payload = {"model": "bge-m3", "embeddings": [[0.1, 0.2], [0.3, 0.4]]}
        with patch("cron.trend_vectors.urllib.request.urlopen", return_value=_fake_response(payload)) as mock_urlopen:
            result = embed_texts(["a", "b"])
        assert result == [[0.1, 0.2], [0.3, 0.4]]
        called_url = mock_urlopen.call_args[0][0].full_url
        assert called_url.endswith("/api/embed")

    def test_legacy_single_shape_fallback_on_missing_endpoint(self):
        import urllib.error

        def fake_urlopen(request, timeout=None):
            if request.full_url.endswith("/api/embed"):
                raise urllib.error.HTTPError(request.full_url, 404, "not found", None, None)
            return _fake_response({"embedding": [0.5, 0.6]})

        with patch("cron.trend_vectors.urllib.request.urlopen", side_effect=fake_urlopen):
            result = embed_texts(["only one"])
        assert result == [[0.5, 0.6]]

    def test_raises_on_http_error(self):
        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "server error", None, None)

        with patch("cron.trend_vectors.urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(EmbeddingError):
                embed_texts(["boom"])

    def test_raises_on_malformed_json(self):
        class _BadResp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return b"not json at all"

        with patch("cron.trend_vectors.urllib.request.urlopen", return_value=_BadResp()):
            with pytest.raises(EmbeddingError):
                embed_texts(["boom"])

    def test_raises_on_unexpected_shape(self):
        payload = {"model": "bge-m3", "unexpected": True}
        with patch("cron.trend_vectors.urllib.request.urlopen", return_value=_fake_response(payload)):
            with pytest.raises(EmbeddingError):
                embed_texts(["a"])


# ---------------------------------------------------------------------------
# embed_texts -- batching (production-scale corpora exceed one HTTP request)
# ---------------------------------------------------------------------------

def _batch_response_for(request):
    """Fake /api/embed response: one distinguishable vector per input text,
    keyed off the text's own index-marker so stitching bugs are visible."""
    sent = json.loads(request.data.decode("utf-8"))
    vectors = [[float(text.split(":")[0])] for text in sent["input"]]
    return _fake_response({"model": "bge-m3", "embeddings": vectors})


class TestEmbedTextsBatching:
    def test_batch_larger_than_batch_size_splits_into_right_number_of_requests(self):
        texts = [f"{i}:text" for i in range(5)]
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            return _batch_response_for(request)

        with patch("cron.trend_vectors.urllib.request.urlopen", side_effect=fake_urlopen):
            embed_texts(texts, batch_size=2)

        # 5 texts at batch_size=2 -> batches of 2, 2, 1
        assert len(calls) == 3
        sizes = [len(json.loads(c.data.decode("utf-8"))["input"]) for c in calls]
        assert sizes == [2, 2, 1]

    def test_results_stitched_back_in_input_order(self):
        texts = [f"{i}:text" for i in range(7)]

        def fake_urlopen(request, timeout=None):
            return _batch_response_for(request)

        with patch("cron.trend_vectors.urllib.request.urlopen", side_effect=fake_urlopen):
            result = embed_texts(texts, batch_size=3)

        # Each vector's value is the index encoded in its source text, so
        # any reordering across batch boundaries shows up as a mismatch.
        assert result == [[float(i)] for i in range(7)]

    def test_batch_size_and_timeout_are_configurable_and_honored(self):
        texts = [f"{i}:text" for i in range(4)]
        seen_timeouts = []

        def fake_urlopen(request, timeout=None):
            seen_timeouts.append(timeout)
            return _batch_response_for(request)

        with patch("cron.trend_vectors.urllib.request.urlopen", side_effect=fake_urlopen):
            embed_texts(texts, batch_size=1, timeout=17.5)

        assert len(seen_timeouts) == 4
        assert all(t == 17.5 for t in seen_timeouts)

    def test_transient_failure_on_one_batch_is_retried_and_succeeds(self):
        texts = ["0:text", "1:text"]
        attempts = {"count": 0}

        def fake_urlopen(request, timeout=None):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TimeoutError("timed out")
            return _batch_response_for(request)

        with patch("cron.trend_vectors.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("cron.trend_vectors.time.sleep") as mock_sleep:
            result = embed_texts(texts, batch_size=2)

        assert result == [[0.0], [1.0]]
        assert attempts["count"] == 2
        mock_sleep.assert_called_once()

    def test_persistent_failure_raises_naming_the_batch_no_partial_results(self):
        texts = [f"{i}:text" for i in range(5)]

        def fake_urlopen(request, timeout=None):
            raise TimeoutError("timed out")

        with patch("cron.trend_vectors.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("cron.trend_vectors.time.sleep"):
            with pytest.raises(EmbeddingError) as exc_info:
                embed_texts(texts, batch_size=2)

        # Names which batch failed (1 of 3, given batch_size=2 over 5 texts).
        assert "batch 1/3" in str(exc_info.value)

    def test_total_budget_exhausted_aborts_before_next_batch(self):
        # Regression for the 2026-08-24 incident: a trend_pass run never
        # completed and never raised in over 2.5 hours because each
        # individual batch call stayed just inside its own per-call
        # timeout. total_budget_seconds bounds the WHOLE call so a slow
        # endpoint fails loud instead of leaking a thread forever.
        texts = [f"{i}:text" for i in range(6)]  # 3 batches at batch_size=2
        calls = []
        # Monotonic clock: first read (deadline calc) at t=0, then one
        # read per batch-start check. Budget of 5s is exhausted right
        # after the first batch completes (fake time jumps to 6s).
        clock = iter([0.0, 0.0, 6.0])

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            return _batch_response_for(request)

        with patch("cron.trend_vectors.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("cron.trend_vectors.time.monotonic", side_effect=lambda: next(clock)):
            with pytest.raises(EmbeddingError, match="budget exhausted"):
                embed_texts(texts, batch_size=2, total_budget_seconds=5.0)

        # Only the first batch's request went out before the abort.
        assert len(calls) == 1

    def test_total_budget_not_exhausted_completes_normally(self):
        texts = [f"{i}:text" for i in range(4)]

        def fake_urlopen(request, timeout=None):
            return _batch_response_for(request)

        with patch("cron.trend_vectors.urllib.request.urlopen", side_effect=fake_urlopen):
            result = embed_texts(texts, batch_size=2, total_budget_seconds=600.0)

        assert result == [[float(i)] for i in range(4)]

    def test_empty_input_makes_zero_http_calls(self):
        with patch("cron.trend_vectors.urllib.request.urlopen") as mock_urlopen:
            assert embed_texts([], batch_size=2) == []
            mock_urlopen.assert_not_called()
