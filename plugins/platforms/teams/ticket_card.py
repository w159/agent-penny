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

# 1.5 is what Action.Execute's Universal Action Model needs
# (learn.microsoft.com/en-us/adaptive-cards/authoring-cards/universal-action-model),
# used by the "I'VE GOT IT" claim button below. It is NOT needed for the fact
# grid: that used to be a Table element, which Teams mobile does not reliably
# render regardless of declared schema version (Teams mobile strips/garbles
# Table content; see learn.microsoft.com/en-us/microsoftteams/platform/
# task-modules-and-cards/cards/cards-reference), which is exactly what
# produced the go.skype.com/cards.unsupported fallback in production. The
# fact grid is built from ColumnSet/Column rows instead, the same pattern the
# trend card below already uses successfully on every client including
# mobile.
_CARD_VERSION = "1.5"
_SUMMARY_MAX_CHARS = 160
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

# U+02CB MODIFIER LETTER GRAVE ACCENT: reads as a backtick, is not one, so a
# pasted code fence in a ticket cannot start or close a markdown fence.
_SAFE_BACKTICK = "ˋ"


def _neutralize_backtick_runs(text: str) -> str:
    """Make ticket text safe to place OUTSIDE a fence, without dropping it.

    Only runs of 2+ backticks are substituted, so ordinary inline code
    (`like this`) in a ticket summary still renders as the tech typed it.
    Card payloads do not need this; ``render_card_fence`` escapes backticks
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


# ── Card layout: the four ConnectWise status variants ────────────────────
# Layout, icons, and accent colors are ported from the Power Automate cards
# in cw-manage-teams_cards/*.json, so what Hermes posts looks like what the
# board is already used to reading. The four designs differ ONLY in icon,
# accent color, and how many lines of the note they show, the structure
# (icon + title row, borderless fact table, note, action row) is identical,
# which is what makes one builder able to serve all four.
#
# Icons are hotlinked exactly as the source cards hotlink them. Teams fetches
# them client side; if a CDN is unreachable the image slot renders empty and
# the rest of the card is unaffected.

_VARIANT_NEW = "new"
_VARIANT_CLOSED = "closed"
_VARIANT_ONHOLD = "onhold"
_VARIANT_WAITING = "waiting"

_VARIANTS: Dict[str, Dict[str, Any]] = {
    _VARIANT_NEW: {
        "icon": "https://img.freepik.com/free-icon/baby_318-559348.jpg",
        "color": "Attention",
        "note_max_lines": 6,
    },
    _VARIANT_CLOSED: {
        "icon": "https://cdn-icons-png.flaticon.com/512/4929/4929385.png",
        "color": "Warning",
        "note_max_lines": 3,
    },
    _VARIANT_ONHOLD: {
        "icon": "https://cdn-icons-png.flaticon.com/512/5238/5238447.png",
        "color": "Accent",
        "note_max_lines": 3,
    },
    _VARIANT_WAITING: {
        "icon": "https://cdn-icons-png.flaticon.com/512/9794/9794979.png",
        "color": "Good",
        "note_max_lines": 3,
    },
}

# Every status on the live Triage board that means the ticket is finished.
# Several carry a trailing asterisk (a board naming convention, not part of
# the name), so the normalizer strips it before the lookup.
_CLOSED_STATUSES = frozenset(
    {"closed", "resolved", "completed", "cancelled", "canceled", "close pending"}
)
_ONHOLD_STATUSES = frozenset({"on-hold", "on hold", "onhold"})

# Everything else the board actually produces, Re-Opened, In Progress,
# In Progress (Silent), Customer Updated, is an ACTIVE ticket, and an active
# ticket needs eyes, so it renders as "new" rather than getting a quiet color.
_DEFAULT_VARIANT = _VARIANT_NEW


def _normalize_status(status: Any) -> str:
    return " ".join(str(status or "").strip().rstrip("*").split()).lower()


def select_card_variant(ticket: Dict[str, Any]) -> str:
    """Choose which of the four card designs a ticket renders as.

    Creation wins over status: a ticket opens in status "New", but a ticket
    reopened into an active status is still not new, so the event is checked
    first and the status only decides for everything after that. "waiting" is
    matched as a substring because the board carries three of them (Waiting
    Client Response*, Waiting 3rd Party, Waiting parts/repair) and will
    happily grow a fourth without telling anyone.
    """
    ticket = ticket or {}
    event = str(ticket.get("event") or "").strip().lower()
    status = _normalize_status(ticket.get("status") or ticket.get("stage"))
    if event in ("new_ticket", "new", "created"):
        return _VARIANT_NEW
    if "waiting" in status:
        return _VARIANT_WAITING
    if status in _ONHOLD_STATUSES:
        return _VARIANT_ONHOLD
    if ticket.get("closed_flag") or status in _CLOSED_STATUSES or event == "closed":
        return _VARIANT_CLOSED
    return _DEFAULT_VARIANT


_ET_ZONE = "America/New_York"


def _eastern_timestamp(value: Any) -> str:
    """Format a CW UTC timestamp as Eastern MM/DD/YYYY HH:MM.

    CW sends ``2026-08-14T15:54:30Z``. Anything that will not parse is
    returned as-is rather than dropped: a timestamp the reader can squint at
    beats a row that silently vanished.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo(_ET_ZONE)).strftime("%m/%d/%Y %H:%M")
    except Exception:
        return raw


