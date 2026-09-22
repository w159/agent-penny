"""ConnectWise ticket-contact lookup tools for Agent Penny.

Answers "who is the contact/end-user on ticket #X" and "who's been filing
tickets lately, and about what" - questions Penny previously had no tool
for at all (see docs/architecture/penny-messaging.md's CW integration
section, which only covered inbound ticket/note webhooks and the board
watcher, never a contact-facing lookup).

Single-ticket and single-contact lookups (get_ticket_contact,
find_tickets_by_contact) hit the live ConnectWise API directly through
cron/cw_client.py's CWClient - the same thin REST client cron/trend_*.py
uses - because they are one-shot, cheap, and staleness would be a worse
bug than the one being fixed here. Aggregate/trend queries
(ticket_trend_by_requester) read cron/cw_contact_index.py's local SQLite
index instead: a 90-day live pull on every chat turn would be slow and
would risk tripping CW's rate limiter (see cw_client.py's
_MAX_ATTEMPTS_RATE_LIMIT comment for the incident that taught that
lesson), so that index is refreshed on its own schedule
(cron/scheduler.py's maybe_run_contact_index_refresh) and queried locally.

Every tool here surfaces a real end-user's name, email, and phone number -
FTC Safeguards/GLBA/Reg S-P data - so every successful lookup gets one INFO
audit line, mirroring tools/graph_schedule_tool.py's _audit_log.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from cron.cw_client import CWClient, CWError
from cron.cw_contact_index import contacts_matching, index_freshness, tickets_for_contact, trend_by_requester
from cron.trend_cluster_embed import DEFAULT_SIMILARITY_THRESHOLD
from cron.trend_vectors import EmbeddingError, cluster_by_similarity, embed_texts
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_CW_ENV_VARS = [
    "CW_MANAGE_COMPANY_ID", "CW_MANAGE_PUBLIC_KEY", "CW_MANAGE_PRIVATE_KEY",
    "CW_MANAGE_CLIENT_ID", "CW_MANAGE_BASE_URL",
]

_MAX_SEARCH_RESULTS = 25


class ContactLookupError(RuntimeError):
    """Expected, user-facing failure (bad input, no match, ambiguous match). Never a stack trace."""


def check_cw_contact_requirements() -> bool:
    import os

    return all(os.getenv(name) for name in _CW_ENV_VARS)


def _audit_log(tool_name: str, query: str, *, result_count: int, detail: str = "") -> None:
    """One INFO audit line per lookup that surfaced contact PII - see module
    docstring for why this is not optional in this environment."""
    logger.info(
        "cw_contact_tool: %s",
        tool_name,
        extra={
            "tool": tool_name,
            "query": query,
            "result_count": result_count,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def _cw_error_message(action: str, exc: CWError) -> str:
    if exc.status == 401 or exc.status == 403:
        return (
            f"Cannot {action}: ConnectWise denied this request (insufficient API member "
            f"permissions). Ask an admin to check the API member's security role. ({exc})"
        )
    if exc.status == 404:
        return f"Cannot {action}: ConnectWise returned not found."
    return f"Cannot {action}: ConnectWise API error: {exc}"


def _ticket_contact_summary(ticket: dict) -> dict[str, Any]:
    company = ticket.get("company") or {}
    return {
        "ticket_id": ticket.get("id"),
        "summary": ticket.get("summary"),
        "board": (ticket.get("board") or {}).get("name"),
        "status": (ticket.get("status") or {}).get("name"),
        "company": company.get("name"),
        "contact_name": ticket.get("contactName") or None,
        "contact_email": ticket.get("contactEmailAddress") or None,
        "contact_phone": ticket.get("contactPhoneNumber") or None,
    }


# ---------------------------------------------------------------------------
# Library functions -- return {"success": bool, "error": str, ...} rather
# than raising for expected failure modes, mirroring tools.graph_schedule_tool.
# ---------------------------------------------------------------------------


async def get_ticket_contact(ticket_number: str, *, client: Optional[CWClient] = None) -> dict[str, Any]:
    """Look up the contact/end-user on a single ConnectWise ticket, live."""
    ticket_number = str(ticket_number or "").strip()
    if not ticket_number or not ticket_number.isdigit():
        return {"success": False, "error": "ticket_number is required and must be a ConnectWise ticket number (digits only)."}

    cw = client or CWClient()
    try:
        ticket = cw.get(f"/service/tickets/{ticket_number}")
    except CWError as exc:
        if exc.status == 404:
            return {"success": False, "error": f"No ConnectWise ticket found with number {ticket_number}."}
        return {"success": False, "error": _cw_error_message(f"look up ticket {ticket_number}", exc)}

    summary = _ticket_contact_summary(ticket)
    _audit_log("get_ticket_contact", ticket_number, result_count=1, detail=summary.get("contact_email") or "")
    return {"success": True, **summary}


async def find_tickets_by_contact(contact: str, *, limit: int = 10, client: Optional[CWClient] = None) -> dict[str, Any]:
    """Find recent tickets for a contact by name (partial, case-insensitive)
    or email (exact). An email always resolves to at most one contact; a
    name that matches more than one distinct contact in the ConnectWise
    conditions filter is reported as ambiguous rather than mixed together.
    """
    contact = (contact or "").strip()
    if not contact:
        return {"success": False, "error": "contact is required (a contact's name or email address)."}
    limit = max(1, min(int(limit or 10), _MAX_SEARCH_RESULTS))

    cw = client or CWClient()
    escaped = contact.replace('"', '\\"')
    is_email = "@" in contact
    conditions = f'contactEmailAddress="{escaped}"' if is_email else f'contactName like "%{escaped}%"'

    try:
        tickets = cw.get("/service/tickets", conditions=conditions, pageSize=limit, orderBy="dateEntered desc")
    except CWError as exc:
        return {"success": False, "error": _cw_error_message(f"search tickets for '{contact}'", exc)}

    tickets = tickets or []
    if not tickets:
        return {"success": False, "error": f"No ConnectWise tickets found for contact matching '{contact}'."}

    if not is_email:
        distinct = {(t.get("contactName"), t.get("contactEmailAddress")) for t in tickets}
        if len(distinct) > 1:
            candidates = ", ".join(f"{name} <{email}>" for name, email in sorted(distinct) if name)
            return {
                "success": False,
                "error": (
                    f"'{contact}' matches more than one contact: {candidates}. "
                    "Use a full email address, or a more specific name, to disambiguate."
                ),
            }

    results = [_ticket_contact_summary(t) for t in tickets]
    _audit_log("find_tickets_by_contact", contact, result_count=len(results))
    return {"success": True, "contact_query": contact, "tickets": results}


async def ticket_trend_by_requester(*, days: int = 30, min_tickets: int = 2, limit: int = 20) -> dict[str, Any]:
    """Which contacts have filed the most tickets in the last `days`, from
    the local contact index (see cron/cw_contact_index.py). Flags the
    result as stale when the refresh job hasn't run recently, so a caller
    reading this in chat knows to trust it or not rather than silently
    getting a number based on old data."""
    days = max(1, int(days or 30))
    min_tickets = max(1, int(min_tickets or 2))
    limit = max(1, min(int(limit or 20), 100))

    freshness = index_freshness()
    if freshness["row_count"] == 0:
        return {
            "success": False,
            "error": (
                "The ConnectWise contact index is empty - it hasn't run its first refresh yet. "
                "It refreshes automatically every few hours; try again shortly."
            ),
        }

    rows = trend_by_requester(days=days, min_tickets=min_tickets, limit=limit)
    _audit_log("ticket_trend_by_requester", f"days={days}", result_count=len(rows))
    return {
        "success": True,
        "window_days": days,
        "requesters": rows,
        "index_stale": freshness["stale"],
        "index_last_refreshed_at": freshness["last_refreshed_at"],
    }


async def contact_semantic_pattern(
    contact: str, *, limit: int = 20, similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> dict[str, Any]:
    """Real semantic-similarity read on one contact's own ticket history: embeds
    each of their ticket summaries through cron/trend_vectors.py (the same
    embedding layer cron/trend_cluster_embed.py uses for org-wide trend
    detection - bge-m3 cosine similarity, not a keyword match) and clusters
    them, so a claim like "this is their third distinct flavor of Outlook
    trouble this month" is backed by a real semantic grouping of their own
    ticket text instead of a hand-typed guess. Reads the local contact index
    (see cron/cw_contact_index.py) - same source and staleness contract as
    ticket_trend_by_requester - never a live 90-day CW pull.

    This is the grounding tool SOUL.md's end-user roast budget requires before
    a specific end user becomes joke material in the internal IT Teams chat:
    a genuine cluster of 2+ semantically-similar tickets is a real pattern;
    an empty or single-ticket history is not, and this function says so
    plainly rather than inventing one.
    """
    contact = (contact or "").strip()
    if not contact:
        return {"success": False, "error": "contact is required (a contact's name or email address)."}
    limit = max(1, min(int(limit or 20), _MAX_SEARCH_RESULTS))

    freshness = index_freshness()
    if freshness["row_count"] == 0:
        return {
            "success": False,
            "error": (
                "The ConnectWise contact index is empty - it hasn't run its first refresh yet. "
                "It refreshes automatically every few hours; try again shortly."
            ),
        }

    is_email = "@" in contact
    rows = tickets_for_contact(
        contact_email=contact if is_email else None,
        contact_name=None if is_email else contact,
        limit=limit,
    )
    if not rows:
        return {
            "success": False,
            "error": f"No ConnectWise tickets found in the local index for contact matching '{contact}'.",
        }

    texts = [r["summary"] for r in rows if r.get("summary")]
    if len(texts) < 2:
        _audit_log("contact_semantic_pattern", contact, result_count=len(rows))
        return {
            "success": True,
            "contact_query": contact,
            "ticket_count": len(rows),
            "has_recurring_pattern": False,
            "pattern_summary": "Only one ticket on file with real text - not enough history for a real pattern.",
            "evidence_ticket_ids": [r["id"] for r in rows],
        }

    try:
        vectors = embed_texts(texts)
    except EmbeddingError as exc:
        return {"success": False, "error": f"Semantic pattern lookup unavailable: {exc}"}

    textual_rows = [r for r in rows if r.get("summary")]
    groups = cluster_by_similarity(textual_rows, vectors, threshold=similarity_threshold)
    largest = max(groups, key=len)
    has_pattern = len(largest) >= 2
    evidence = [textual_rows[i] for i in largest] if has_pattern else textual_rows[:1]

    _audit_log("contact_semantic_pattern", contact, result_count=len(rows))
    return {
        "success": True,
        "contact_query": contact,
        "ticket_count": len(rows),
        "has_recurring_pattern": has_pattern,
        "pattern_summary": (
            f"{len(largest)} of their {len(rows)} tickets semantically cluster together, "
            f"e.g. \"{evidence[0]['summary']}\"" if has_pattern
            else "Their tickets don't semantically cluster - each looks like a distinct, one-off issue."
        ),
        "evidence_ticket_ids": [e["id"] for e in evidence],
        "cluster_similarity_threshold": similarity_threshold,
    }


# ---------------------------------------------------------------------------
# Registry handlers -- thin argument parsing + JSON-string boundary.
# ---------------------------------------------------------------------------


async def _handle_get_ticket_contact(args: dict, **kw: Any) -> str:
    ticket_number = (args.get("ticket_number") or "").strip() if isinstance(args.get("ticket_number"), str) else str(args.get("ticket_number") or "")
    result = await get_ticket_contact(ticket_number)
    if not result["success"]:
        return tool_error(result["error"])
    return tool_result({k: v for k, v in result.items() if k != "success"})


async def _handle_find_tickets_by_contact(args: dict, **kw: Any) -> str:
    contact = (args.get("contact") or "").strip()
    if not contact:
        return tool_error("contact is required (a contact's name or email address).")
    limit = args.get("limit") or 10
    result = await find_tickets_by_contact(contact, limit=limit)
    if not result["success"]:
        return tool_error(result["error"])
    return tool_result({k: v for k, v in result.items() if k != "success"})


async def _handle_ticket_trend_by_requester(args: dict, **kw: Any) -> str:
    result = await ticket_trend_by_requester(
        days=args.get("days") or 30,
        min_tickets=args.get("min_tickets") or 2,
        limit=args.get("limit") or 20,
    )
    if not result["success"]:
        return tool_error(result["error"])
    return tool_result({k: v for k, v in result.items() if k != "success"})


async def _handle_contact_semantic_pattern(args: dict, **kw: Any) -> str:
    contact = (args.get("contact") or "").strip()
    if not contact:
        return tool_error("contact is required (a contact's name or email address).")
    limit = args.get("limit") or 20
    result = await contact_semantic_pattern(contact, limit=limit)
    if not result["success"]:
        return tool_error(result["error"])
    return tool_result({k: v for k, v in result.items() if k != "success"})


GET_TICKET_CONTACT_SCHEMA = {
    "name": "get_ticket_contact",
    "description": (
        "Look up the contact/end-user on a single ConnectWise ticket by ticket number: "
        "their name, email, phone, and the ticket's company/board/status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ticket_number": {
                "type": "string",
                "description": "The ConnectWise ticket number, e.g. '96886'.",
            }
        },
        "required": ["ticket_number"],
    },
}

FIND_TICKETS_BY_CONTACT_SCHEMA = {
    "name": "find_tickets_by_contact",
    "description": (
        "Find recent ConnectWise tickets filed by a specific contact/end-user, searched "
        "by name (partial match) or email address (exact match). If a partial name matches "
        "more than one distinct contact, reports the ambiguity instead of mixing their tickets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "contact": {
                "type": "string",
                "description": "The contact's name or email address, e.g. 'Erica Martin' or 'EMartin@HENSSLER.com'.",
            },
            "limit": {"type": "integer", "description": "Max tickets to return (default 10, max 25)."},
        },
        "required": ["contact"],
    },
}

TICKET_TREND_BY_REQUESTER_SCHEMA = {
    "name": "ticket_trend_by_requester",
    "description": (
        "Who has filed the most ConnectWise tickets over a recent window, from the local "
        "contact index (refreshed every few hours, not a live call). Use for trend/volume "
        "questions like 'who's been filing the most tickets this month', not for a single "
        "ticket's contact (use get_ticket_contact for that)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Rolling window size in days (default 30)."},
            "min_tickets": {"type": "integer", "description": "Minimum ticket count to include a requester (default 2)."},
            "limit": {"type": "integer", "description": "Max requesters to return (default 20, max 100)."},
        },
        "required": [],
    },
}

CONTACT_SEMANTIC_PATTERN_SCHEMA = {
    "name": "contact_semantic_pattern",
    "description": (
        "Semantic-similarity check on one contact/end-user's own ConnectWise ticket "
        "history: embeds their ticket summaries and clusters them by real similarity "
        "(not a keyword match) to say whether they have a genuine recurring pattern "
        "versus a set of one-off issues. Use this before making a specific end user "
        "roast/joke material in the internal IT Teams chat - it is the evidence bar, "
        "not a vibe check."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "contact": {
                "type": "string",
                "description": "The contact's name or email address, e.g. 'Erica Martin' or 'EMartin@HENSSLER.com'.",
            },
            "limit": {"type": "integer", "description": "Max tickets to consider (default 20, max 25)."},
        },
        "required": ["contact"],
    },
}


registry.register(
    name="get_ticket_contact",
    toolset="cw_contact",
    schema=GET_TICKET_CONTACT_SCHEMA,
    handler=_handle_get_ticket_contact,
    check_fn=check_cw_contact_requirements,
    requires_env=_CW_ENV_VARS,
    is_async=True,
    emoji="\U0001f464",
)

registry.register(
    name="find_tickets_by_contact",
    toolset="cw_contact",
    schema=FIND_TICKETS_BY_CONTACT_SCHEMA,
    handler=_handle_find_tickets_by_contact,
    check_fn=check_cw_contact_requirements,
    requires_env=_CW_ENV_VARS,
    is_async=True,
    emoji="\U0001f50d",
)

registry.register(
    name="ticket_trend_by_requester",
    toolset="cw_contact",
    schema=TICKET_TREND_BY_REQUESTER_SCHEMA,
    handler=_handle_ticket_trend_by_requester,
    check_fn=check_cw_contact_requirements,
    requires_env=_CW_ENV_VARS,
    is_async=True,
    emoji="\U0001f4c8",
)

registry.register(
    name="contact_semantic_pattern",
    toolset="cw_contact",
    schema=CONTACT_SEMANTIC_PATTERN_SCHEMA,
    handler=_handle_contact_semantic_pattern,
    check_fn=check_cw_contact_requirements,
    requires_env=_CW_ENV_VARS,
    is_async=True,
    emoji="\U0001f9e0",
)
