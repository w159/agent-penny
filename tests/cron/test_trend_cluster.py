#!/usr/bin/env python3
"""Tests for cron/trend_cluster.py - the deterministic + semantic clustering
engine that turns TicketDigests into thresholded trends."""
from __future__ import annotations

from datetime import datetime, timezone

from cron.trend_corpus import TicketDigest
from cron.trend_cluster import Candidate, _is_promoted, build_candidates, detect_trends, score_candidate

NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


def _noise(n=120, start=50000):
    """Padding so a small test corpus doesn't trip the >10%-of-corpus
    generic-entity filter or the >10%-of-corpus MAX_CANDIDATE_SHARE
    promotion gate (cron/trend_cluster.py), both tuned for corpora in the
    hundreds/thousands of tickets, not a bare handful of fixture tickets.
    120 keeps a 6-ticket real trend candidate at ~5% of a ~126-ticket
    corpus, comfortably under the 10% bar with margin for other fixtures
    in the same test to add a few more tickets."""
    return [
        _digest(start + i, contact=f"Noise {i}", entities={f"noise_entity_{i}"})
        for i in range(n)
    ]


def _digest(
    id,
    date="2026-08-15",
    contact="Jane Smith",
    devices=None,
    entities=None,
    is_automated=False,
    summary="issue",
    tech_notes=None,
):
    return TicketDigest(
        id=id,
        date=date,
        board="Triage",
        summary=summary,
        contact=contact,
        status="Open",
        priority="Priority 3 - Medium",
        issue=summary,
        resolution="",
        tech_notes=tech_notes or [],
        techs=[],
        devices=devices or [],
        entities=set(entities or {"bitlocker_recovery"}),
        is_automated=is_automated,
    )


# ---------------------------------------------------------------------------
# Threshold enforcement (Stage 2)
# ---------------------------------------------------------------------------

def test_three_tickets_one_contact_no_devices_does_not_promote():
    digests = [_digest(i, contact="Jane Smith") for i in range(100, 103)]
    trends = detect_trends(digests, now=NOW, model_call=None)
    assert trends == []


def test_three_tickets_two_contacts_promotes():
    digests = [
        _digest(100, contact="Jane Smith"),
        _digest(101, contact="Jane Smith"),
        _digest(102, contact="John Doe"),
    ] + _noise()
    trends = detect_trends(digests, now=NOW, model_call=None)
    assert len(trends) == 1
    assert trends[0]["ticket_count"] == 3
    assert trends[0]["user_count"] == 2


def test_two_tickets_never_promotes_regardless_of_overlap():
    digests = [
        _digest(100, contact="Jane Smith", entities={"bitlocker_recovery", "startup_repair"}),
        _digest(101, contact="John Doe", entities={"bitlocker_recovery", "startup_repair"}),
    ]
    trends = detect_trends(digests, now=NOW, model_call=None)
    assert trends == []


def test_over_generic_entity_does_not_seed_a_candidate():
    # "outlook" appears in every one of 8 tickets (>25% of corpus) so it
    # must not seed a candidate on its own, even though every ticket
    # technically shares it.
    digests = []
    for i in range(8):
        digests.append(
            _digest(
                200 + i,
                contact=f"User {i}",
                entities={"outlook"},
            )
        )
    candidates = build_candidates(digests)
    assert candidates == []


def test_candidate_over_max_share_of_corpus_is_not_promoted():
    # Live 2026-08-28 pull: a "defender" candidate at 285/1459 = 19.5% of
    # corpus spanned the full window uniformly (background AV-alert noise,
    # not a dated outage) and cleared the old MIN_TICKETS/MIN_DISTINCT_
    # SUBJECTS gate outright. A candidate this large a share of the corpus
    # must fail promotion even though it clears the ticket/subject floor.
    c = Candidate(
        entities={"defender"},
        digests=[_digest(400 + i, contact=f"User {i}") for i in range(30)],
    )
    assert _is_promoted(c)  # no corpus_size given -> old behavior, still promotes
    assert not _is_promoted(c, corpus_size=100)  # 30/100 = 30% > MAX_CANDIDATE_SHARE