def _fact_row(label: str, value: str, *, color: Optional[str] = None) -> Dict[str, Any]:
    """One borderless label/value row of the fact grid, built from ColumnSet.

    This used to be a TableRow inside a Table element. Table needs schema
    1.5+ and, even declared at 1.5, Teams mobile does not reliably render it
    (learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/
    cards/cards-reference) - that is what produced the go.skype.com/
    cards.unsupported fallback in production. ColumnSet with a fixed-width
    label column and a stretch value column renders the same two-column look
    on every client, the same pattern ``_label_value_row`` already uses for
    the trend card below.
    """
    return {
        "type": "ColumnSet",
        "spacing": "None",
        "columns": [
            {
                "type": "Column",
                "width": 1,
                "verticalContentAlignment": "Center",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": label,
                        "wrap": True,
                        "weight": "Bolder",
                        "size": "Small",
                        **({"color": color} if color else {}),
                    }
                ],
            },
            {
                "type": "Column",
                "width": 2,
                "verticalContentAlignment": "Center",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": value,
                        "wrap": True,
                        "size": "Small",
                        **({"color": color} if color else {}),
                    }
                ],
            },
        ],
    }


def build_ticket_card(ticket: Dict[str, Any]) -> Dict[str, Any]:
    """Build the Adaptive Card CONTENT object for one ticket.

    An icon + "Ticket #id : summary" heading row, a borderless two-column
    table of the facts that are actually known, the (image-stripped) issue
    note, and a VIEW TICKET button, styled by ``select_card_variant``.

    Rows whose data the caller does not have are omitted rather than filled
    with "Unknown": the board-watch cron carries no note, timestamp, or
    updated-by, and printing placeholders for three quarters of the table
    would make an incomplete card look like a complete one. Status and
    Priority always render, because a ticket card with neither is not worth
    posting.
    """
    ticket = ticket or {}
    number = _field(ticket, "number", "id", "ticket_id", "ticket_number", default="?")
    summary = _one_line(ticket.get("summary") or ticket.get("title"))
    status = _field(ticket, "status", "stage", default=_UNKNOWN)
    priority = _field(ticket, "priority", default=_UNKNOWN)

    variant = _VARIANTS[select_card_variant(ticket)]
    color = variant["color"]

    title_text = f"Ticket #{number} : {summary}"
    if ticket.get("blocked"):
        title_text = "BLOCKED - " + title_text

    rows = [_fact_row(status, priority, color=color)]

    contact = _field(ticket, "contact", default="").strip()
    if contact:
        rows.append(_fact_row("Contact", contact))

    # "No owner field" and "owner field that came back empty" are different
    # facts. Only the second one is grounds for saying UNASSIGNED or offering
    # the claim button, the cron lane has no assignee data at all, and
    # inferring "nobody owns this" from its silence would put a claim button
    # on a ticket someone is already working.
    owner = _field(ticket, "owner", "assignee", "assigned_to", default="").strip()
    owner_known = any(k in ticket for k in ("owner", "assignee", "assigned_to", "unassigned"))
    unassigned = owner_known and (
        bool(ticket.get("unassigned")) or owner.lower() in ("", "none", "null")
    )
    if owner_known:
        rows.append(_fact_row("Owner", _UNASSIGNED_MARKUP if unassigned else owner))

    last_updated = _eastern_timestamp(ticket.get("last_updated") or ticket.get("updated"))
    if last_updated:
        rows.append(_fact_row("Last Updated", last_updated))

    updated_by = _field(ticket, "updated_by", default="").strip()
    if updated_by:
        rows.append(_fact_row("Updated By", updated_by))

    body: List[Dict[str, Any]] = [
        {
            "type": "ColumnSet",
            "spacing": "Small",
            "columns": [
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [
                        {
                            "type": "ImageSet",
                            "images": [
                                {
                                    "type": "Image",
                                    "size": "Medium",
                                    "url": variant["icon"],
                                }
                            ],
                        }
                    ],
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "spacing": "Small",
                    "verticalContentAlignment": "Center",
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": title_text,
                            "size": "Medium",
                            "weight": "Bolder",
                            "wrap": True,
                            "maxLines": 2,
                            "color": color,
                            "style": "heading",
                        }
                    ],
                },
            ],
        },
        {
            "type": "Container",
            "items": rows,
            "spacing": "Small",
            "separator": True,
        },
    ]

    # The note block is dropped entirely when the caller has no note field at
    # all (the cron lane), and kept with its placeholder when the caller does
    # have one and CW returned nothing, "this ticket has no note" is itself
    # worth knowing on a live callback.
    issue = _issue_text(ticket) if _carries_issue_field(ticket) else ""
    if issue:
        body.append(
            {
                "type": "TextBlock",
                "text": issue,
                "wrap": True,
                "size": "Small",
                "maxLines": variant["note_max_lines"],
                "separator": True,
            }
        )

    actions = _ticket_actions(ticket, number, unassigned)
    if actions:
        body.append(
            {
                "type": "ActionSet",
                "actions": actions,
                "spacing": "ExtraLarge",
                "separator": True,
            }
        )

    return {
        "type": "AdaptiveCard",
        "$schema": "https://adaptivecards.io/schemas/adaptive-card.json",
        "version": _CARD_VERSION,
        "targetWidth": "Wide",
        "fallbackText": f"{title_text} ({status}, {priority})",
        "body": body,
    }


