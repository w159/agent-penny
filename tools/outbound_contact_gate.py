"""Outbound-contact gate -- blocks agent-initiated messaging to anyone outside
the established IT roster / conversation until Jerry explicitly approves it.

Owner's rule (verbatim, from the capability-review ticket): Penny must never
email or message anyone she isn't already established to talk to -- the IT
department roster (the same people ``TEAMS_ALLOWED_USERS`` already
authorizes to talk to her) and the existing Teams group chat membership, and
Jerry himself -- without his direct, per-instance approval first. Ordinary
in-conversation replies to people already in an active chat are unaffected;
this only gates NEW agent-initiated contact to a target outside that roster.

This module does two things:

  1. ``is_established_outbound_target()`` decides whether a resolved send
     target (platform, chat_id, thread_id) is already part of the roster,
     reusing the SAME per-platform env vars the gateway already uses to
     authorize inbound senders (``<PLATFORM>_ALLOWED_USERS``,
     ``<PLATFORM>_GROUP_ALLOWED_CHATS``, ``<PLATFORM>_HOME_CHANNEL`` --
     Teams' variants are already populated with the IT staff + Jerry and the
     "Triage Sweep" group chat). No second roster file is built: this is
     "already exists," per the investigation, so it is reused verbatim.

  2. ``check_outbound_contact()`` blocks a non-roster target on the exact
     same human-approval plumbing ``tools/connector_action_gate.py`` uses
     for a NinjaOne device action (``request_connector_action_approval`` --
     one approval mechanism, not a second one), with an in-process dedupe
     cache so a retried identical send does not re-prompt Jerry every time.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# How long an identical (platform, target, thread, message) decision is
# reused without re-prompting Jerry or re-auditing as a fresh event. Keeps a
# retried tool call from spamming approval cards for the exact same send.
_DEDUPE_TTL_SECONDS = 300

_dedupe_lock = threading.Lock()
_dedupe_cache: dict[tuple[str, str, str, str], tuple[bool, str, float]] = {}


def _csv_set(raw: str) -> set[str]:
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def _platform_established_targets(platform_name: str) -> set[str]:
    """Chat/user ids already authorized for *platform_name*: its configured
    home channel, its allowed-senders roster, and its allowed group chat(s).
    Sourced entirely from existing env config -- see module docstring."""
    from tools.send_message_targets import _HOME_CHANNEL_ENV_OVERRIDES

    key = platform_name.upper()
    home_env = _HOME_CHANNEL_ENV_OVERRIDES.get(platform_name, f"{key}_HOME_CHANNEL")
    targets: set[str] = set()
    home = os.getenv(home_env, "").strip()
    if home:
        targets.add(home)
    targets |= _csv_set(os.getenv(f"{key}_ALLOWED_USERS", ""))
    targets |= _csv_set(os.getenv(f"{key}_GROUP_ALLOWED_CHATS", ""))
    return targets


def is_established_outbound_target(
    platform_name: str, chat_id: Optional[str], thread_id: Optional[str] = None,
) -> bool:
    """True when *chat_id* (or *thread_id*) is already part of the roster for
    *platform_name*. An empty ``chat_id`` means the caller is about to fall
    back to that platform's own home channel, which is always established."""
    if not chat_id:
        return True
    targets = _platform_established_targets(platform_name)
    return chat_id in targets or (bool(thread_id) and thread_id in targets)


def _dedupe_key(platform_name: str, chat_id: Optional[str], thread_id: Optional[str], message: str) -> tuple[str, str, str, str]:
    return (platform_name, chat_id or "", thread_id or "", message or "")


def _cached_decision(key) -> Optional[tuple[bool, str]]:
    with _dedupe_lock:
        entry = _dedupe_cache.get(key)
    if entry and (time.time() - entry[2]) < _DEDUPE_TTL_SECONDS:
        return entry[0], entry[1]
    return None


def _store_decision(key, allowed: bool, outcome: str) -> None:
    with _dedupe_lock:
        _dedupe_cache[key] = (allowed, outcome, time.time())
        # Bound growth for a long-running gateway process.
        if len(_dedupe_cache) > 2048:
            oldest = sorted(_dedupe_cache.items(), key=lambda kv: kv[1][2])[:512]
            for stale_key, _ in oldest:
                _dedupe_cache.pop(stale_key, None)


def check_outbound_contact(
    *, platform_name: str, chat_id: Optional[str], thread_id: Optional[str] = None,
    message: str = "", session_key: str = "", requested_by: str = "Penny",
) -> tuple[bool, str]:
    """``(allowed, outcome)`` for one agent-initiated outbound send.

    ``outcome`` is one of ``"auto_allowed_roster"``, ``"approved"``,
    ``"denied"``, ``"timeout"``, ``"error"``, or one of those suffixed
    ``"_cached"`` when a dedupe hit answered without re-prompting. Never
    raises -- an internal failure returns ``(False, "error")``, fail closed.
    """
    from tools.connector_action_gate import record_audit_event, request_connector_action_approval

    args_for_audit = {"target": chat_id or "(home channel)", "thread": thread_id or "", "message_preview": (message or "")[:200]}

    try:
        established = is_established_outbound_target(platform_name, chat_id, thread_id)
    except Exception as exc:
        logger.error("outbound_contact_gate: roster check failed for %s: %s -- failing closed", platform_name, exc)
        record_audit_event(
            session_key=session_key, requested_by=requested_by, server_name="outbound_message",
            tool_name=platform_name, arguments=args_for_audit, outcome="error",
            detail=f"roster check raised: {exc}",
        )
        return False, "error"

    if established:
        record_audit_event(
            session_key=session_key, requested_by=requested_by, server_name="outbound_message",
            tool_name=platform_name, arguments=args_for_audit, outcome="auto_allowed_roster",
        )
        return True, "auto_allowed_roster"

    key = _dedupe_key(platform_name, chat_id, thread_id, message)
    if cached := _cached_decision(key):
        allowed, outcome = cached
        record_audit_event(
            session_key=session_key, requested_by=requested_by, server_name="outbound_message",
            tool_name=platform_name, arguments=args_for_audit, outcome=f"{outcome}_cached",
            detail="dedupe hit: identical request answered without re-prompting",
        )
        return allowed, f"{outcome}_cached"

    approved, outcome = request_connector_action_approval(
        session_key=session_key, server_name="outbound_message", tool_name=platform_name,
        arguments=args_for_audit, requested_by=requested_by,
    )
    _store_decision(key, approved, outcome)
    return approved, outcome
