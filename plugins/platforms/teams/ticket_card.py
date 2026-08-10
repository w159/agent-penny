"""Python-owned Adaptive Card rendering for tickets, plus the one-ticket-per-
autonomous-message guarantee.

Cards used to arrive as model-authored JSON embedded in a ```adaptivecard```
fence, which meant a bad model turn could ship a malformed or oversized card.
This module builds the card CONTENT object in plain Python from a ticket
dict, so the shape is fixed and testable; the model only ever sees rendered
text, never card JSON it has to get right.

It also makes "one autonomous message covers exactly one ticket" a structural
property rather than a prompting convention: ``split_tickets_to_messages``
is the only path that turns N tickets into N sends, and
``guard_single_ticket_per_autonomous_message`` rejects any autonomous send
that still references more than one ticket number.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from gateway.platforms.base import AUTONOMOUS_DELIVERY_METADATA_KEY

_CARD_VERSION = "1.4"
_SUMMARY_MAX_CHARS = 160
_UNASSIGNED = "Unassigned"
_UNKNOWN = "Unknown"


class MultiTicketAutonomousError(Exception):
    """Raised when an autonomous (cron/unsolicited) message names 2+ tickets.

    Splitting N tickets into N sends (``split_tickets_to_messages``) only
    helps if nothing upstream can still glue several tickets back into one
    autonomous post. This is the guard that makes that structurally
    impossible instead of merely discouraged.
    """


def _one_line(text: Optional[str], max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """Collapse to a single line and cap length; never raises, never "None"."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return "(no summary)"
    if len(collapsed) > max_chars:
        return collapsed[: max_chars - 3].rstrip() + "..."
    return collapsed


_BACKTICK_RUN_RE = re.compile(r"``+")

# U+02CB MODIFIER LETTER GRAVE ACCENT — reads as a backtick, is not one, so a
# pasted code fence in a ticket cannot start or close a markdown fence.
_SAFE_BACKTICK = "ˋ"


def _neutralize_backtick_runs(text: str) -> str:
    """Make ticket text safe to place OUTSIDE a fence, without dropping it.

    Only runs of 2+ backticks are substituted, so ordinary inline code
    (`like this`) in a ticket summary still renders as the tech typed it.
    Card payloads do not need this — ``render_card_fence`` escapes backticks
    inside the JSON instead, which preserves them exactly.
    """
    return _BACKTICK_RUN_RE.sub(
        lambda match: _SAFE_BACKTICK * len(match.group(0)), str(text or "")
    )


def _field(ticket: Dict[str, Any], *keys: str, default: str = _UNKNOWN) -> str:
    """Pull the first present, non-empty value across a set of key aliases."""
    for key in keys:
        value = ticket.get(key)
        if value not in (None, ""):
            return str(value)
    return default


_PRIORITY_ATTENTION = {"high", "critical", "emergency"}
_PRIORITY_WARNING = {"medium"}


def _priority_color(priority: str) -> str:
    """Map a priority string to an Adaptive Card container/text color.

    Unrecognized priorities (including "Unknown") fall through to "default"
    rather than guessing — only known-urgent values get flagged.
    """
    normalized = priority.strip().lower()
    if normalized in _PRIORITY_ATTENTION:
        return "attention"
    if normalized in _PRIORITY_WARNING:
        return "warning"
    if normalized == "low":
        return "good"
    return "default"


def build_ticket_card(ticket: Dict[str, Any]) -> Dict[str, Any]:
    """Build the Adaptive Card CONTENT object for one ticket.

    Ticket number with a priority-colored indicator, a bold title line (the
    summary), and a FactSet of Priority/Status/Board/Owner plus an optional
    Age or Updated fact when the data is present. No narrative prose.
    Missing fields degrade to readable placeholders ("Unassigned",
    "Unknown") rather than raising or leaking a literal "None" into the
    card; fields that were simply never provided (age/updated) are omitted
    entirely instead of showing "Unknown".
    """
    ticket = ticket or {}
    number = _field(ticket, "number", "id", "ticket_number", default="?")
    priority = _field(ticket, "priority", default=_UNKNOWN)
    status = _field(ticket, "status", "stage", default=_UNKNOWN)
    board = _field(ticket, "board", default=_UNKNOWN)
    owner = _field(ticket, "owner", "assignee", "assigned_to", default=_UNASSIGNED)
    summary = _one_line(ticket.get("summary") or ticket.get("title"))
    url = ticket.get("url") or ticket.get("link")

    title_text = f"#{number}"
    fallback = (
        f"{title_text}: {summary} "
        f"(Priority: {priority}, Status: {status}, Board: {board}, Owner: {owner})"
    )

    facts = [
        {"title": "Priority", "value": priority},
        {"title": "Status", "value": status},
        {"title": "Board", "value": board},
        {"title": "Owner", "value": owner},
    ]
    updated = ticket.get("updated")
    if updated not in (None, ""):
        facts.append({"title": "Updated", "value": str(updated)})
    age = ticket.get("age")
    if age not in (None, ""):
        facts.append({"title": "Age", "value": str(age)})

    card: Dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": _CARD_VERSION,
        "fallbackText": fallback,
        "body": [
            {
                "type": "TextBlock",
                "text": title_text,
                "weight": "Bolder",
                "size": "Medium",
                "color": _priority_color(priority),
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": summary,
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Small",
            },
            {
                "type": "FactSet",
                "facts": facts,
                "separator": True,
                "spacing": "Medium",
            },
        ],
    }

    if url:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": "Open ticket", "url": str(url)}
        ]

    return card


