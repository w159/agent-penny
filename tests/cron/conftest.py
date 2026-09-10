"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.
"""

import hashlib

import pytest

# Fixed dimensionality for the fake hashing-trick embedding below. Any test
# corpus produces vectors of this width regardless of vocabulary size, so
# cosine_similarity never sees a dimension mismatch across separate calls.
_FAKE_EMBED_DIMS = 64


def _fake_embed_texts(texts, *, model="bge-m3", **_kwargs):
    """Deterministic, network-free stand-in for cron/trend_vectors.py's
    embed_texts(): a hashing-trick bag-of-words vector per text. Same words
    land in the same buckets (high cosine similarity); disjoint vocabulary
    lands in disjoint buckets (low similarity) - close enough to a real
    embedding model's behavior for cron/trend_cluster_embed.py's merge
    tests without ever touching the network."""
    vectors = []
    for text in texts:
        vector = [0.0] * _FAKE_EMBED_DIMS
        for word in text.lower().split():
            bucket = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16) % _FAKE_EMBED_DIMS
            vector[bucket] += 1.0
        vectors.append(vector)
    return vectors


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch, tmp_path):
    """No cron test may hit a live embedding endpoint. Stubs
    cron/trend_cluster_embed.py's embed_texts with the deterministic fake
    above and points the vector cache at a per-test tmp file so tests never
    share or persist real embedding state (mirrors the pattern of the
    HERMES_MODEL fixture below - hermetic by default, opt out per test)."""
    import cron.trend_cluster_embed as trend_cluster_embed
    import cron.trend_vectors as trend_vectors

    monkeypatch.setattr(trend_cluster_embed, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(trend_vectors, "VECTOR_STORE_FILE", tmp_path / "trend_vectors.json")
    yield


@pytest.fixture()
def make_cron_provider():
    """Factory for minimal CronScheduler test doubles.

    ``make_cron_provider(register_job=...)`` returns a real ``CronScheduler``
    subclass instance whose ``register_job`` is the given callable — so tests
    exercising the creation-registration contract share one stub instead of
    redefining inline spy/failing classes, and an ABC rename breaks them
    loudly instead of silently passing a duck-type.
    """
    from cron.scheduler_provider import CronScheduler

    def _make(register_job=None, name="stub"):
        class _StubProvider(CronScheduler):
            @property
            def name(self):  # pragma: no cover - trivial
                return name

            def start(self, stop_event, **kw):  # pragma: no cover - unused
                pass

            def register_job(self, job):
                if register_job is not None:
                    return register_job(job)
                return None

        return _StubProvider()

    return _make


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield


@pytest.fixture(autouse=True)
def _reset_session_context_vars():
    """Restore session ContextVars around cron tests that call run_job directly.

    Production confines each cron run to a copied context, but direct unit tests
    share the pytest context. ``run_job`` intentionally clears ordinary session
    variables to explicit empty values, which would otherwise shadow legacy env
    fallbacks used by later approval tests in the same process.
    """
    from gateway.session_context import _UNSET, _VAR_MAP

    def _reset_all():
        for var in _VAR_MAP.values():
            var.set(_UNSET)

    _reset_all()
    yield
    _reset_all()