_CW_TICKET_URL = (
    "https://na.myconnectwise.net/v4_6_release/services/system_io/Service/"
    "fv_sr100_request.rails?service_recid={number}"
)


def _ticket_actions(
    ticket: Dict[str, Any], number: str, unassigned: bool
) -> List[Dict[str, Any]]:
    """VIEW TICKET, plus the claim button when the ticket is open and unowned.

    The claim button predates this layout and still has a live verb handler
    behind it, so it moves into the ActionSet rather than disappearing with
    the old card. Claiming a ticket that is closed or already owned would
    just be a way to take work off someone, so neither gets the button.
    """
    actions: List[Dict[str, Any]] = []
    url = str(ticket.get("url") or ticket.get("link") or "").strip()
    if not url and number != "?":
        url = _CW_TICKET_URL.format(number=number)
    if url:
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": "VIEW TICKET",
                "url": url,
                "style": "positive",
            }
        )

    if unassigned and select_card_variant(ticket) != _VARIANT_CLOSED:
        # Numeric ids stay numeric so the payload matches what the existing
        # verb handler was built against.
        try:
            action_ticket_id: Any = int(str(number))
        except (TypeError, ValueError):
            action_ticket_id = str(number)
        actions.append(
            {
                "type": "Action.Execute",
                "title": "I'VE GOT IT",
                "verb": "penny_cw_assign",
                "data": {"penny_action": "cw_assign", "ticket_id": action_ticket_id},
            }
        )
    return actions