def render_card_fence(card: Dict[str, Any]) -> str:
    """Wrap a card content object in the ```adaptivecard fence the adapter's
    ``_CARD_FENCE_RE`` (adapter.py) detects. Do not change the fence shape
    here without also checking that regex.

    This is the single chokepoint every card on every lane passes through
    (cron digest and Triage webhook alike), so it is where backticks get
    disarmed. ``json.dumps`` escapes quotes and backslashes but NOT
    backticks, so a pasted log or code snippet in a ConnectWise field used
    to close the fence early and spill the rest of the payload into Teams as
    raw text. Backticks can only ever appear inside JSON string literals —
    JSON's own syntax has none — so rewriting every one of them to the
    ``\\u0060`` escape is safe, keeps the payload valid JSON, leaves zero
    backticks for the fence to trip over, and round-trips through
    ``json.loads`` to the exact character the user typed.
    """
    payload = json.dumps(card, ensure_ascii=False).replace("`", "\\u0060")
    return "```adaptivecard\n" + payload + "\n```"


# ── ConnectWise Triage webhook lane ──────────────────────────────────────
# The cron lane above renders a board digest; the webhook lane renders a
# single freshly-created Triage ticket with a claim button. The two card
# bodies differ enough (heading container, Company/Contact facts, an
# Action.Execute claim button instead of an OpenUrl link) that sharing one
# builder would mean a pile of flags, so this is a second builder that
# reuses the sanitizing helpers and — importantly — the same
# ``render_card_fence``, which is the piece the adapter's regex is coupled to.

_TRIAGE_HEADING = "New Triage Ticket"
_TRIAGE_HEADING_BLOCKED = "Blocked - New Triage Ticket"
_TRIAGE_SUMMARY_MAX_CHARS = 100
_UNASSIGNED_MARKUP = "**UNASSIGNED**"

VERDICT_BLOCKED = "BLOCKED"
VERDICT_ROUTINE = "ROUTINE"
_VERDICTS = (VERDICT_BLOCKED, VERDICT_ROUTINE)

# Same shape as adapter.py's ``_CARD_FENCE_RE``. Kept as a local copy rather
# than imported because tests load the adapter under a different module name
# (``plugin_adapter_teams``), so an import here would bind to whichever copy
# happened to load first. Change one, check the other.
_FENCE_RE = re.compile(
    r"```(?:adaptivecard|adaptive[_-]?card)\b[ \t\r]*\n?"
    r"((?:(?!```(?:adaptivecard|adaptive[_-]?card)\b)[\s\S])*?)"
    r"```(?!(?:adaptivecard|adaptive[_-]?card)\b)",
    re.DOTALL | re.IGNORECASE,
)

# A verdict line is a bare token, optionally wrapped in the punctuation models
# reach for (``**BLOCKED**``, ``BLOCKED:``, ``[ROUTINE]``).
_VERDICT_STRIP = " \t*_`[]():.-"


def parse_verdict(content: str) -> tuple:
    """Split a model reply into ``(verdict, prose)``.

    The model's whole contract is: first line is ``BLOCKED`` or ``ROUTINE``,
    everything after it is prose. There is no syntax to get wrong, so there is
    nothing here that can fail — an unrecognized first line yields
    ``(None, content)`` and the caller leaves the message exactly as the model
    wrote it. This function never raises.
    """
    text = content if isinstance(content, str) else ""
    head, _, rest = text.partition("\n")
    token = head.strip().strip(_VERDICT_STRIP).upper()
    if token in _VERDICTS:
        return token, rest.strip()
    return None, text


