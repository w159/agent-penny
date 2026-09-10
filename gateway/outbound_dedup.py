"""Structural guard against repeated/near-duplicate outbound chat messages.

The owner's complaint (2026-08-28, verbatim): "stop allowing penny to
constantly repeat the same messages ... like she did about three of ernesto
velarde's tickets earlier." Five messages went to one Teams group chat in
six minutes, all rephrased restatements of the same ask about the same three
tickets, closing with the catchphrase "Your move." repeated over and over.

Root cause of WHY she had nothing new to say was a separate MCP bug (every
tool call was failing, so she had no fresh facts each turn) that is already
fixed elsewhere. That explains the repetition; it does not excuse it. A
person with nothing new to say says nothing. This module is the structural
guard that makes that true in Python, at the send boundary, so no amount of
prompt drift can talk a model past it.

Three independent checks, all enforced here rather than in a prompt:

  1. Near-duplicate suppression: a candidate message is compared against the
     recent messages already sent to the same chat. Byte-identical hashing
     would catch none of the five real messages above -- the wording
     differs every time. Instead: normalized token overlap (Jaccard) PLUS
     the set of ticket ids referenced. Two messages about the same ticket
     ids making a similar ask, sent close together in time, are the same
     complaint said again -- unless the candidate introduces a status-change
     word (moved, closed, resolved, ...) absent from the prior message, in
     which case it carries new information and is never suppressed. See
     ``is_near_duplicate``.
  2. Verbal-tic stripping: a small configurable catchphrase list (seeded
     with "Your move.") is stripped from the tail of outgoing text rather
     than rejecting the whole message -- a trailing tic doesn't invalidate
     real content sitting in front of it, so stripping is kinder than
     dropping. See ``strip_tics``.
  3. Per-chat rate limiting: independent of similarity, caps how many
     messages may go to one chat in a rolling window, because five
     *distinct* messages in six minutes is still exhausting. A direct
     reply to a human's question is exempt -- answering someone who just
     asked something is not spam, and must never be silently dropped.
     See ``check_rate_limit``.

Persistence: a small rolling history (last HISTORY_LIMIT messages plus
timestamps) per chat, under the established ops-memory convention
(``OPS_DIR = get_hermes_home() / "memories" / "ops"``, see
cron/escalation.py:57), so the guard survives a gateway restart.

Fail-open: any internal error in this module must never block a send. A
dedup layer that can silence Penny entirely is worse than the repetition it
prevents (see ``guard_outbound``).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

OPS_DIR = get_hermes_home() / "memories" / "ops"
HISTORY_FILE = OPS_DIR / "outbound_dedup_history.json"

# --------------------------------------------------------------------------
# Configuration defaults
# --------------------------------------------------------------------------

# How many of the most recent sends per chat are kept (and compared
# against). 10 is enough to catch a burst like the real 5-in-6-minutes
# incident without the file growing unbounded.
HISTORY_LIMIT = 10

# Near-duplicate window: only messages sent to the same chat within this
# many minutes are considered for similarity comparison. The five real
# messages landed within 6 minutes of each other; 30 minutes gives comfortable
# margin for a slow back-and-forth without treating unrelated messages sent
# hours apart as repeats.
SIMILARITY_WINDOW_MINUTES = 30

# A candidate is a "repeat" when its token-overlap Jaccard score against a
# prior message meets this threshold (see is_near_duplicate for how ticket
# ids raise or lower the bar). 0.15 was picked by running it over the five
# real incident messages: the three pure restatements score 0.19-0.35
# against each other and clear this bar, while the one message that carried
# a genuinely new fact (a ConnectWise tool failure, not a ticket update)
# scores only 0.067 against the original and correctly stays under it.
SIMILARITY_THRESHOLD = 0.15

# Rate limit: at most this many autonomous (non-reply) messages per chat in
# this many minutes. 3 in 10 keeps a burst like the real incident (5 in 6
# minutes) from recurring while still allowing normal, spaced-out updates.
RATE_LIMIT_MAX_MESSAGES = 3
RATE_LIMIT_WINDOW_MINUTES = 10

# Catchphrases stripped from the tail of outgoing text. Extend this list
# without a code edit by adding entries -- callers may also pass their own
# list to override/extend this default.
DEFAULT_BANNED_TICS = [
    "Your move.",
    "Your move",
]

# Words that signal an actual state change rather than a restatement of the
# same demand. A candidate that introduces one of these words -- absent
# from the prior message it would otherwise match -- carries new
# information even when it references the same ticket ids as before (e.g.
# "#94492 just moved to In Progress" after "those tickets still need
# blocker notes"), so it is never suppressed as a duplicate.
_STATUS_CHANGE_WORDS = frozenset({
    "moved", "progress", "closed", "resolved", "reopened", "escalated",
    "assigned", "unassigned", "fixed", "completed", "done", "update",
    "updated", "scheduled", "cancelled", "canceled", "reassigned",
})

_TICKET_ID_RE = re.compile(r"#(\d{3,7})\b")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Stopwords stripped before token-overlap scoring so shared connective
# tissue ("the", "those", "still") doesn't inflate similarity between
# messages that aren't actually about the same thing.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "been", "to", "of", "in", "on", "at", "for", "with", "those", "these",
    "this", "that", "it", "its", "they", "them", "either", "i", "you",
    "your", "still", "back", "let", "keep", "calling", "out", "until",
    "move", "so", "what", "want", "me", "ll", "won", "t",
})


@dataclass
class GuardResult:
    """Outcome of running a candidate message through the guard."""

    allowed: bool
    text: str  # possibly tic-stripped text to actually send
    reason: Optional[str] = None  # set when allowed is False, or when text was modified


@dataclass
class _HistoryEntry:
    ts: float
    text: str
    tokens: list = field(default_factory=list)
    ticket_ids: list = field(default_factory=list)
    is_reply: bool = False


# --------------------------------------------------------------------------
# Text analysis
# --------------------------------------------------------------------------


def extract_ticket_ids(text: str) -> set:
    """Return the set of ticket ids (as ints) referenced in ``text``.

    Matches ``#94669`` style references, which is how every real message in
    the incident referenced tickets.
    """
    return {int(m) for m in _TICKET_ID_RE.findall(text or "")}


def normalize_tokens(text: str) -> set:
    """Lowercase, tokenize, and drop stopwords -- the basis for Jaccard
    similarity. No stemming library is used; the incident's five messages
    already share enough raw vocabulary (tickets, blocker, notes, close)
    that stemming isn't needed to catch them, and skipping it avoids a new
    dependency.
    """
    words = _WORD_RE.findall((text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def strip_tics(text: str, tics: Optional[list] = None) -> tuple:
    """Strip any banned catchphrase from the tail of ``text``.

    Returns ``(stripped_text, removed)`` where ``removed`` is the list of
    phrases actually removed. Stripping (not rejecting the whole message) is
    the right call here: a trailing tic doesn't invalidate real content that
    precedes it, and dropping a message with real content over a closer
    phrase would be worse than the phrase itself.
    """
    tics = tics if tics is not None else DEFAULT_BANNED_TICS
    result = text or ""
    removed = []
    changed = True
    while changed:
        changed = False
        stripped = result.rstrip()
        for tic in tics:
            if not tic:
                continue
            if stripped.endswith(tic):
                stripped = stripped[: -len(tic)].rstrip()
                removed.append(tic)
                changed = True
        result = stripped
    return result, removed


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def _load_history() -> dict:
    """Read the per-chat rolling history. Missing or corrupt -> empty."""
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("outbound_dedup: history file unreadable (%s), starting fresh", e)
        return {}


def _save_history(history: dict) -> None:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, sort_keys=True), encoding="utf-8")


def _record_send(chat_id: str, text: str, ticket_ids: set, is_reply: bool, now: float) -> None:
    history = _load_history()
    chat_entries = history.get(chat_id, [])
    chat_entries.append({
        "ts": now,
        "text": text,
        "tokens": sorted(normalize_tokens(text)),
        "ticket_ids": sorted(ticket_ids),
        "is_reply": is_reply,
    })
    history[chat_id] = chat_entries[-HISTORY_LIMIT:]
    _save_history(history)


# --------------------------------------------------------------------------
# Near-duplicate suppression
# --------------------------------------------------------------------------


def is_near_duplicate(
    candidate_text: str,
    prior_entries: list,
    *,
    now: float,
    window_minutes: float = SIMILARITY_WINDOW_MINUTES,
    threshold: float = SIMILARITY_THRESHOLD,
) -> Optional[dict]:
    """Return the matched prior entry dict if ``candidate_text`` is a
    near-duplicate of something already sent recently, else ``None``.

    A message is a repeat when, against some recent prior message to the
    same chat:
      - neither message names a ticket id disagreeing with the other (two
        messages that both name tickets but name DIFFERENT ones are never
        a repeat -- that disagreement is itself new information), AND
      - token overlap (Jaccard) meets ``threshold`` when either message
        names a ticket id (the shared/one-sided ticket reference is itself
        strong evidence of "same subject"), or meets a higher bar
        (threshold + 0.15) when neither message names any ticket id at all
        -- general chat without ticket ids needs a stronger textual match
        to avoid false positives on short, generic replies ("thanks", "ok").

    New-fact protection: a message carrying a new ticket id not in the
    prior set, or a materially different token set (dropping below
    threshold), is NOT a duplicate even if it discusses the same subject --
    e.g. "94492 just moved to In Progress" differs from a prior message
    both in ticket-id overlap (adds no new ids alone) and in vocabulary
    ("moved", "progress" are new, absent from a stale demand for blocker
    notes), which drops Jaccard below threshold in practice.
    """
    candidate_tokens = normalize_tokens(candidate_text)
    candidate_ids = extract_ticket_ids(candidate_text)
    window_seconds = window_minutes * 60

    for entry in reversed(prior_entries):
        age = now - entry.get("ts", 0)
        if age > window_seconds:
            continue
        prior_tokens = set(entry.get("tokens", []))
        prior_ids = set(entry.get("ticket_ids", []))
        score = jaccard(candidate_tokens, prior_tokens)

        new_words = candidate_tokens - prior_tokens
        if new_words & _STATUS_CHANGE_WORDS:
            continue

        if candidate_ids and prior_ids and candidate_ids != prior_ids:
            # Both messages name tickets but disagree on which ones -- that
            # is new information (a ticket dropped, or a new one added),
            # never a plain restatement. Do not suppress.
            continue

        if candidate_ids or prior_ids:
            # At least one side names tickets and they agree (or the other
            # side is silent on ids, e.g. an early message that hadn't
            # started citing ticket numbers yet) -- the base threshold is
            # enough, since the shared ticket reference is itself strong
            # evidence of "same subject."
            if score >= threshold:
                return entry
        else:
            # Neither message names a ticket id -- ticket ids can't help,
            # so require a stronger textual match to avoid false positives
            # on short, generic replies ("thanks", "ok", "got it").
            if score >= (threshold + 0.15):
                return entry
    return None


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def check_rate_limit(
    prior_entries: list,
    *,
    now: float,
    is_reply: bool,
    max_messages: int = RATE_LIMIT_MAX_MESSAGES,
    window_minutes: float = RATE_LIMIT_WINDOW_MINUTES,
) -> bool:
    """Return True if sending now would exceed the per-chat rate limit.

    A direct reply to a human's question (``is_reply=True``) is always
    exempt: answering someone who just asked something is not spam, and the
    rate limit only ever governs autonomous/unsolicited traffic (cron nags,
    proactive nudges). Callers must pass ``is_reply`` based on whether this
    send is a response to an inbound message, not on message content.
    """
    if is_reply:
        return False
    window_seconds = window_minutes * 60
    recent_autonomous = [
        e for e in prior_entries
        if not e.get("is_reply") and (now - e.get("ts", 0)) <= window_seconds
    ]
    return len(recent_autonomous) >= max_messages


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def guard_outbound(
    chat_id: str,
    text: str,
    *,
    is_reply: bool = False,
    tics: Optional[list] = None,
    similarity_window_minutes: float = SIMILARITY_WINDOW_MINUTES,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    rate_limit_max_messages: int = RATE_LIMIT_MAX_MESSAGES,
    rate_limit_window_minutes: float = RATE_LIMIT_WINDOW_MINUTES,
    now: Optional[float] = None,
) -> GuardResult:
    """Run ``text`` through tic-stripping, near-duplicate suppression, and
    rate limiting before it is sent to ``chat_id``. Records the send in
    history when allowed.

    Fail-open: any internal exception here is logged and treated as an
    allow -- this guard must never be the reason Penny goes silent.
    """
    try:
        now = now if now is not None else time.time()
        stripped_text, removed_tics = strip_tics(text, tics)
        if removed_tics:
            logger.info(
                "outbound_dedup: stripped tic(s) %s for chat=%s", removed_tics, chat_id,
            )

        history = _load_history()
        prior_entries = history.get(chat_id, [])

        dup = is_near_duplicate(
            stripped_text, prior_entries, now=now,
            window_minutes=similarity_window_minutes,
            threshold=similarity_threshold,
        )
        if dup is not None:
            logger.info(
                "outbound_dedup: suppressed near-duplicate for chat=%s "
                "(matched prior sent %.0fs ago: %r)",
                chat_id, now - dup.get("ts", now), dup.get("text", "")[:200],
            )
            return GuardResult(allowed=False, text=stripped_text, reason="near_duplicate")

        if check_rate_limit(
            prior_entries, now=now, is_reply=is_reply,
            max_messages=rate_limit_max_messages,
            window_minutes=rate_limit_window_minutes,
        ):
            logger.info(
                "outbound_dedup: rate limit engaged for chat=%s "
                "(>= %d autonomous messages in last %.0f min)",
                chat_id, rate_limit_max_messages, rate_limit_window_minutes,
            )
            return GuardResult(allowed=False, text=stripped_text, reason="rate_limited")

        _record_send(chat_id, stripped_text, extract_ticket_ids(stripped_text), is_reply, now)
        reason = "tic_stripped" if removed_tics else None
        return GuardResult(allowed=True, text=stripped_text, reason=reason)
    except Exception as e:
        logger.error("outbound_dedup: internal error, failing open: %s", e, exc_info=True)
        return GuardResult(allowed=True, text=text, reason="guard_error_fail_open")
