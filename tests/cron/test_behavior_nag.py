#!/usr/bin/env python3
"""Tests for cron/behavior_nag.py -- the stale-pending-proposal prompt nag."""
import pytest

from cron import behavior_nag as nag
from cron import behavior_store as store

JARVIS = "6ff84f43-2fac-4366-926d-382cb712deae"

_BANNED_WORDS = ("rule", "behavior_store", "pending", "approval", "cron", "sweep", "scheduled",
                 "automated", "job")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "behavior.db"


def test_render_empty_when_nothing_stale(db_path):
    assert nag.render_stale_pending_nag(db_path=db_path) == ""


def test_render_nonempty_paraphrases_without_banned_vocabulary(db_path):
    store.propose(
        "instruction",
        "When a Teams message calls someone an 'end user', treat it as a ConnectWise "
        "ticket-contact question before replying.",
        scope="ops-review", requested_by="ops-memory-nightly-review",
        now="2026-09-18T20:44:14+00:00", db_path=db_path,
    )
    block = nag.render_stale_pending_nag(
        older_than_hours=24, now="2026-09-21T00:00:00+00:00", db_path=db_path,
    )
    assert block != ""
    assert "ops-review" in block
    lowered = block.lower()
    for word in _BANNED_WORDS:
        assert word not in lowered, f"banned word {word!r} leaked into nag text: {block!r}"


def test_render_never_exposes_numeric_rule_id(db_path):
    proposal = store.propose(
        "instruction", "still waiting on someone", scope="triage", requested_by=JARVIS,
        now="2026-09-01T00:00:00+00:00", db_path=db_path,
    )
    block = nag.render_stale_pending_nag(
        older_than_hours=24, now="2026-09-05T00:00:00+00:00", db_path=db_path,
    )
    assert str(proposal["id"]) not in block


def test_render_caps_items_and_accepts_prefetched_list(db_path):
    for i in range(5):
        store.propose(
            "instruction", f"open ask number {i}", scope="triage", requested_by=JARVIS,
            now="2026-09-01T00:00:00+00:00", db_path=db_path,
        )
    prefetched = store.list_stale_pending(
        older_than_hours=24, now="2026-09-05T00:00:00+00:00", db_path=db_path,
    )
    assert len(prefetched) == 5
    block = nag.render_stale_pending_nag(prefetched)
    assert block.count("- still waiting") <= 3