def test_candidate_under_max_share_of_corpus_still_promotes():
    c = Candidate(
        entities={"bitlocker_recovery"},
        digests=[_digest(500 + i, contact=f"User {i}") for i in range(3)],
    )
    assert _is_promoted(c, corpus_size=1000)  # 3/1000 = 0.3%, well under the bar


def test_detect_trends_drops_a_candidate_spanning_most_of_the_corpus():
    # End-to-end: an oversized generic-entity-adjacent cluster (individual
    # entities each under the per-entity generic bar, but the merged
    # candidate itself dominates the corpus) must not reach finalize_trend.
    huge_cluster = [
        _digest(600 + i, contact=f"User {i}", entities={"office_365", "outlook"})
        for i in range(40)
    ]
    real_trend = [
        _digest(700 + i, contact=f"Contact {i}", entities={"bitlocker_recovery"})
        for i in range(4)
    ]
    digests = huge_cluster + real_trend + _noise(n=10, start=80000)
    trends = detect_trends(digests, now=NOW, model_call=None)
    # The oversized cluster (40 tickets, >10% of a ~54-ticket corpus) must
    # not appear; the small real trend still does.
    ticket_id_sets = [{tk["id"] for tk in t["tickets"]} for t in trends]
    assert not any(len(ids) >= 40 for ids in ticket_id_sets)
    assert any({700, 701, 702, 703} <= ids for ids in ticket_id_sets)


# ---------------------------------------------------------------------------
# Merge behavior
# ---------------------------------------------------------------------------

def test_overlapping_candidates_merge_into_one():
    # bitlocker_recovery and startup_repair each seed their own candidate,
    # but cover nearly the same ticket set, so they should collapse.
    digests = [
        _digest(300, contact="A", entities={"bitlocker_recovery"}),
        _digest(301, contact="B", entities={"bitlocker_recovery", "startup_repair"}),
        _digest(302, contact="C", entities={"bitlocker_recovery", "startup_repair"}),
        _digest(304, contact="E", entities={"bitlocker_recovery", "startup_repair"}),
        _digest(303, contact="D", entities={"startup_repair"}),
    ] + _noise()
    candidates = build_candidates(digests)
    candidates = [c for c in candidates if c.entities & {"bitlocker_recovery", "startup_repair"}]
    assert len(candidates) == 1
    assert candidates[0].entities == {"bitlocker_recovery", "startup_repair"}
    assert candidates[0].ticket_ids == {300, 301, 302, 303, 304}


# ---------------------------------------------------------------------------
# trend_id stability
# ---------------------------------------------------------------------------

def _base_trend_digests(n=6, start=90000, with_noise=True):
    digests = [
        _digest(start + i, contact=f"User {i % 3}", date="2026-08-10")
        for i in range(n)
    ]
    if with_noise:
        digests += _noise()
    return digests


def test_trend_id_stable_regardless_of_input_order():
    digests = _base_trend_digests()
    trends_a = detect_trends(digests, now=NOW, model_call=None)
    trends_b = detect_trends(list(reversed(digests)), now=NOW, model_call=None)
    assert trends_a[0]["trend_id"] == trends_b[0]["trend_id"]


def test_trend_id_stable_when_one_ticket_added():
    digests = _base_trend_digests(n=6, start=90000)
    trends_before = detect_trends(digests, now=NOW, model_call=None)

    grown = digests + [_digest(90006, contact="User 9", date="2026-08-16")]
    trends_after = detect_trends(grown, now=NOW, model_call=None)

    assert trends_before[0]["trend_id"] == trends_after[0]["trend_id"]
    assert trends_after[0]["ticket_count"] == 7


def test_trend_id_differs_for_a_different_ticket_set():
    digests_a = _base_trend_digests(n=6, start=90000)
    digests_b = [
        _digest(70000 + i, contact=f"User {i % 3}", date="2026-08-10", entities={"vpn"})
        for i in range(6)
    ] + _noise(start=60000)
    trend_a = detect_trends(digests_a, now=NOW, model_call=None)[0]
    trend_b = detect_trends(digests_b, now=NOW, model_call=None)[0]
    assert trend_a["trend_id"] != trend_b["trend_id"]