def render_card_fence(card: Dict[str, Any]) -> str:
    """Wrap a card content object in the ```adaptivecard fence the adapter's
    ``_CARD_FENCE_RE`` (adapter.py) detects. Do not change the fence shape
    here without also checking that regex.

    This is the single chokepoint every card on every lane passes through
    (cron digest and Triage webhook alike), so it is where backticks get
    disarmed. ``json.dumps`` escapes quotes and backslashes but NOT
    backticks, so a pasted log or code snippet in a ConnectWise field used
    to close the fence early and spill the rest of the payload into Teams as
    raw text. Backticks can only ever appear inside JSON string literals
    (JSON's own syntax has none), so rewriting every one of them to the
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
# reuses the sanitizing helpers and, importantly, the same
# ``render_card_fence``, which is the piece the adapter's regex is coupled to.

_TRIAGE_SUMMARY_MAX_CHARS = 100
_UNASSIGNED_MARKUP = "**UNASSIGNED**"

# The issue note is the reason the ticket exists, so it is the one field that
# must never be missing. It is also the only free-form field, so it is the one
# that needs a cap: CW notes can carry a whole email thread.
_ISSUE_MAX_CHARS = 300
_ISSUE_MISSING = "(no issue note on the ticket)"

_BLANK_LINE_RUN_RE = re.compile(r"\n\s*\n\s*\n+")

# ConnectWise embeds inline screenshots as markdown image markup, and the alt
# text is not one shape but three: a nested bracket
# (``![[Image.png]](https://na.myconnectwise.net/.../inlineimage...)``), a
# BACKSLASH-ESCAPED bracket (``![\[Auvik SaaS Management Logo\]](url)``, which
# is what email-sourced tickets from Auvik, Microsoft, and Okta actually
# carry, 551 of the 553 inline images in the last 353 issue notes), or a
# plain ``![alt](url)``. A lazy alt-text run ending at the ``](`` boundary
# covers all three without enumerating them. Both halves are newline-bounded
# so a stray ``![`` cannot swallow the rest of the note.
_MD_IMAGE_RE = re.compile(r"!\[[^\n]*?\]\([^)\n]*\)")
# A URL alone on its own line, what is left over once a markdown wrapper has
# already been stripped, or a raw link a CW integration pasted in isolation.
# Requiring the whole line be the URL keeps this from touching a URL that is
# part of a sentence.
_BARE_URL_LINE_RE = re.compile(r"^[ \t]*(?:https?://|www\.)\S+[ \t]*$", re.MULTILINE)


def _strip_inline_images(text: str) -> str:
    """Remove markdown image markup and orphaned bare image URLs.

    Never raises: ``text`` is expected to already be a ``str`` (callers
    coerce with ``str(...)`` first), and both patterns above degrade to a
    no-op on input that does not match.
    """
    stripped = _MD_IMAGE_RE.sub("", text)
    return _BARE_URL_LINE_RE.sub("", stripped)


_ISSUE_KEYS = ("issue", "initial_description")


def _carries_issue_field(ticket: Dict[str, Any]) -> bool:
    """Whether the caller is in a position to know the ticket's issue note.

    Presence of the key, not truth of the value: the webhook lane always sets
    ``issue`` (empty when CW returned nothing, which is worth showing), while
    the cron lane has no access to notes at all and should not imply it does.
    """
    return any(key in (ticket or {}) for key in _ISSUE_KEYS)


def _issue_text(ticket: Dict[str, Any]) -> str:
    """The ticket's detail-description note, capped, newlines preserved.

    Unlike the summary this is NOT collapsed to one line: an issue note is
    frequently a short paragraph or a pasted alert body, and flattening it
    costs the reader the structure the submitter typed. Inline images
    (``![[Image.png]](url)`` and similar) are stripped before the length cap
    is applied, so the cap counts prose rather than a spent-out URL, and any
    blank-line run the stripping leaves behind is squeezed along with the
    ones already in the source, so a signature block or a removed image
    cannot push the facts off screen.
    """
    raw = ticket.get("issue") or ticket.get("initial_description") or ""
    text = str(raw).replace("\r\n", "\n")
    text = _strip_inline_images(text)
    text = _BLANK_LINE_RUN_RE.sub("\n\n", text).strip()
    if not text:
        return _ISSUE_MISSING
    if len(text) > _ISSUE_MAX_CHARS:
        return text[: _ISSUE_MAX_CHARS - 3].rstrip() + "..."
    return text


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
    nothing here that can fail: an unrecognized first line yields
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
    """The webhook lane's card: the shared ticket card, plus the verdict.

    Both lanes render the same four designs now, so this is no longer a
    second card builder, it is the shared one with the model's ``blocked``
    verdict folded in. ``blocked`` cannot remove or reorder a field; it only
    prefixes the title, so a model turn can never change the card's shape.

    The summary is capped tighter here than on the cron lane because a live
    callback's summary is the reader's first line and a two-line heading
    pushes the facts down.
    """
    ticket = dict(ticket or {})
    ticket["summary"] = _one_line(ticket.get("summary"), _TRIAGE_SUMMARY_MAX_CHARS)
    if blocked:
        ticket["blocked"] = True
    return build_ticket_card(ticket)


def render_triage_message(content: str, ticket: Dict[str, Any]) -> str:
    """Turn a model reply into the message Teams receives: one card, only.

    ``json.dumps`` supplies the card unconditionally, and that is the fix for
    what shipped before:

    - Every delivered callback gets a card, whatever its event class. Reopened
      and closed events used to return bare prose, so what a reader saw
      depended entirely on which sentence the model chose to write.
    - The card is attached whether or not the model produced a verdict. Across
      229 stored callbacks the verdict line was present twice; on the other
      path the reply passed through verbatim, which is how model-authored card
      JSON, 7 of 22 of it unparseable, reached the channel as raw text.

    The model's reply is only ever consulted for its verdict token, which
    picks the ``blocked`` styling and nothing else; a missing or unrecognized
    one means ROUTINE, not "structure is optional". None of the model's prose
    is ever rendered, so there is nothing for it to spill as raw text and
    nothing beside the card to make one ticket ring twice, the return value
    is the fenced card and nothing else.
    """
    verdict, _ = parse_verdict(content)
    ticket = ticket or {}

    # One ticket dict in, one card out; there is no loop here to produce a
    # second card, so "one ticket per message" holds structurally.
    return render_card_fence(build_triage_card(ticket, blocked=verdict == VERDICT_BLOCKED))


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


# ── Cross-ticket trend card ──────────────────────────────────────────────
# A trend is not a ticket: it is a claim that several tickets, which do not
# look related on the board, share one root cause. The card exists to carry
# that claim (``why_related``) and the evidence for it, not to restate any
# one ticket's facts, which is why it reuses the sanitizing helpers above but
# not ``build_ticket_card``'s layout.

_CW_BOARD_URL = "https://na.myconnectwise.net/v4_6_release/services/system_io/Service/fv_sr100_boardview.rails"

_TREND_MEMBER_ROWS_MAX = 6
_TREND_DEVICES_MAX = 8

# Teams mobile only renders Adaptive Card schema up to 1.2, and the Table
# element needs 1.5+ (learn.microsoft.com/en-us/microsoftteams/platform/
# task-modules-and-cards/cards/cards-reference); Microsoft's own card design
# guidance says to build tabular layout with ColumnSet instead
# (.../cards/design-effective-cards, "Column layouts"). The trend card is
# meant for the Teams group chat, mobile included, so it uses ColumnSet/
# Column rows rather than the Table element ``build_ticket_card`` uses.

# The reasoning behind a trend is the whole point of the card, so it gets a
# far higher cap than a single ticket's issue note (_ISSUE_MAX_CHARS), just
# enough of a ceiling that a runaway model turn cannot post a wall of text.
_TREND_WHY_MAX_CHARS = 1200

# Escalation reads as urgency, not as a fifth color the reader has to learn:
# 0-1 uses the same palette as an active ticket, 2+ tips into "Attention" red
# and says so in words, because a trend nobody acknowledged twice over is the
# one case this card exists to make impossible to skim past.
_TREND_CONFIDENCE_COLOR = {"high": "Attention", "medium": "Warning"}
_TREND_DEFAULT_COLOR = "Accent"


def _trend_field(trend: Dict[str, Any], key: str, default: str = _UNKNOWN) -> str:
    value = (trend or {}).get(key)
    return str(value) if value not in (None, "") else default


def _trend_clean(text: Any) -> str:
    """Route trend free text through the same sanitizing pipeline as a ticket."""
    return _neutralize_backtick_runs(_strip_inline_images(str(text or "")))


def _trend_color(trend: Dict[str, Any]) -> str:
    escalation = trend.get("escalation_level")
    try:
        escalation = int(escalation)
    except (TypeError, ValueError):
        escalation = 0
    if escalation >= 2:
        return "Attention"
    confidence = str(trend.get("confidence") or "").strip().lower()
    return _TREND_CONFIDENCE_COLOR.get(confidence, _TREND_DEFAULT_COLOR)


def _label_value_row(
    label: str, value: str, *, wrap_label: bool = True, color: Optional[str] = None
) -> Dict[str, Any]:
    """One narrow-label / wide-wrapping-value row, built from ColumnSet.

    ``build_ticket_card`` uses the Table element for this same shape, but
    Table needs schema 1.5+ and Teams mobile only renders up to 1.2, so the
    trend card (posted into a group chat, mobile included) uses ColumnSet
    instead: "auto" for the label, "stretch" for the value, exactly what
    Microsoft's own card design guidance recommends for tabular layout.
    ``wrap_label`` is off for the member rows, where the "#id (date)" label
    must stay compact rather than wrap onto a second line.
    """
    return {
        "type": "ColumnSet",
        "spacing": "None",
        "columns": [
            {
                "type": "Column",
                "width": "auto",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": label,
                        "wrap": wrap_label,
                        "weight": "Bolder",
                        "size": "Small",
                        **({"color": color} if color else {}),
                    }
                ],
            },
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": value,
                        "wrap": True,
                        "size": "Small",
                        **({"color": color} if color else {}),
                    }
                ],
            },
        ],
    }


def _trend_member_rows(trend: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Up to 6 member-ticket rows, then one roll-up row, never a silent drop."""
    tickets = trend.get("tickets") or []
    if not isinstance(tickets, list):
        tickets = []
    rows: List[Dict[str, Any]] = []
    shown = tickets[:_TREND_MEMBER_ROWS_MAX]
    for member in shown:
        member = member or {}
        ticket_id = _trend_field(member, "id", default="?")
        date = _trend_field(member, "date", default="")
        summary = _one_line(_trend_clean(member.get("summary")))
        label = f"#{ticket_id}" + (f" ({date})" if date else "")
        rows.append(_label_value_row(label, summary, wrap_label=False))
    remaining = len(tickets) - len(shown)
    if remaining > 0:
        rows.append(_label_value_row("", f"+{remaining} more", wrap_label=False))
    return rows


