"""roast_variety_context plugin - structural anti-repetition for roast material.

Root cause (Jerry's report, 2026-09-21): asked "roast an end user for me" and got the
exact same Jarvis Williams / "Waiting Client Response pile is aging like forgotten
yogurt" material Penny has used repeatedly, tacked onto an unrelated ask. SOUL.md
already has a "Vary it. Do not reuse the same joke or opener twice in a row" rule, but
this codebase's own established lesson (see plugins/cw_contact_context/__init__.py's
docstring, agent/turn_finalizer.py's _strip_behavior_state_aside) is that a model can
ignore its own system prompt in production; an instruction alone was proven
insufficient here too.

This plugin is the structural, input-side half of the fix: before the model drafts a
reply in the internal IT Teams chat (the same channel VOICE & PERSONALITY's roast
budget is scoped to), it deterministically scans the model's own recent messages in
this session for real roster names (memories/ops/roster.md's `## Name` headings - the
same names roster.md and the roast budget already ground jokes in, not a generic
capitalized-words guess) and, when one was used recently, injects a plain fact: who,
how many messages ago. The model still decides what to write; it just can no longer
plausibly claim not to know it already used that subject. Silent (returns None) when
nothing was said recently or no roster names are found, so it adds no noise to a
normal turn - mirrors cw_contact_context's fail-open, additive-only shape exactly.

This does not attempt to strip or rewrite an actual repeat after the fact - post-hoc
text-similarity stripping can't tell a reused joke from a legitimately recurring real
fact (the same ticket genuinely stalling two days running) without risking deleting
real content. Preventing it via real grounded input, the same architecture already
proven for the end-user/ticket-contact recognition gap, is the safer lever.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The one internal IT Teams group chat the roast budget (VOICE & PERSONALITY,
# SOUL.md) is scoped to - same chat id used throughout this deployment's cron
# delivery config (see docs/CHANGELOG.md, cron/jobs.json's `deliver` field).
_IT_TEAMS_CHAT_ID = "19:d72b9e0d737b4dda960814e674c260b7@thread.v2"

# How many of the model's own most recent messages in this session to scan for a
# reused subject. Small on purpose: "vary it" means don't repeat the last one or
# two things you said, not build a lifetime blocklist of every name ever roasted.
_LOOKBACK_MESSAGES = 6

_ROSTER_NAME_HEADING_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def _it_channel_session(session_id: Any, platform: Any) -> bool:
    return str(platform or "") == "teams" and _IT_TEAMS_CHAT_ID in str(session_id or "")


def _roster_names() -> list[str]:
    """Real names from memories/ops/roster.md's `## Name` headings - the same source
    the roast budget itself is required to ground jokes in. Empty on any read failure
    (missing file, permissions) rather than raising - this plugin is additive-only."""
    try:
        from hermes_constants import get_hermes_home

        roster_path = get_hermes_home() / "memories" / "ops" / "roster.md"
        text = roster_path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [m.group(1).strip() for m in _ROSTER_NAME_HEADING_RE.finditer(text) if m.group(1).strip()]


def _recent_assistant_texts(conversation_history: Any) -> list[str]:
    """The model's own last `_LOOKBACK_MESSAGES` messages in this session, most
    recent first, flattened to plain text."""
    if not isinstance(conversation_history, list):
        return []
    from agent.message_content import flatten_message_text

    texts: list[str] = []
    for msg in reversed(conversation_history):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        text = flatten_message_text(msg.get("content"))
        if text and text.strip():
            texts.append(text)
        if len(texts) >= _LOOKBACK_MESSAGES:
            break
    return texts


def _recently_used_names(conversation_history: Any, names: list[str]) -> list[tuple[str, int]]:
    """``[(name, messages_ago)]`` for each roster name found in the model's own
    recent messages, nearest mention first, deduplicated to the nearest occurrence
    of each name."""
    if not names:
        return []
    recent_texts = _recent_assistant_texts(conversation_history)
    if not recent_texts:
        return []
    found: dict[str, int] = {}
    for messages_ago, text in enumerate(recent_texts, start=1):
        for name in names:
            if name in found:
                continue
            if re.search(rf"\b{re.escape(name)}\b", text):
                found[name] = messages_ago
    return sorted(found.items(), key=lambda pair: pair[1])


def _on_pre_llm_call(
    *, session_id: Any = None, platform: Any = None, conversation_history: Any = None, **_: Any,
) -> Optional[dict]:
    if not _it_channel_session(session_id, platform):
        return None
    try:
        names = _roster_names()
        recent = _recently_used_names(conversation_history, names)
    except Exception:
        logger.warning("roast_variety_context: recent-subject scan failed", exc_info=True)
        return None
    if not recent:
        return None

    lines = ["[Recent roast/mention history in this chat - not from the user, for your own awareness]"]
    for name, messages_ago in recent[:3]:
        ago = "your last message" if messages_ago == 1 else f"{messages_ago} messages ago"
        lines.append(f"- {name} was named in {ago} here.")
    lines.append(
        "If a roast or joke you're about to write would land on the same subject or reuse the same "
        "angle, per SOUL.md's \"Vary it\" rule: pick someone else with a genuinely documented pattern "
        "in memories/ops/roster.md, or a different angle on the same backlog. Otherwise ignore this."
    )
    return {"context": "\n".join(lines)}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