# ---------------------------------------------------------------------------
# Fallback contract
# ---------------------------------------------------------------------------

def test_fallback_when_model_call_is_none():
    digests = _base_trend_digests()
    trends = detect_trends(digests, now=NOW, model_call=None)
    assert len(trends) == 1
    assert trends[0]["title"]
    assert "bitlocker" in trends[0]["title"].lower() or trends[0]["title"]


def test_fallback_when_model_call_raises():
    def boom(system, user):
        raise RuntimeError("provider down")

    digests = _base_trend_digests()
    trends = detect_trends(digests, now=NOW, model_call=boom)
    assert len(trends) == 1
    assert trends[0]["title"]


def test_fallback_when_model_returns_garbage():
    def garbage(system, user):
        return "not json at all, sorry"

    digests = _base_trend_digests()
    trends = detect_trends(digests, now=NOW, model_call=garbage)
    assert len(trends) == 1
    assert trends[0]["title"]


def test_markdown_fenced_json_parses():
    # Stage A (embedding merge) has no prompt to fence; this exercises
    # Stage B narration's tolerance of a markdown-fenced JSON response.
    digests = _base_trend_digests()

    def fenced(system, user):
        return (
            "Here is my analysis:\n"
            "```json\n"
            '{"title": "Windows Update Recovery Loop", '
            '"why_related": "Same failure pattern.", '
            '"recommended_action": "Escalate to a senior tech."}\n'
            "```\n"
            "Let me know if you need anything else."
        )

    trends = detect_trends(digests, now=NOW, model_call=fenced)
    assert len(trends) == 1
    assert trends[0]["title"] == "Windows Update Recovery Loop"


# ---------------------------------------------------------------------------
# The model cannot invent counts or promote below-threshold clusters
# ---------------------------------------------------------------------------

def test_model_cannot_invent_counts():
    digests = _base_trend_digests(n=6, start=90000)

    def lying(system, user):
        return (
            '{"title": "Fake Big Trend", "why_related": "x", '
            '"recommended_action": "y", "ticket_count": 500, '
            '"device_count": 500, "user_count": 500}'
        )

    trends = detect_trends(digests, now=NOW, model_call=lying)
    assert len(trends) == 1
    assert trends[0]["ticket_count"] == 6
    assert trends[0]["title"] == "Fake Big Trend"


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def test_confidence_high_when_spans_automated_and_human():
    digests = [
        _digest(90100, contact="User A", is_automated=False),
        _digest(90101, contact="User B", is_automated=False),
        _digest(90102, contact="auvik-monitor@system", is_automated=True),
    ] + _noise()
    trends = detect_trends(digests, now=NOW, model_call=None)
    assert len(trends) == 1
    assert trends[0]["confidence"] == "high"


def test_confidence_medium_for_a_small_human_only_cluster():
    digests = [
        _digest(90200, contact="User A", is_automated=False),
        _digest(90201, contact="User B", is_automated=False),
        _digest(90202, contact="User C", is_automated=False),
    ] + _noise()
    trends = detect_trends(digests, now=NOW, model_call=None)
    assert len(trends) == 1
    assert trends[0]["confidence"] == "medium"


# ---------------------------------------------------------------------------
# score_candidate / Candidate sanity
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Subject-overlap merge (Stage 1b) - zero ticket overlap, same person/device
# ---------------------------------------------------------------------------

def test_zero_ticket_overlap_but_shared_contact_and_device_merges():
    device = "GWH-PW0PYPCC"
    group_a = [
        _digest(400, contact="Ike Dixon", devices=[device], entities={"bitlocker_recovery"}),
        _digest(401, contact="Other A1", entities={"bitlocker_recovery"}),
        _digest(402, contact="Other A2", entities={"bitlocker_recovery"}),
    ]
    group_b = [
        _digest(500, contact="Ike Dixon", devices=[device], entities={"startup_repair"}),
        _digest(501, contact="Other B1", entities={"startup_repair"}),
        _digest(502, contact="Other B2", entities={"startup_repair"}),
    ]
    digests = group_a + group_b + _noise()
    trends = detect_trends(digests, now=NOW, model_call=None)
    combined = [
        t for t in trends
        if {tk["id"] for tk in t["tickets"]} >= {400, 401, 402, 500, 501, 502}
    ]
    assert len(combined) == 1