def _trend_devices_text(trend: Dict[str, Any]) -> str:
    devices = trend.get("devices") or []
    if not isinstance(devices, list) or not devices:
        return ""
    shown = [str(d) for d in devices[:_TREND_DEVICES_MAX]]
    remaining = len(devices) - len(shown)
    text = ", ".join(shown)
    if remaining > 0:
        text += f" (+{remaining} more)"
    return text


def _trend_strongest_evidence(trend: Dict[str, Any]) -> str:
    """The single evidence quote that carries the most weight, attributed.

    "Strongest" here means "first with an evidence field": the caller
    already ordered ``tickets`` newest first, and picking anything smarter
    would require judging quote quality, which is a job for whatever built
    the trend, not for this renderer.
    """
    tickets = trend.get("tickets") or []
    if not isinstance(tickets, list):
        return ""
    for member in tickets:
        member = member or {}
        evidence = _trend_clean(member.get("evidence"))
        if evidence.strip():
            ticket_id = _trend_field(member, "id", default="?")
            return f'#{ticket_id}: "{_one_line(evidence, _ISSUE_MAX_CHARS)}"'
    return ""


def _build_mention_entities(mention_upns: Optional[List[str]]) -> List[Dict[str, Any]]:
    """One Teams ``mention`` entity per UPN, the documented user-mention shape.

    Display name is the UPN's local part -- simple and Graph-lookup-free,
    per the owner's instruction. A malformed entry with no "@" falls back
    to using the whole string as both id and name rather than raising, so
    one bad UPN in config does not take down the rest of the card.
    """
    entities: List[Dict[str, Any]] = []
    for upn in mention_upns or []:
        name = str(upn).split("@", 1)[0]
        entities.append(
            {
                "type": "mention",
                "text": f"<at>{name}</at>",
                "mentioned": {"id": upn, "name": name},
            }
        )
    return entities


