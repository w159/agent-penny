#!/usr/bin/env python3
"""
Deterministic-first clustering engine that turns cron/trend_corpus.py's
TicketDigest stream into named, thresholded TRENDS.

The owner's own framing: several tickets can each look like a surface-level
help desk case in isolation, and only turn out to be one bigger issue once
someone sits down and compares notes across the whole board. This module is
that comparison, run every cycle instead of once a week.

Three-stage pipeline, same "Python owns the math, the model only narrates"
discipline as board_watch.py and escalation.py:

  1. build_candidates() - pure Python, no model. Groups digests by shared
     normalized entities (see trend_entities.py), discarding entities too
     generic to carry signal and merging candidates whose ticket sets
     substantially overlap.
  2. A hard MIN_TICKETS / MIN_DISTINCT_SUBJECTS gate, enforced here in code,
     before semantic_pass() ever sees a candidate. One person filing three
     tickets is not a trend, and no prompt wording can talk this module
     into promoting one.
  3. semantic_pass() (cron/trend_cluster_semantic.py) - merges candidates
     describing one real issue by embedding similarity (deterministic, no
     model call), then the model only narrates each surviving group. It
     never computes a count; every number in the final trend dict is
     recomputed in cron/trend_cluster_output.py from the merged ticket set
     after the model responds.

detect_trends() is the single entry point cron wiring calls. `now` is
always passed in, never read from the clock inside this module, so tests
stay deterministic. trend_cluster_semantic.py and trend_cluster_output.py
are split out only to keep every file under the house 300-line cap - both
are implementation details of this one public surface.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from cron.trend_cluster_output import _date_only, finalize_trend
from cron.trend_cluster_semantic import semantic_pass  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Promotion thresholds - see module docstring. Named per the owner's own
# "one person filing three tickets is NOT a trend" example.
# ---------------------------------------------------------------------------
MIN_TICKETS = 3
MIN_DISTINCT_SUBJECTS = 2
WINDOW_DAYS = 14

# An entity in only one ticket carries no clustering signal (nothing to
# link). An entity in more than 10% of the corpus is too generic to mean
# anything - "outlook" showing up in 40% of tickets says nothing about
# which of those tickets are actually related to each other. Was 0.25
# until a live 21-day/1459-ticket pull (2026-08-28) showed "defender"
# alone at 18.0% share seeding a 285-ticket "trend" spanning the entire
# window uniformly (span_days=21, first-to-last ticket) - background
# antivirus-alert noise, not a correlated incident, and it would have
# cleared the old 25% bar. The real bitlocker/bitlocker_recovery signal
# from that same pull sits at 2.3% each, well clear of the new bar.
GENERIC_ENTITY_MIN_TICKETS = 1
GENERIC_ENTITY_MAX_SHARE = 0.10

# Second, corpus-relative promotion gate (see MAX_CANDIDATE_SHARE below):
# a merge of several individually-non-generic entities (each under
# GENERIC_ENTITY_MAX_SHARE alone) can still jaccard-merge into one
# mega-candidate that is, in aggregate, most of the help desk's traffic -
# the same live pull showed an office_365/outlook/teams/err_-6399 merge
# reach 165 tickets (11.3% of corpus) with no single seed entity over
# 6.9%. A "trend" that size is not a dated outage window someone can
# investigate and close; it is business-as-usual chatter that happens to
# share vocabulary. Checked at promotion time (_is_promoted), not at
# entity-seeding time, since it is a property of the merged cluster.
MAX_CANDIDATE_SHARE = 0.10

# Two seed candidates collapse into one when their ticket sets overlap by
# more than this fraction (Jaccard). Tuned so near-duplicate entity pairs
# like startup_repair/bitlocker_recovery - which the corpus shows co-occur
# on almost the same ticket set - merge, while genuinely distinct clusters
# that just happen to share a couple of stray tickets do not.
MERGE_JACCARD_THRESHOLD = 0.5

# Second, independent merge signal (Stage 1b, see _merge_by_subject_overlap):
# two ALREADY-PROMOTED candidates with zero ticket overlap still merge when
# they share this many or more subjects (contacts/devices) - the real-data
# case being bitlocker_recovery and startup_repair, whose ticket sets never
# co-occur but which both hit Ike Dixon's GWH-PW0PYPCC. Only promoted
# candidates participate, so one shared user can never drag a below-threshold
# noise candidate up to trend status.
SUBJECT_OVERLAP_MERGE_MIN_SHARED = 1

# Caps how many candidates one shared subject can chain together. Without
# this, a single busy contact who happens to touch five unrelated issues
# would collapse the whole corpus into one giant "trend" - exactly the kind
# of over-merge the owner warned against.
MAX_SUBJECT_MERGE_CHAIN = 4


@dataclass
class Candidate:
    """One deterministic ticket cluster, pre-model. Digests are the source
    of truth for every count; entities are carried only to build prompts
    and fallback titles."""

    entities: set
    digests: list = field(default_factory=list)

    @property
    def ticket_ids(self) -> set:
        return {d.id for d in self.digests}

    @property
    def contacts(self) -> set:
        return {d.contact for d in self.digests if d.contact}

    @property
    def devices(self) -> set:
        found: set = set()
        for d in self.digests:
            found.update(d.devices)
        return found

    @property
    def subjects(self) -> set:
        """Distinct users OR devices - the threshold's actual unit of
        corroboration. Namespaced separately so a contact named the same
        as a device string can never collide."""
        return {("user", c) for c in self.contacts} | {("device", d) for d in self.devices}

    @property
    def spans_both_streams(self) -> bool:
        """True when this candidate has both automated and human tickets -
        cross-stream corroboration the owner called out as a strong signal."""
        flags = {d.is_automated for d in self.digests}
        return flags == {True, False}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def build_candidates(digests: list) -> list:
    """Stage 1: pure deterministic clustering, no model call.

    Builds an entity -> ticket-id inverted index, drops entities outside
    the [not-singleton, not-too-generic] band, seeds one candidate per
    surviving entity, then repeatedly merges candidates whose ticket sets
    overlap above MERGE_JACCARD_THRESHOLD until no more merges apply.
    """
    corpus_size = len(digests)
    if corpus_size == 0:
        return []

    by_id = {d.id: d for d in digests}
    index: dict = {}
    for d in digests:
        for entity in d.entities:
            index.setdefault(entity, set()).add(d.id)

    max_generic_count = GENERIC_ENTITY_MAX_SHARE * corpus_size

    candidates: list = []
    for entity, ids in index.items():
        if len(ids) <= GENERIC_ENTITY_MIN_TICKETS:
            continue
        if len(ids) > max_generic_count:
            continue
        member_digests = sorted((by_id[i] for i in ids), key=lambda d: d.id)
        candidates.append(Candidate(entities={entity}, digests=member_digests))

    changed = True
    while changed:
        changed = False
        for i in range(len(candidates)):
            if candidates[i] is None:
                continue
            for j in range(i + 1, len(candidates)):
                if candidates[j] is None:
                    continue
                a, b = candidates[i], candidates[j]
                if _jaccard(a.ticket_ids, b.ticket_ids) > MERGE_JACCARD_THRESHOLD:
                    merged_ids = sorted(a.ticket_ids | b.ticket_ids)
                    candidates[i] = Candidate(
                        entities=a.entities | b.entities,
                        digests=[by_id[mid] for mid in merged_ids],
                    )
                    candidates[j] = None
                    changed = True
        candidates = [c for c in candidates if c is not None]

    return candidates


def _merge_by_subject_overlap(candidates: list) -> list:
    """Stage 1b: merges candidates that share a subject (contact or device)
    but were never linked by ticket-set Jaccard overlap because their
    ticket ids are entirely disjoint - the structural case Jaccard can
    never catch (see module docstring). Only called on candidates that
    have ALREADY cleared the promotion gate; merging two independently-real
    trends can only grow the result, never manufacture one from noise.

    Generic on purpose: no entity name, contact name, or ticket id is ever
    referenced here - the signal is "these two clusters touch the same
    person or device," nothing more specific.
    """
    items = [{"c": c, "chain": 1} for c in candidates]
    changed = True
    while changed:
        changed = False
        for i in range(len(items)):
            if items[i] is None:
                continue
            for j in range(i + 1, len(items)):
                if items[j] is None:
                    continue
                a, b = items[i]["c"], items[j]["c"]
                shared = a.subjects & b.subjects
                if len(shared) < SUBJECT_OVERLAP_MERGE_MIN_SHARED:
                    continue
                combined_chain = items[i]["chain"] + items[j]["chain"]
                if combined_chain > MAX_SUBJECT_MERGE_CHAIN:
                    logger.warning(
                        "subject-overlap merge capped at %d candidates "
                        "(shared subjects=%r); leaving separate",
                        MAX_SUBJECT_MERGE_CHAIN,
                        sorted(str(s) for s in shared),
                    )
                    continue
                merged_ids = sorted(a.ticket_ids | b.ticket_ids)
                by_id = {d.id: d for d in a.digests + b.digests}
                items[i] = {
                    "c": Candidate(
                        entities=a.entities | b.entities,
                        digests=[by_id[mid] for mid in merged_ids],
                    ),
                    "chain": combined_chain,
                }
                items[j] = None
                changed = True
        items = [it for it in items if it is not None]

    return [it["c"] for it in items]


def score_candidate(c: Candidate) -> float:
    """Ranks promoted candidates for prompt ordering and fallback naming.
    Not a promotion gate - MIN_TICKETS/MIN_DISTINCT_SUBJECTS already
    decided promotion before this runs."""
    score = len(c.digests) + 2 * len(c.subjects)
    if c.spans_both_streams:
        score += 5
    return float(score)


def _is_promoted(c: Candidate, *, corpus_size: int = 0) -> bool:
    """Stage 2: the hard gate. Enforced here, in code, so the model in
    semantic_pass() never sees a below-threshold candidate to talk up.

    ``corpus_size`` is optional (defaults to 0, which disables the
    MAX_CANDIDATE_SHARE check) so existing callers/tests that only care
    about the ticket/subject floor keep working unchanged; detect_trends()
    below always passes the real windowed corpus size.
    """
    if len(c.digests) < MIN_TICKETS or len(c.subjects) < MIN_DISTINCT_SUBJECTS:
        return False
    if corpus_size > 0 and len(c.digests) > MAX_CANDIDATE_SHARE * corpus_size:
        logger.info(
            "trend_cluster: candidate %s rejected - %d tickets is %.1f%% of a "
            "%d-ticket corpus, over the %.0f%% MAX_CANDIDATE_SHARE bar "
            "(entities=%r)",
            "+".join(sorted(c.entities)[:3]),
            len(c.digests),
            100 * len(c.digests) / corpus_size,
            corpus_size,
            100 * MAX_CANDIDATE_SHARE,
            sorted(c.entities),
        )
        return False
    return True


def _within_window(digest, now: datetime, window_days: int) -> bool:
    try:
        d = datetime.strptime(_date_only(digest.date), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    age_days = (now.date() - d).days
    return 0 <= age_days <= window_days


def detect_trends(digests: list, *, now: datetime, model_call=None, window_days: int = WINDOW_DAYS) -> list:
    """Single entry point: TicketDigest list -> list of trend dicts.

    `now` is always injected, never read from the clock, so callers (and
    tests) control the window explicitly. `model_call` is optional - see
    cron/trend_cluster_semantic.py's semantic_pass() for the fallback
    contract when it is None, errors, or returns unparseable JSON.
    `window_days` defaults to this module's own WINDOW_DAYS (14) so every
    existing caller is unaffected; cron/trend_pass.py's daily rolling
    pass overrides it to match cron.trend.window_days (21) so the
    detection window and the CW pull window agree.
    """
    windowed = [d for d in digests if _within_window(d, now, window_days)]
    corpus_size = len(windowed)
    candidates = build_candidates(windowed)
    promoted = [c for c in candidates if _is_promoted(c, corpus_size=corpus_size)]
    promoted = _merge_by_subject_overlap(promoted)
    # Defensive re-check: a subject-overlap merge only ever grows a
    # candidate's ticket/subject sets, so this should always still hold -
    # but re-verifying here means a future change to the merge pass can't
    # silently smuggle a below-threshold cluster past Stage 2. Also
    # re-applies MAX_CANDIDATE_SHARE, since a subject-overlap merge can
    # push a candidate that individually cleared the bar over it.
    promoted = [c for c in promoted if _is_promoted(c, corpus_size=corpus_size)]
    promoted.sort(key=score_candidate, reverse=True)

    groups = semantic_pass(promoted, model_call=model_call, now=now, window_days=WINDOW_DAYS)

    trends = []
    for group in groups:
        trend = finalize_trend(
            group, min_tickets=MIN_TICKETS, min_distinct_subjects=MIN_DISTINCT_SUBJECTS
        )
        if trend is not None:
            trends.append(trend)
    return trends
