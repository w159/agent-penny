"""Behavior-change tools -- makes Agent Penny's self-improvement visible.

When a teammate asks Penny to change how she behaves ("stop posting cards
for closed tickets"), she calls propose_behavior_change() to record the
request via cron/behavior_store.py, and an authorized teammate approves it
with approve_behavior_change(). Both calls post a plain-language message
to the originating Teams chat through the existing send_message tool seam
(tools/send_message_tool.py) so the request, the exact diff, the tool
call, and the result are all visible in the group chat -- not a silent
config edit.

Authorization is enforced entirely by cron/behavior_store.py (reads
TEAMS_ALLOWED_USERS, fails closed). This module never re-implements that
check; it only surfaces the store's decision to the chat.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_BEH_ID_RE = re.compile(r"^\s*(?:beh-)?(\d+)\s*$", re.IGNORECASE)
# Matches a reply that names a proposal id anywhere in free text, e.g.
# "approve BEH-12", "BEH-12 approved", "yes to BEH-12". A bare "yes" with
# no id never matches -- the id is the load-bearing part of the pattern.
_BEH_ID_IN_TEXT_RE = re.compile(r"\bbeh-(\d+)\b", re.IGNORECASE)


def parse_beh_id(raw: str) -> Optional[int]:
    """Parse a 'BEH-<n>' (or bare '<n>') proposal id into its integer id.

    Returns None for anything that is not unambiguously a proposal id --
    including a bare "yes" -- so callers never guess at which proposal is
    meant.
    """
    if raw is None:
        return None
    match = _BEH_ID_RE.match(str(raw))
    if not match:
        return None
    return int(match.group(1))


def extract_beh_id_from_reply(text: str) -> Optional[str]:
    """Find a 'BEH-<n>' id anywhere in a chat reply, or None.

    Used to reason about approval replies such as "approve BEH-12" or
    "BEH-12 approved". A message with no BEH-<n> token -- including a bare
    "yes" -- always returns None, since approving without naming which
    proposal is meant would be ambiguous when several proposals are open.
    """
    if not text:
        return None
    match = _BEH_ID_IN_TEXT_RE.search(text)
    if not match:
        return None
    return f"BEH-{match.group(1)}"


def _format_beh_id(row_id: int) -> str:
    return f"BEH-{row_id}"


def _current_identity() -> tuple[str, str, str, str]:
    """Return (user_id, chat_id, message_id, platform) for the running turn."""
    from gateway.session_context import get_session_env

    user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip()
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip()
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    return user_id, chat_id, message_id, platform


def _post_visibility_message(platform: str, chat_id: str, text: str) -> None:
    """Post *text* to the originating Teams chat via the existing send_message seam.

    Only fires for a live Teams session with a known chat id -- never
    raises, since a failed visibility post must not fail the underlying
    propose/approve call.
    """
    if platform != "teams" or not chat_id:
        return
    try:
        from tools.send_message_tool import send_message_tool

        send_message_tool({"action": "send", "target": f"teams:{chat_id}", "message": text})
    except Exception:
        logger.warning("behavior_change_tool: failed to post visibility message", exc_info=True)


def _prior_active_value(key: str, exclude_id: int):
    """Return the value of the currently active rule for *key*, if any."""
    from cron import behavior_store

    try:
        for row in behavior_store.history(key=key, limit=50):
            if row.get("id") != exclude_id and row.get("active"):
                return row.get("value")
    except Exception:
        pass
    return None


def _render_diff(row: dict) -> str:
    """Human-readable rendering of exactly what a proposal would change."""
    if row["kind"] == "knob":
        old_value = _prior_active_value(row["key"], row["id"])
        return f"{row['key']}: {old_value!r} -> {row['value']!r}"
    return row["text"]


PROPOSE_BEHAVIOR_CHANGE_SCHEMA = {
    "name": "propose_behavior_change",
    "description": (
        "Record a proposed change to your own behavior when a teammate asks you to "
        "change how you act (a knob value or a standing instruction). The proposal is "
        "PENDING and has no effect until an authorized teammate approves it with "
        "approve_behavior_change. Use this instead of silently editing behavior -- "
        "the request, the exact diff, and the approval are all posted to the group "
        "chat so self-improvement stays visible."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["knob", "instruction"],
                "description": "'knob' for a named, typed setting (see behavior_knobs.KNOB_SCHEMA); 'instruction' for a standing free-text rule.",
            },
            "text": {
                "type": "string",
                "description": "What you understood the teammate to want, in one clear sentence.",
            },
            "scope": {
                "type": "string",
                "description": "Short label for what area this rule governs, e.g. 'cards', 'triage', 'quiet_hours'.",
            },
            "key": {
                "type": "string",
                "description": "Required for kind='knob': the knob key from KNOB_SCHEMA (e.g. 'cards.post_on_closed_ticket').",
            },
            "value": {
                "description": "Required for kind='knob': the new value, typed per the knob's schema (bool/int/float/str).",
            },
        },
        "required": ["kind", "text", "scope"],
    },
}

APPROVE_BEHAVIOR_CHANGE_SCHEMA = {
    "name": "approve_behavior_change",
    "description": (
        "Activate a pending behavior-change proposal. Only a teammate listed in "
        "TEAMS_ALLOWED_USERS may approve -- the store enforces this and rejects "
        "anyone else. Call this when an authorized teammate replies with an "
        "approval that names the proposal id, e.g. 'approve BEH-12', "
        "'BEH-12 approved', or 'yes to BEH-12'. Never call this for a bare 'yes' "
        "with no BEH-<n> id -- that is ambiguous when more than one proposal is open."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {
                "type": "string",
                "description": "The BEH-<n> id from the proposal message, e.g. 'BEH-12'.",
            },
        },
        "required": ["proposal_id"],
    },
}


def propose_behavior_change_tool(args: dict, **kw) -> str:
    """Handle propose_behavior_change tool calls."""
    kind = args.get("kind")
    text = (args.get("text") or "").strip()
    scope = (args.get("scope") or "").strip()
    key = args.get("key")
    value = args.get("value")

    if kind not in ("knob", "instruction"):
        return tool_error("kind must be 'knob' or 'instruction'")
    if not text:
        return tool_error("text is required")
    if not scope:
        return tool_error("scope is required")

    requested_by, chat_id, message_id, platform = _current_identity()
    if not requested_by:
        return tool_error(
            "No requesting user identity is available in this session; cannot "
            "propose a behavior change without knowing who asked."
        )

    from cron import behavior_store

    try:
        row = behavior_store.propose(
            kind, text, scope=scope, key=key, value=value,
            requested_by=requested_by, source_chat_id=chat_id or None,
            source_message_id=message_id or None,
        )
    except ValueError as e:
        return tool_error(str(e))

    beh_id = _format_beh_id(row["id"])
    diff = _render_diff(row)
    message = (
        f"Behavior change proposed by {requested_by}.\n"
        f"Understood: {text}\n"
        f"Change: {diff}\n"
        f"Proposal id: {beh_id}\n"
        f"An authorized teammate can activate it by replying: approve {beh_id}"
    )
    _post_visibility_message(platform, chat_id, message)

    return json.dumps({
        "proposal_id": beh_id,
        "status": row["status"],
        "diff": diff,
        "chat_message": message,
    })


def approve_behavior_change_tool(args: dict, **kw) -> str:
    """Handle approve_behavior_change tool calls."""
    proposal_id_raw = args.get("proposal_id", "")
    beh_id = parse_beh_id(proposal_id_raw)
    if beh_id is None:
        return tool_error(
            f"'{proposal_id_raw}' is not a valid proposal id. Use the BEH-<n> id "
            "from the proposal message, e.g. 'BEH-12'."
        )

    approved_by, chat_id, _message_id, platform = _current_identity()
    if not approved_by:
        return tool_error(
            "No approving user identity is available in this session; cannot "
            "approve a behavior change without knowing who approved it."
        )

    from cron import behavior_store

    try:
        row = behavior_store.approve(beh_id, approved_by=approved_by)
    except PermissionError as e:
        refusal = (
            f"Approval refused: {approved_by} is not authorized to approve "
            f"behavior changes ({_format_beh_id(beh_id)})."
        )
        _post_visibility_message(platform, chat_id, refusal)
        return tool_error(str(e))
    except ValueError as e:
        return tool_error(str(e))

    diff = _render_diff(row)
    message = (
        f"Behavior change approved by {approved_by}.\n"
        f"Tool call: approve_behavior_change(proposal_id='{_format_beh_id(beh_id)}')\n"
        f"Change: {diff}\n"
        f"Rule is now active."
    )
    _post_visibility_message(platform, chat_id, message)

    return json.dumps({
        "proposal_id": _format_beh_id(beh_id),
        "status": row["status"],
        "diff": diff,
        "chat_message": message,
    })


registry.register(
    name="propose_behavior_change",
    toolset="behavior",
    schema=PROPOSE_BEHAVIOR_CHANGE_SCHEMA,
    handler=propose_behavior_change_tool,
    emoji="🧠",
)

registry.register(
    name="approve_behavior_change",
    toolset="behavior",
    schema=APPROVE_BEHAVIOR_CHANGE_SCHEMA,
    handler=approve_behavior_change_tool,
    emoji="✅",
)
