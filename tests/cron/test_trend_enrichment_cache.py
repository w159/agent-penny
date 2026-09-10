"""Unit tests for cron/trend_enrichment_cache.py."""
from __future__ import annotations

from cron.trend_enrichment_cache import EnrichmentCache


def test_missing_ids_returns_all_ids_when_cache_empty(tmp_path):
    cache = EnrichmentCache(tmp_path / "cache.json")
    assert cache.missing_ids("notes", [1, 2, 3]) == [1, 2, 3]


def test_update_then_missing_ids_excludes_cached(tmp_path):
    cache = EnrichmentCache(tmp_path / "cache.json")
    cache.update("notes", {1: [{"text": "a"}]})
    assert cache.missing_ids("notes", [1, 2]) == [2]
    assert cache.get_all("notes") == {1: [{"text": "a"}]}


def test_second_pass_does_not_refetch_already_enriched_tickets(tmp_path):
    """The resumability requirement: save/reload roundtrip via a fresh
    EnrichmentCache instance still reports the ticket as cached."""
    path = tmp_path / "cache.json"
    first = EnrichmentCache(path)
    first.update("notes", {1: [{"text": "a"}]})
    first.update("configurations", {1: [{"name": "GWH-1"}]})
    first.save()

    second = EnrichmentCache(path)
    assert second.missing_ids("notes", [1, 2]) == [2]
    assert second.missing_ids("configurations", [1, 2]) == [2]
    assert second.get_all("configurations") == {1: [{"name": "GWH-1"}]}


def test_corrupt_cache_file_logged_and_starts_empty(tmp_path, caplog):
    path = tmp_path / "cache.json"
    path.write_text("{not valid json")
    with caplog.at_level("ERROR"):
        cache = EnrichmentCache(path)
    assert cache.missing_ids("notes", [1]) == [1]
    assert any("failed to load" in r.message for r in caplog.records)


def test_kinds_are_independent(tmp_path):
    cache = EnrichmentCache(tmp_path / "cache.json")
    cache.update("notes", {1: ["note"]})
    assert cache.missing_ids("configurations", [1]) == [1]
    assert cache.missing_ids("notes", [1]) == []


def test_ci_kind_persists_across_a_save_reload_roundtrip(tmp_path):
    """The "ci" kind is keyed by ConnectWise configuration-item id, not
    ticket id, but persists the same way as notes/configurations."""
    path = tmp_path / "cache.json"
    first = EnrichmentCache(path)
    first.update("ci", {1420: {"type": {"name": "Laptop"}}})
    first.save()

    second = EnrichmentCache(path)
    assert second.missing_ids("ci", [1420, 1169]) == [1169]
    assert second.get_all("ci") == {1420: {"type": {"name": "Laptop"}}}