def build_triage_card(ticket: Dict[str, Any], blocked: bool = False) -> Dict[str, Any]:
    """Build the Adaptive Card CONTENT object for one new Triage ticket.

    ``blocked`` comes from the model's verdict and drives the same styling the
    model used to choose for itself: an attention-styled container and heading
    when someone reads as unable to work, emphasis otherwise. Every value is
    stringified and length-capped here, so no field content can change the
    card's structure.
    """
    ticket = ticket or {}
    raw_number = _field(ticket, "ticket_id", "id", "number", default="?")
    summary = _one_line(ticket.get("summary"), _TRIAGE_SUMMARY_MAX_CHARS)
    company = _field(ticket, "company", default=_UNKNOWN)
    contact = _field(ticket, "contact", default=_UNKNOWN)
    priority = _field(ticket, "priority", default=_UNKNOWN)

    owner = _field(ticket, "owner", default="")
    if ticket.get("unassigned") or not owner.strip():
        owner = _UNASSIGNED_MARKUP

    url = ticket.get("url") or ""

    heading = _TRIAGE_HEADING_BLOCKED if blocked else _TRIAGE_HEADING
    style = "attention" if blocked else "emphasis"
    color = "attention" if blocked else "accent"

    title_text = f"#{raw_number} - {summary}"
    if url:
        title_text = f"[{title_text}]({url})"

    # The claim button carries the id CW expects. Numeric ids stay numeric so
    # the payload matches what the existing verb handler was built against.
    try:
        action_ticket_id: Any = int(str(raw_number))
    except (TypeError, ValueError):
        action_ticket_id = str(raw_number)

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": _CARD_VERSION,
        "body": [
            {
                "type": "Container",
                "style": style,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": heading,
                        "weight": "Bolder",
                        "size": "Small",
                        "color": color,
                        "spacing": "None",
                    },
                    {
                        "type": "TextBlock",
                        "text": title_text,
                        "wrap": True,
                        "weight": "Bolder",
                        "size": "Medium",
                    },
                ],
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Company", "value": company},
                    {"title": "Contact", "value": contact},
                    {"title": "Priority", "value": priority},
                    {"title": "Owner", "value": owner},
                ],
            },
        ],
        "actions": [
            {
                "type": "Action.Execute",
                "title": "I've got it",
                "verb": "penny_cw_assign",
                "data": {
                    "penny_action": "cw_assign",
                    "ticket_id": action_ticket_id,
                },
            }
        ],
    }


def _summary_fallback(ticket: Dict[str, Any]) -> str:
    """Non-empty stand-in prose when the model supplied none.

    ``_one_line`` already yields "(no summary)" for missing text, so this
    cannot return "". Backtick runs are neutralized because this text lands
    OUTSIDE the card fence, where a pasted code fence would open a stray
    markdown block.
    """
    return _neutralize_backtick_runs(_one_line((ticket or {}).get("summary")))


def render_triage_message(content: str, ticket: Dict[str, Any]) -> str:
    """Turn a verdict-prefixed model reply into the message Teams receives.

    The model supplies a verdict token and prose; ``json.dumps`` supplies the
    card. That split is the whole point: a syntax error in the card is no
    longer reachable, because the model never writes JSON.

    Degradation is deliberate and total. No recognized verdict (an interim
    status message, a model that forgot the contract) means the content passes
    through untouched and no card is attached — the pre-existing plain-text
    behavior. A non-``new_ticket`` event keeps its plain text too, matching
    what the closed/reopened/unknown lanes already did.

    What it never does is return nothing. Every exit runs through the same
    ticket-summary fallback, so a verdict-only reply (the model saying
    ``BLOCKED`` and no more) on a closed or reopened event delivers the
    summary rather than an empty message.
    """
    verdict, prose = parse_verdict(content)
    ticket = ticket or {}
    if verdict is None:
        # ``prose`` is the content verbatim for any string input, and a safe
        # empty string for the shapes a caller should never pass.
        return prose if prose.strip() else _summary_fallback(ticket)
    # Any fence the model emitted anyway is stripped: this path owns the card.
    prose = _FENCE_RE.sub("", prose).strip()

    # Before the event check, not after: the closed/reopened/unknown lanes
    # return early and would otherwise deliver an empty message.
    if not prose:
        prose = _summary_fallback(ticket)

    if str(ticket.get("event") or "").strip().lower() != "new_ticket":
        return prose

    # One ticket dict in, one card out — there is no loop here to produce a
    # second card, so "one ticket per message" holds structurally.
    fence = render_card_fence(build_triage_card(ticket, blocked=verdict == VERDICT_BLOCKED))
    return f"{prose}\n\n{fence}"


def split_tickets_to_messages(tickets: List[Dict[str, Any]]) -> List[str]:
    """Turn N tickets into exactly N fenced card messages, one ticket each.

    This is the function that makes "one ticket per message" true: nothing
    downstream can merge tickets back together once each has its own fence.
    """
    return [render_card_fence(build_ticket_card(ticket)) for ticket in (tickets or [])]


# Ticket numbers appear in rendered content as "#1234" (cards, and the
# markdown links autonomous sweeps used to emit). Matching that shape is
# enough to detect "this autonomous message still names more than one
# ticket" without parsing full card JSON.
_TICKET_NUMBER_RE = re.compile(r"#(\d+)")


def guard_single_ticket_per_autonomous_message(
    content: str, metadata: Optional[Dict[str, Any]]
) -> None:
    """Hard-reject an autonomous send that references 2+ distinct tickets.

    Scoped to ``metadata[AUTONOMOUS_DELIVERY_METADATA_KEY]`` being truthy,
    same key ``_cap_autonomous_message`` uses in adapter.py. Interactive
    replies are exempt on purpose: "show me the whole board" legitimately
    spans many tickets and must not be blocked.
    """
    if not (metadata or {}).get(AUTONOMOUS_DELIVERY_METADATA_KEY):
        return
    numbers = set(_TICKET_NUMBER_RE.findall(content or ""))
    if len(numbers) > 1:
        raise MultiTicketAutonomousError(
            f"autonomous message references {len(numbers)} distinct tickets "
            f"({sorted(numbers)}); use split_tickets_to_messages() to send "
            "one ticket per message instead"
        )