def test_zero_ticket_overlap_no_shared_subject_does_not_merge():
    # Distinct summary wording per group, not just distinct entities - the
    # default "issue" summary is identical across fixtures and would give
    # the fake test-double embedding (see conftest._fake_embed_texts)
    # spurious word overlap that a real embedding model wouldn't have.
    group_a = [
        _digest(600, contact="A1", entities={"bitlocker_recovery"}, summary="printer will not print at all"),
        _digest(601, contact="A2", entities={"bitlocker_recovery"}, summary="printer will not print at all"),
        _digest(602, contact="A3", entities={"bitlocker_recovery"}, summary="printer will not print at all"),
    ]
    group_b = [
        _digest(700, contact="B1", entities={"startup_repair"}, summary="cannot connect to vpn from home"),
        _digest(701, contact="B2", entities={"startup_repair"}, summary="cannot connect to vpn from home"),
        _digest(702, contact="B3", entities={"startup_repair"}, summary="cannot connect to vpn from home"),
    ]
    digests = group_a + group_b + _noise()
    trends = detect_trends(digests, now=NOW, model_call=None)
    assert len(trends) == 2
    for t in trends:
        ids = {tk["id"] for tk in t["tickets"]}
        assert ids in ({600, 601, 602}, {700, 701, 702})


def test_subject_overlap_chain_cap_does_not_collapse_whole_corpus():
    # Five unrelated candidates, each promoted on its own, all sharing one
    # busy contact plus two of their own. MAX_SUBJECT_MERGE_CHAIN=4 must
    # stop all five from becoming a single trend.
    busy = "Busy Contact"
    # Wholly unrelated wording per group (not a shared template with one
    # varying token) so the fake test-double embedding (see
    # conftest._fake_embed_texts, word-overlap based) doesn't spuriously
    # merge groups the deterministic subject-overlap cap already keeps
    # separate - a real embedding model would key on meaning, not on
    # sharing four out of five template words.
    unrelated_summaries = [
        "printer will not print in accounting",
        "vpn drops every few minutes at home",
        "excel crashes opening a shared workbook",
        "bitlocker recovery key requested at boot",
        "outlook stuck reconnecting to exchange",
    ]
    digests = []
    for g in range(5):
        base = 800 + g * 10
        summary = unrelated_summaries[g]
        digests += [
            _digest(base, contact=busy, entities={f"entity_{g}"}, summary=summary),
            _digest(base + 1, contact=f"Other{g}A", entities={f"entity_{g}"}, summary=summary),
            _digest(base + 2, contact=f"Other{g}B", entities={f"entity_{g}"}, summary=summary),
        ]
    digests += _noise()
    trends = detect_trends(digests, now=NOW, model_call=None)
    assert len(trends) >= 2
    all_ids = {tk["id"] for t in trends for tk in t["tickets"]}
    largest = max(len({tk["id"] for tk in t["tickets"]}) for t in trends)
    assert largest < len(all_ids)


def test_subject_overlap_merge_requires_both_sides_above_threshold():
    # One side never clears MIN_TICKETS on its own; the shared subject must
    # not pull it up into the other's trend.
    device = "GWH-BELOWTHRESH"
    promoted_group = [
        _digest(900, contact="X1", devices=[device], entities={"bitlocker_recovery"}),
        _digest(901, contact="X2", entities={"bitlocker_recovery"}),
        _digest(902, contact="X3", entities={"bitlocker_recovery"}),
    ]
    below_threshold_group = [
        _digest(950, contact="Y1", devices=[device], entities={"startup_repair"}),
        _digest(951, contact="Y1", devices=[device], entities={"startup_repair"}),
    ]
    digests = promoted_group + below_threshold_group + _noise()
    trends = detect_trends(digests, now=NOW, model_call=None)
    assert len(trends) == 1
    ids = {tk["id"] for tk in trends[0]["tickets"]}
    assert ids == {900, 901, 902}


