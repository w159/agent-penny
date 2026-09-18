"""cw_contact_context plugin - structural pre-fetch of the end-user/ticket-contact context.

Root cause (state.db session 20260915_133141_81527460, msgs 28187-28194): Jerry asked
"now, who's the worst end user?" and Penny answered from the internal-tech/roster
frame twice ("I keep the roasts strictly internal... which tech's ticket habits") before
Jerry had to spell out the tool by name ("no, i said end user. for your tools this would
be cw_ticket_contact"). Nothing in SOUL.md or agent/system_prompt.py ever told the model
that "end user" means the ConnectWise ticket contact, and tools/cw_contact_tool.py's
gating in toolsets.py only makes the lookup tools *reachable* - it does not make the model
*reach for them*. Session 20260916_121850_4a3ee518 shows the same pattern again: ticket
#91041 comes up in conversation and Penny narrates from cw_get_ticket's inline fields
without ever calling get_ticket_contact/find_tickets_by_contact to surface that contact's
other open tickets.

A SOUL.md instruction (see the "End users are ConnectWise ticket contacts" section) covers
the fuzzy cases (a name mentioned with no ticket number, "who's the worst end user"-style
questions) where an instruction is enough because there's a tool the model can reach for.
This plugin is the structural backstop for the reliable case: whenever a ConnectWise
ticket number appears in the incoming message, the contact + their recent ticket history
is pre-fetched and injected as `pre_llm_call` context - mirroring how ext_prefetch_cache
already does this for external memory (agent/turn_context.py's
compose_user_api_content) - so the reply carries real history even if the model never
decides to call a tool. Prompt-only fixes proved unreliable in this environment today
(turn_finalizer.py's _strip_behavior_state_aside backstop exists for the exact same
reason: a model can ignore its own system prompt).

Every lookup goes through tools/cw_contact_tool.py's library functions, so the FTC
Safeguards/GLBA/Reg S-P audit line (`cw_contact_tool: get_ticket_contact` / ``:
find_tickets_by_contact``, one INFO log per PII-surfacing lookup) fires exactly as it does
for a model-initiated tool call - this plugin adds no separate access path or audit gap.
"""
from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ConnectWise ticket numbers are referenced in chat either as "#91041" or "ticket 91041" /
# "ticket #91041". Require one of those two shapes (not a bare number) so we don't fire on
# phone numbers, dollar amounts, or other incidental digits in a message.
_TICKET_NUMBER_RE = re.compile(r"(?:\bticket\s*#?|#)\s*(\d{4,7})\b", re.IGNORECASE)

# One lookup call gets a short budget; a stuck/slow ConnectWise API call must never hold up
# the turn. Same shape as agent/memory_manager.py's external-provider prefetch bound.
_LOOKUP_TIMEOUT_S = 6.0

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cw-contact-prefetch")


def _extract_text(user_message: Any) -> str:
    """Flatten whatever shape the host passes for the turn's user message."""
    if isinstance(user_message, str):
        return user_message
    try:
        from agent.message_content import flatten_message_text

        return flatten_message_text(user_message)
    except Exception:
        return ""


def _run_lookup(ticket_number: str) -> Optional[str]:
    """Fetch the ticket's contact plus their recent ticket history, formatted for
    injection. Returns None on any failure (missing CW config, 404, API error, ambiguous
    contact) - a failed pre-fetch must never block or corrupt the turn, it just means no
    structural context was available and the SOUL.md instruction is the only backstop."""
    from tools.cw_contact_tool import check_cw_contact_requirements, find_tickets_by_contact, get_ticket_contact

    if not check_cw_contact_requirements():
        return None

    async def _fetch() -> Optional[str]:
        contact_result = await get_ticket_contact(ticket_number)
        if not contact_result.get("success"):
            return None

        contact_name = contact_result.get("contact_name")
        contact_email = contact_result.get("contact_email")
        lines = [
            f"Ticket #{ticket_number}'s contact (the end user) is "
            f"{contact_name or 'unknown'}"
            + (f" <{contact_email}>" if contact_email else "")
            + f", company {contact_result.get('company') or 'unknown'}, "
            f"status {contact_result.get('status') or 'unknown'}."
        ]

        history_key = contact_email or contact_name
        if history_key:
            history = await find_tickets_by_contact(history_key, limit=5)
            if history.get("success"):
                other_tickets = [
                    t for t in history.get("tickets", []) if str(t.get("ticket_id")) != str(ticket_number)
                ]
                if other_tickets:
                    lines.append(f"Their other recent tickets ({len(other_tickets)}):")
                    for t in other_tickets:
                        lines.append(
                            f"  - #{t.get('ticket_id')}: {t.get('summary')} "
                            f"[{t.get('status') or 'unknown status'}]"
                        )
                else:
                    lines.append("No other recent tickets on file for this contact.")

        return "\n".join(lines)

    return asyncio.run(_fetch())


def _on_pre_llm_call(*, user_message: Any = None, **_: Any) -> Optional[dict]:
    text = _extract_text(user_message)
    if not text:
        return None

    match = _TICKET_NUMBER_RE.search(text)
    if not match:
        return None
    ticket_number = match.group(1)

    future = _executor.submit(_run_lookup, ticket_number)
    try:
        context = future.result(timeout=_LOOKUP_TIMEOUT_S)
    except _FutureTimeoutError:
        logger.warning("cw_contact_context: lookup for ticket #%s timed out after %.1fs", ticket_number, _LOOKUP_TIMEOUT_S)
        return None
    except Exception:
        logger.warning("cw_contact_context: lookup for ticket #%s failed", ticket_number, exc_info=True)
        return None

    if not context:
        return None

    return {
        "context": (
            "[ConnectWise ticket-contact context - auto-fetched, not from the user]\n"
            + context
        )
    }


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