def build_trend_card(
    trend: Dict[str, Any],
    *,
    kind: Optional[str] = None,
    hours_since_raised: Optional[float] = None,
    mention_upns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build the Adaptive Card CONTENT object for one cross-ticket trend.

    A trend card is not a ticket card wearing a different color: it has to
    make the case that a set of tickets, which look unrelated in a board
    scan, share one root cause. So the spread line (counts + date range) and
    ``why_related`` carry the weight here, not a per-field fact table.
    Missing or malformed keys degrade to ``_UNKNOWN`` or an omitted row,
    same contract as ``build_ticket_card``: a trend that fails to build
    never reaches a human, so this function must not raise.

    ``kind`` labels what raised this card -- "new", "update", or
    "escalation" -- matching the ``kind`` field trend_escalation.py's
    ``_build_alert`` puts on the alert dict. Left at its default of
    ``None``, the card renders exactly as it did before this label existed
    (existing callers/tests are unaffected).

    ``mention_upns`` attaches one Teams ``mention`` entity per UPN, which
    IS the documented, notifying shape (Format cards in Teams > Mention
    support within Adaptive Cards: "Bots support user mention with the
    Microsoft Entra Object ID and UPN ... The user gets activity feed
    notification when being @mentioned with the IDs"). Channel/team
    mentions are explicitly NOT supported in bot messages per the same
    page, which is why this takes UPNs, not a channel id. Each UPN's
    display name is derived from its local part (no Graph lookup) and its
    ``<at>Name</at>`` token is folded into the title TextBlock, since
    mentions are only supported inside TextBlock/FactSet elements. Leaving
    ``mention_upns`` at its default of ``None`` (or passing an empty list)
    leaves the card byte-identical to before.
    """
    trend = trend or {}
    trend_id = _trend_field(trend, "trend_id", default="?")
    title = _one_line(_trend_clean(trend.get("title")))
    confidence = _trend_field(trend, "confidence", default=_UNKNOWN)
    first_seen = _trend_field(trend, "first_seen", default=_UNKNOWN)
    last_seen = _trend_field(trend, "last_seen", default=_UNKNOWN)
    ticket_count = _trend_field(trend, "ticket_count", default="0")
    device_count = _trend_field(trend, "device_count", default="0")
    user_count = _trend_field(trend, "user_count", default="0")
    escalation = trend.get("escalation_level")
    try:
        escalation = int(escalation)
    except (TypeError, ValueError):
        escalation = 0

    color = _trend_color(trend)
    title_text = f"TREND {trend_id} : {title}"
    if kind == "new":
        title_text = "NEW TREND - " + title_text
    elif kind == "update":
        title_text = "TREND UPDATE - BLAST RADIUS GREW - " + title_text
    elif kind == "escalation":
        label = (
            f"UNACKNOWLEDGED TREND ({int(hours_since_raised)}h)"
            if hours_since_raised is not None
            else "UNACKNOWLEDGED TREND"
        )
        title_text = f"{label} - " + title_text
    elif escalation >= 2:
        # No explicit kind (older callers): fall back to the original
        # escalation-count prefix so existing behavior is preserved.
        title_text = f"UNACKNOWLEDGED (x{escalation}) - " + title_text

    mention_entities = _build_mention_entities(mention_upns)
    if mention_entities:
        tokens = " ".join(e["text"] for e in mention_entities)
        title_text = f"{tokens} " + title_text

    spread_text = (
        f"{ticket_count} tickets - {device_count} devices - {user_count} users - "
        f"{first_seen} to {last_seen} - confidence {confidence}"
    )

    techs = trend.get("techs") or []
    techs_text = ", ".join(str(t) for t in techs) if isinstance(techs, list) else ""

    rows = [_label_value_row("Spread", spread_text, color=color)]
    if techs_text:
        rows.append(_label_value_row("Techs", techs_text))

    devices_text = _trend_devices_text(trend)
    if devices_text:
        rows.append(_label_value_row("Devices", devices_text))

    body: List[Dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": title_text,
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
            "maxLines": 3,
            "color": color,
            "style": "heading",
        },
        {
            "type": "Container",
            "id": "trend-spread",
            "items": rows,
            "spacing": "Small",
            "separator": True,
        },
    ]

    # Bypass the ticket-note cap here on purpose: why_related is the reason
    # the card exists, not a side field, so it wraps in full up to its own
    # much higher ceiling instead of getting cut off mid-sentence.
    why_related = _one_line(_trend_clean(trend.get("why_related")), _TREND_WHY_MAX_CHARS)
    if trend.get("why_related"):
        body.append(
            {
                "type": "TextBlock",
                "text": "Why these are related: " + why_related,
                "wrap": True,
                "size": "Small",
                "weight": "Bolder",
                "separator": True,
            }
        )

    member_rows = _trend_member_rows(trend)
    if member_rows:
        body.append(
            {
                "type": "Container",
                "id": "trend-members",
                "items": member_rows,
                "spacing": "Small",
                "separator": True,
            }
        )

    evidence_text = _trend_strongest_evidence(trend)
    if evidence_text:
        body.append(
            {
                "type": "TextBlock",
                "text": "Strongest evidence - " + evidence_text,
                "wrap": True,
                "size": "Small",
                "isSubtle": True,
                "separator": True,
            }
        )

    recommended_action = _trend_clean(trend.get("recommended_action"))
    if trend.get("recommended_action"):
        body.append(
            {
                "type": "TextBlock",
                "text": "Recommended action: " + _one_line(recommended_action, _ISSUE_MAX_CHARS),
                "wrap": True,
                "size": "Small",
                "separator": True,
            }
        )

    actions = [
        {
            "type": "Action.Submit",
            "title": "ACKNOWLEDGE TREND",
            "data": {"action": "ack_trend", "trend_id": trend_id},
        },
        {
            "type": "Action.OpenUrl",
            "title": "VIEW BOARD",
            "url": _CW_BOARD_URL,
        },
    ]
    body.append(
        {
            "type": "ActionSet",
            "actions": actions,
            "spacing": "ExtraLarge",
            "separator": True,
        }
    )

    fallback = (
        f"Trend {trend_id}: {title} ({ticket_count} tickets, {device_count} devices, "
        f"{user_count} users, {first_seen} to {last_seen})"
    )

    card: Dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "https://adaptivecards.io/schemas/adaptive-card.json",
        "version": _CARD_VERSION,
        "targetWidth": "Wide",
        "fallbackText": fallback,
        "body": body,
    }
    if mention_entities:
        card["msteams"] = {"entities": mention_entities}
    return card


# Opt-in metadata key a job can set to raise the default one-ticket ceiling
# below. Absent/1 preserves today's strict behavior for anything that has
# not explicitly reviewed and approved a higher count, e.g. triage-nag,
# whose whole design is one ticket per message. Jobs like the Triage board
# sweep, whose prompt (and SOUL.md's MESSAGE SIZE rule) explicitly allows up
# to 6 tickets plus a reconciling roll-up line in ONE message, set this so a
# compliant message is not rejected here and shipped instead through a
# fallback path that mangles it (see cron/scheduler.py's standalone fallback
# and the 2026-09-04 Madison Todd incident: a correct 2203-char, 6-ticket
# message was rejected by this guard, fell back to standalone delivery,
# which lost the autonomous marker, and got trimmed to 1177 chars with a
# generic "+3 more tickets not shown" tail replacing the model's own
# reconciling summary).
AUTONOMOUS_TICKET_LIMIT_METADATA_KEY = "autonomous_ticket_limit"
_DEFAULT_AUTONOMOUS_TICKET_LIMIT = 1


def guard_single_ticket_per_autonomous_message(
    content: str, metadata: Optional[Dict[str, Any]]
) -> None:
    """Hard-reject an autonomous send that names more tickets than the
    caller's declared limit (default 1) allows.

    Scoped to ``metadata[AUTONOMOUS_DELIVERY_METADATA_KEY]`` being truthy,
    same key ``_cap_autonomous_message`` uses in adapter.py. Interactive
    replies are exempt on purpose: "show me the whole board" legitimately
    spans many tickets and must not be blocked.

    The limit comes from ``metadata[AUTONOMOUS_TICKET_LIMIT_METADATA_KEY]``
    so it is a per-job, reviewed decision rather than one global number that
    either blocks a legitimate multi-ticket design (the board sweep) or lets
    a design that must stay one-ticket-per-message (triage-nag) drift.
    """
    metadata = metadata or {}
    if not metadata.get(AUTONOMOUS_DELIVERY_METADATA_KEY):
        return
    try:
        limit = int(metadata.get(AUTONOMOUS_TICKET_LIMIT_METADATA_KEY) or _DEFAULT_AUTONOMOUS_TICKET_LIMIT)
    except (TypeError, ValueError):
        limit = _DEFAULT_AUTONOMOUS_TICKET_LIMIT
    if limit < 1:
        limit = _DEFAULT_AUTONOMOUS_TICKET_LIMIT
    numbers = set(_TICKET_NUMBER_RE.findall(content or ""))
    if len(numbers) > limit:
        raise MultiTicketAutonomousError(
            f"autonomous message references {len(numbers)} distinct tickets "
            f"({sorted(numbers)}), over this job's limit of {limit}; use "
            "split_tickets_to_messages() to send one ticket per message, or "
            "raise autonomous_ticket_limit in the job's delivery metadata if "
            "this many tickets in one message is reviewed and intended"
        )


# Plain-text blocks are blank-line separated -- the same convention
# gateway/platforms/base.py's _ITEM_SEPARATOR_RE reads on the trim side.
# Kept as a local, simpler copy (no tab handling) rather than imported:
# this module already keeps _CARD_FENCE_RE local to adapter.py for the same
# reason (see that constant's comment) -- one regex line duplicated is
# cheaper than a cross-module private import.
_BLANK_LINE_SPLIT_RE = re.compile(r"\n\s*\n")


def split_autonomous_message_by_ticket(content: str) -> Optional[List[str]]:
    """Split one oversized autonomous message into one message per ticket.

    This is what makes "one ticket per message" a real guarantee instead of
    a prompting convention the model can violate: rather than merely
    rejecting a send that named too many tickets at once (the caller's
    fallback when this returns ``None``), pull the model's own
    already-written per-ticket paragraphs apart and let each ship as its
    own message. Confirmed against a real triage-nag violation (2026-09-04,
    session cron_triage-nag-001_20260904_133005): the model's single
    response was already three blank-line-separated blocks, each opening
    with exactly one ``[#<id> ...]`` link -- there was no prose to lose by
    splitting, only a Python-side send loop missing.

    A ticket-less block (a stray intro or closing line) is folded into the
    following ticket block, or the previous one if it comes last, so no
    content is dropped and no block ships with nothing but connective
    tissue.

    Returns ``None`` -- "do not attempt this" -- when:
      - there is only one block to begin with (nothing to split),
      - any single block itself names 2+ tickets (splitting would require
        guessing which sentence belongs to which ticket, which this
        function will never do), or
      - no block names any ticket at all (nothing to split *by*).
    The caller's existing hard-reject behavior is the correct fallback in
    every one of those cases.
    """
    blocks = [b.strip() for b in _BLANK_LINE_SPLIT_RE.split(content or "") if b.strip()]
    if len(blocks) < 2:
        return None

    per_block_ids = [set(_TICKET_NUMBER_RE.findall(b)) for b in blocks]
    if any(len(ids) > 1 for ids in per_block_ids):
        return None
    if not any(per_block_ids):
        return None

    messages: List[str] = []
    pending: List[str] = []
    for block, ids in zip(blocks, per_block_ids):
        pending.append(block)
        if ids:
            messages.append("\n\n".join(pending))
            pending = []
    if pending:
        # Trailing ticket-less block (a closing line after the last ticket) --
        # fold into the last message rather than dropping it or sending it
        # alone. `messages` is guaranteed non-empty here: `any(per_block_ids)`
        # above means at least one block had an id and was already flushed.
        messages[-1] = messages[-1] + "\n\n" + "\n\n".join(pending)

    return messages if len(messages) > 1 else None