def test_merged_trend_counts_are_recomputed_union_not_sum():
    # A ticket present via both the Jaccard pass and the subject-overlap
    # pass must be counted once, not twice.
    device = "GWH-DUPCOUNT"
    group_a = [
        _digest(1000, contact="Ike Dixon", devices=[device], entities={"bitlocker_recovery"}),
        _digest(1001, contact="A2", entities={"bitlocker_recovery"}),
        _digest(1002, contact="A3", entities={"bitlocker_recovery"}),
    ]
    group_b = [
        _digest(1000, contact="Ike Dixon", devices=[device], entities={"startup_repair"}),
        _digest(1100, contact="B2", entities={"startup_repair"}),
        _digest(1101, contact="B3", entities={"startup_repair"}),
    ]
    digests = group_a + group_b + _noise()
    trends = detect_trends(digests, now=NOW, model_call=None)
    combined = [t for t in trends if any(tk["id"] == 1000 for tk in t["tickets"])]
    assert len(combined) == 1
    ids = {tk["id"] for tk in combined[0]["tickets"]}
    assert ids == {1000, 1001, 1002, 1100, 1101}
    assert combined[0]["ticket_count"] == 5


# ---------------------------------------------------------------------------
# Two-stage semantic pass (Stage A embedding merge / Stage B narration)
# ---------------------------------------------------------------------------

def test_stage_b_call_count_capped_even_with_20_groups():
    from cron.trend_cluster_semantic import MAX_NARRATION_CALLS, _run_stage_b

    candidates = [
        Candidate(entities={f"e{i}"}, digests=[_digest(9000 + i, contact=f"U{i}")])
        for i in range(20)
    ]
    groups = [{"indices": [i], "reason": ""} for i in range(20)]
    calls = {"n": 0}

    def counting(system, user):
        calls["n"] += 1
        return '{"title": "t", "why_related": "w", "recommended_action": "a"}'

    _run_stage_b(groups, candidates, counting)
    assert calls["n"] == MAX_NARRATION_CALLS


def test_embedding_merges_zero_overlap_candidates_with_matching_wording():
    # Two candidates with completely disjoint ticket sets - the kind of
    # merge ticket-set Jaccard and subject overlap can never catch. Same
    # summary wording (a paraphrase in reality; identical here since the
    # fake test-double embedding is word-overlap based - see
    # conftest._fake_embed_texts) pushes their embeddings together so
    # Stage A's cosine similarity merges them.
    # Different entities so build_candidates() (Stage 1) seeds two separate
    # candidates - the merge under test happens in semantic_pass (Stage A),
    # not in the deterministic entity-sharing pass.
    same_wording = "blue screen asking for a recovery key after update"
    group_a = [
        _digest(2000, contact="P1", entities={"issue_a"}, summary=same_wording),
        _digest(2001, contact="P2", entities={"issue_a"}, summary=same_wording),
        _digest(2002, contact="P3", entities={"issue_a"}, summary=same_wording),
    ]
    group_b = [
        _digest(2100, contact="Q1", entities={"issue_b"}, summary=same_wording),
        _digest(2101, contact="Q2", entities={"issue_b"}, summary=same_wording),
        _digest(2102, contact="Q3", entities={"issue_b"}, summary=same_wording),
    ]
    digests = group_a + group_b + _noise()

    trends = detect_trends(digests, now=NOW, model_call=None)
    merged = [
        t for t in trends
        if {tk["id"] for tk in t["tickets"]} >= {2000, 2001, 2002, 2100, 2101, 2102}
    ]
    assert len(merged) == 1
    assert merged[0]["ticket_count"] == 6


def test_score_candidate_rewards_cross_stream_and_subject_count():
    human_only = Candidate(
        entities={"bitlocker_recovery"},
        digests=[_digest(1, contact="A"), _digest(2, contact="B"), _digest(3, contact="C")],
    )
    cross_stream = Candidate(
        entities={"bitlocker_recovery"},
        digests=[
            _digest(1, contact="A"),
            _digest(2, contact="B"),
            _digest(3, contact="C", is_automated=True),
        ],
    )
    assert score_candidate(cross_stream) > score_candidate(human_only)
