"""Schedule/presence lookup tools for Agent Penny, backed by Microsoft Graph.

Answers "is this person in a meeting" / "are they out of office" for any
Henssler staff member by email or display name. Reuses the existing Graph
auth layer (``tools.microsoft_graph_auth``) and the generic
``MicrosoftGraphClient`` HTTP/retry machinery instead of standing up a
second token flow or a second HTTP client -- see ``tools/graph_mail.py``
for the sibling that established this pattern for outbound mail.

Same rationale as ``tools/graph_mail.py`` for the credential env vars: the
Hermes Teams app registration authenticates the same way it already does
for the Teams webhook (client-credentials, app-only), but its secrets live
in the Teams-flavored env var names (``ENTRA_TENANT_ID`` / ``TEAMS_CLIENT_ID``
/ ``TEAMS_CLIENT_SECRET``) rather than the ``MSGRAPH_*`` names
``GraphCredentials.from_env`` expects, so credentials are built directly
here instead of going through that helper. Do not introduce a third
credential convention -- add scopes to this same app registration.

Requires three NEW application permissions on that registration, each with
admin consent, that may not be granted yet at the time this ships:
Calendars.Read, Presence.Read.All, MailboxSettings.Read. Every Graph call
below treats a 403 as an expected, user-facing outcome ("permission not
yet granted") rather than letting it crash the agent turn.

Docs consulted (mandatory before writing the calls below): Microsoft Learn,
"presence: get" (GET /users/{id}/presence, Presence.Read.All application
permission), "user: list" ($filter on mail/displayName), "user: calendarView"
(GET /users/{id}/calendarView?startDateTime=&endDateTime=, Calendars.Read
application permission), "user: get mailboxSettings" (automaticRepliesSetting,
MailboxSettings.Read application permission).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from tools.microsoft_graph_auth import (
    GraphCredentials,
    MicrosoftGraphAuthError,
    MicrosoftGraphTokenProvider,
)
from tools.microsoft_graph_client import (
    MicrosoftGraphAPIError,
    MicrosoftGraphClient,
    MicrosoftGraphClientError,
)
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Same Teams app registration env vars as tools/graph_mail.py -- see this
# module's docstring for why these are used instead of GraphCredentials.from_env's
# MSGRAPH_* names.
_TENANT_ID_ENV = "ENTRA_TENANT_ID"
_CLIENT_ID_ENV = "TEAMS_CLIENT_ID"
_CLIENT_SECRET_ENV = "TEAMS_CLIENT_SECRET"

_MAX_RETRIES = 3

# calendarView window: far enough back to still catch an in-progress meeting,
# far enough forward to answer "when's their next meeting" without paging.
_CALENDAR_LOOKBACK = timedelta(minutes=15)
_CALENDAR_LOOKAHEAD = timedelta(hours=24)

_MAX_OOO_MESSAGE_CHARS = 500


class ScheduleLookupError(RuntimeError):
    """Expected, user-facing failure (ambiguous/missing person). Never a stack trace."""


def check_schedule_requirements() -> bool:
    return bool(
        os.getenv(_TENANT_ID_ENV) and os.getenv(_CLIENT_ID_ENV) and os.getenv(_CLIENT_SECRET_ENV)
    )


def _load_schedule_credentials(environ: Optional[dict[str, str]] = None) -> GraphCredentials:
    """Build Graph credentials from the Teams app registration's env vars.

    Fails loudly and names exactly what is missing -- same rationale as
    ``tools.graph_mail._load_mail_credentials``: this is the credential
    boundary, so a silent no-op would hide a real configuration gap.
    """
    env = environ if environ is not None else os.environ
    tenant_id = (env.get(_TENANT_ID_ENV) or "").strip()
    client_id = (env.get(_CLIENT_ID_ENV) or "").strip()
    client_secret = (env.get(_CLIENT_SECRET_ENV) or "").strip()

    missing = [
        name
        for name, value in (
            (_TENANT_ID_ENV, tenant_id),
            (_CLIENT_ID_ENV, client_id),
            (_CLIENT_SECRET_ENV, client_secret),
        )
        if not value
    ]
    if missing:
        raise MicrosoftGraphAuthError(
            "Cannot look up schedule/presence via Microsoft Graph: missing "
            f"{', '.join(missing)}. Set these in .env for the Teams app "
            "registration and grant it the Calendars.Read, Presence.Read.All, "
            "and MailboxSettings.Read application permissions with admin consent."
        )
    return GraphCredentials(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)


def _build_client() -> MicrosoftGraphClient:
    credentials = _load_schedule_credentials()
    token_provider = MicrosoftGraphTokenProvider(credentials)
    return MicrosoftGraphClient(token_provider, max_retries=_MAX_RETRIES, timeout=30.0)


def _odata_escape(value: str) -> str:
    """Escape a string for use inside an OData literal (double any single quote)."""
    return value.replace("'", "''")


def _person_summary(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id"),
        "displayName": user.get("displayName"),
        "mail": user.get("mail") or user.get("userPrincipalName"),
    }


def _graph_permission_message(
    action: str, exc: MicrosoftGraphAPIError, *, needed_permission: str = "Calendars.Read, Presence.Read.All, or MailboxSettings.Read",
) -> str:
    return (
        f"Cannot {action}: Microsoft Graph denied this request (insufficient privileges). "
        "The Entra app registration has likely not yet been granted -- or admin-consented "
        f"for -- the application permission this needs ({needed_permission}). "
        f"Ask an admin to finish granting it, then retry. ({exc})"
    )


async def _resolve_person_or_error(
    client: MicrosoftGraphClient, person: str, action: str
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """``_resolve_person`` wrapped so every failure mode -- bad input, no match,
    ambiguous match, or a 403 from the lookup itself (needs ``User.Read.All``,
    distinct from this tool's three named permissions) -- returns an error dict
    instead of an exception escaping to the caller."""
    try:
        return await _resolve_person(client, person), None
    except ScheduleLookupError as exc:
        return None, {"success": False, "error": str(exc)}
    except MicrosoftGraphAPIError as exc:
        if exc.status_code == 403:
            return None, {
                "success": False,
                "error": _graph_permission_message(action, exc, needed_permission="User.Read.All"),
            }
        return None, {"success": False, "error": f"Microsoft Graph error looking up '{person}': {exc}"}
    except MicrosoftGraphClientError as exc:
        return None, {"success": False, "error": f"Microsoft Graph request looking up '{person}' failed: {exc}"}

async def _resolve_person(client: MicrosoftGraphClient, person: str) -> dict[str, Any]:
    """Resolve *person* (email/UPN or display name) to a Graph user resource.

    Raises ``ScheduleLookupError`` for empty, not-found, or ambiguous input --
    callers must never let those escape as a raw Graph exception or stack trace.
    """
    person = (person or "").strip()
    if not person:
        raise ScheduleLookupError("person is required (an email address or display name).")

    select = "id,displayName,mail,userPrincipalName"
    looks_like_email = "@" in person

    if looks_like_email:
        # Fast, precise path: the UPN/email lookup is a direct resource GET.
        try:
            return await client.get_json(f"/users/{_odata_escape(person)}", params={"$select": select})
        except MicrosoftGraphAPIError as exc:
            if exc.status_code != 404:
                raise

    odata_filter = f"mail eq '{_odata_escape(person)}' or displayName eq '{_odata_escape(person)}'"
    payload = await client.get_json("/users", params={"$filter": odata_filter, "$select": select})
    matches = payload.get("value", []) if isinstance(payload, dict) else []

    if not matches:
        raise ScheduleLookupError(f"No Henssler staff member found matching '{person}'.")
    if len(matches) > 1:
        candidates = ", ".join(f"{m.get('displayName')} <{m.get('mail')}>" for m in matches)
        raise ScheduleLookupError(
            f"'{person}' matches more than one person: {candidates}. "
            "Use a full email address to disambiguate."
        )
    return matches[0]


def _audit_log(tool_name: str, requested_person: str, user: dict[str, Any]) -> None:
    """One INFO audit line per successful lookup (FTC Safeguards/GLBA/Reg S-P: this
    surfaces arbitrary staff scheduling data through a chat bot and needs a trail)."""
    logger.info(
        "graph_schedule_tool: %s resolved person",
        tool_name,
        extra={
            "tool": tool_name,
            "requested_person": requested_person,
            "resolved_display_name": user.get("displayName"),
            "resolved_mail": user.get("mail") or user.get("userPrincipalName"),
            "resolved_id": user.get("id"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Mirrors plugins/platforms/teams/ticket_card.py's _ET_ZONE/_eastern_timestamp
# convention (same zone id, same format) rather than inventing a second one.
# Henssler staff are Eastern-time; a bare UTC timestamp read out loud in a
# Teams chat is the wrong answer even when it's technically correct.
_ET_ZONE = "America/New_York"


def _eastern(dt: Optional[datetime]) -> Optional[str]:
    """*dt* (any tzinfo, naive treated as UTC) as Eastern MM/DD/YYYY HH:MM, or
    None when *dt* is None -- callers pair this with the UTC ISO field, never
    replace it, so a downstream consumer needing the exact instant still has
    it."""
    if dt is None:
        return None
    from zoneinfo import ZoneInfo

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(_ET_ZONE)).strftime("%m/%d/%Y %H:%M %Z")


def _parse_graph_datetime(value: str) -> Optional[datetime]:
    """Parse a Graph ``dateTime`` string (up to 7 fractional-second digits, no
    zone suffix when no ``Prefer: outlook.timezone`` header was sent -- ours
    never is, so these are UTC). Returns None instead of raising on garbage."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        if "." in value:
            base, frac = value.split(".", 1)
            frac_digits = "".join(c for c in frac if c.isdigit())[:6]
            value = f"{base}.{frac_digits}" if frac_digits else base
        value = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _event_start(event: dict[str, Any]) -> Optional[datetime]:
    return _parse_graph_datetime((event.get("start") or {}).get("dateTime", ""))


def _event_end(event: dict[str, Any]) -> Optional[datetime]:
    return _parse_graph_datetime((event.get("end") or {}).get("dateTime", ""))


def _is_private(event: dict[str, Any]) -> bool:
    return str(event.get("sensitivity") or "").lower() in ("private", "confidential")


# ---------------------------------------------------------------------------
# Library functions -- return {"success": bool, "error": str, ...} rather than
# raising for expected failure modes, mirroring tools.graph_mail.send_html_mail.
# ---------------------------------------------------------------------------


async def get_user_presence(
    person: str, *, client: Optional[MicrosoftGraphClient] = None
) -> dict[str, Any]:
    """Resolve *person* then GET /users/{id}/presence (availability, activity)."""
    client = client or _build_client()
    user, error = await _resolve_person_or_error(client, person, "look up that person")
    if error is not None:
        return error

    try:
        presence = await client.get_json(f"/users/{user['id']}/presence")
    except MicrosoftGraphAPIError as exc:
        if exc.status_code == 403:
            return {"success": False, "error": _graph_permission_message("read presence", exc)}
        return {"success": False, "error": f"Microsoft Graph error reading presence: {exc}"}
    except MicrosoftGraphClientError as exc:
        return {"success": False, "error": f"Microsoft Graph request for presence failed: {exc}"}

    _audit_log("get_user_presence", person, user)
    return {
        "success": True,
        "person": _person_summary(user),
        "availability": presence.get("availability"),
        "activity": presence.get("activity"),
    }


async def get_user_calendar_status(
    person: str,
    *,
    client: Optional[MicrosoftGraphClient] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Resolve *person* then report whether they're in a meeting right now."""
    client = client or _build_client()
    user, error = await _resolve_person_or_error(client, person, "look up that person")
    if error is not None:
        return error

    now = now or datetime.now(timezone.utc)
    window_start, window_end = now - _CALENDAR_LOOKBACK, now + _CALENDAR_LOOKAHEAD

    try:
        payload = await client.get_json(
            f"/users/{user['id']}/calendarView",
            params={
                "startDateTime": _iso(window_start),
                "endDateTime": _iso(window_end),
                "$select": "subject,start,end,sensitivity",
            },
        )
    except MicrosoftGraphAPIError as exc:
        if exc.status_code == 403:
            return {"success": False, "error": _graph_permission_message("read calendar", exc)}
        return {"success": False, "error": f"Microsoft Graph error reading calendar: {exc}"}
    except MicrosoftGraphClientError as exc:
        return {"success": False, "error": f"Microsoft Graph request for calendar failed: {exc}"}

    events = payload.get("value", []) if isinstance(payload, dict) else []
    events = sorted((e for e in events if _event_start(e) is not None), key=_event_start)

    _audit_log("get_user_calendar_status", person, user)

    current = next((e for e in events if _event_start(e) <= now and (_event_end(e) or now) > now), None)
    if current is not None:
        ends_at = _event_end(current)
        return {
            "success": True,
            "person": _person_summary(user),
            "in_meeting": True,
            "subject": None if _is_private(current) else current.get("subject"),
            "meeting_ends_at": ends_at.isoformat() if ends_at else None,
            "meeting_ends_at_eastern": _eastern(ends_at),
            "next_meeting_starts_at": None,
            "next_meeting_starts_at_eastern": None,
        }

    upcoming = next((e for e in events if _event_start(e) > now), None)
    starts_at = _event_start(upcoming) if upcoming else None
    return {
        "success": True,
        "person": _person_summary(user),
        "in_meeting": False,
        "subject": None,
        "meeting_ends_at": None,
        "meeting_ends_at_eastern": None,
        "next_meeting_starts_at": starts_at.isoformat() if starts_at else None,
        "next_meeting_starts_at_eastern": _eastern(starts_at),
    }


async def get_user_out_of_office(
    person: str, *, client: Optional[MicrosoftGraphClient] = None
) -> dict[str, Any]:
    """Resolve *person* then report their automatic-replies (OOO) setting."""
    client = client or _build_client()
    user, error = await _resolve_person_or_error(client, person, "look up that person")
    if error is not None:
        return error

    try:
        settings = await client.get_json(
            f"/users/{user['id']}/mailboxSettings",
            params={"$select": "automaticRepliesSetting"},
        )
    except MicrosoftGraphAPIError as exc:
        if exc.status_code == 403:
            return {"success": False, "error": _graph_permission_message("read mailbox settings", exc)}
        return {"success": False, "error": f"Microsoft Graph error reading mailbox settings: {exc}"}
    except MicrosoftGraphClientError as exc:
        return {"success": False, "error": f"Microsoft Graph request for mailbox settings failed: {exc}"}

    auto_replies = settings.get("automaticRepliesSetting") or {}
    _audit_log("get_user_out_of_office", person, user)

    internal_message = str(auto_replies.get("internalReplyMessage") or "")
    if len(internal_message) > _MAX_OOO_MESSAGE_CHARS:
        internal_message = internal_message[:_MAX_OOO_MESSAGE_CHARS] + "... [truncated]"

    # scheduledStartDateTime/EndDateTime.timeZone is "UTC" for every mailbox
    # checked against the real tenant (2026-09-18) -- _parse_graph_datetime's
    # naive-string-is-UTC assumption holds for this deployment.
    scheduled_start_raw = ((auto_replies.get("scheduledStartDateTime") or {}).get("dateTime")) or None
    scheduled_end_raw = ((auto_replies.get("scheduledEndDateTime") or {}).get("dateTime")) or None
    return {
        "success": True,
        "person": _person_summary(user),
        "status": auto_replies.get("status"),
        "scheduled_start_at": scheduled_start_raw,
        "scheduled_start_at_eastern": _eastern(_parse_graph_datetime(scheduled_start_raw or "")),
        "scheduled_end_at": scheduled_end_raw,
        "scheduled_end_at_eastern": _eastern(_parse_graph_datetime(scheduled_end_raw or "")),
        "internal_message": internal_message,
    }


# ---------------------------------------------------------------------------
# Registry handlers -- thin argument parsing + JSON-string boundary.
# ---------------------------------------------------------------------------


def _require_person_arg(args: dict) -> tuple[Optional[str], Optional[str]]:
    person = (args.get("person") or "").strip()
    if not person:
        return None, tool_error("person is required (an email address or display name).")
    return person, None


async def _handle_get_user_presence(args: dict, **kw: Any) -> str:
    person, error = _require_person_arg(args)
    if error is not None:
        return error
    result = await get_user_presence(person)
    if not result["success"]:
        return tool_error(result["error"])
    return tool_result({k: v for k, v in result.items() if k != "success"})


async def _handle_get_user_calendar_status(args: dict, **kw: Any) -> str:
    person, error = _require_person_arg(args)
    if error is not None:
        return error
    result = await get_user_calendar_status(person)
    if not result["success"]:
        return tool_error(result["error"])
    return tool_result({k: v for k, v in result.items() if k != "success"})


async def _handle_get_user_out_of_office(args: dict, **kw: Any) -> str:
    person, error = _require_person_arg(args)
    if error is not None:
        return error
    result = await get_user_out_of_office(person)
    if not result["success"]:
        return tool_error(result["error"])
    return tool_result({k: v for k, v in result.items() if k != "success"})


_PERSON_PARAM = {
    "type": "string",
    "description": (
        "The staff member to look up, by work email address or display name, "
        "e.g. 'jane.doe@henssler.com' or 'Jane Doe'."
    ),
}

GET_USER_PRESENCE_SCHEMA = {
    "name": "get_user_presence",
    "description": (
        "Look up a Henssler staff member's live Teams presence (available, busy, "
        "away, do not disturb, offline) and current activity."
    ),
    "parameters": {
        "type": "object",
        "properties": {"person": _PERSON_PARAM},
        "required": ["person"],
    },
}

GET_USER_CALENDAR_STATUS_SCHEMA = {
    "name": "get_user_calendar_status",
    "description": (
        "Check whether a Henssler staff member is in a meeting right now. Reports "
        "the meeting subject when it isn't marked private, or their next meeting "
        "start time when they're currently free. Henssler staff are Eastern time: "
        "when telling a person a time, use meeting_ends_at_eastern / "
        "next_meeting_starts_at_eastern, not the raw UTC fields."
    ),
    "parameters": {
        "type": "object",
        "properties": {"person": _PERSON_PARAM},
        "required": ["person"],
    },
}

GET_USER_OUT_OF_OFFICE_SCHEMA = {
    "name": "get_user_out_of_office",
    "description": (
        "Check a Henssler staff member's automatic-replies (out-of-office) setting: "
        "whether it's on, its scheduled start/end, and a truncated preview of the "
        "internal reply message. Henssler staff are Eastern time: when telling a "
        "person a time, use scheduled_start_at_eastern / scheduled_end_at_eastern, "
        "not the raw UTC fields."
    ),
    "parameters": {
        "type": "object",
        "properties": {"person": _PERSON_PARAM},
        "required": ["person"],
    },
}


registry.register(
    name="get_user_presence",
    toolset="schedule",
    schema=GET_USER_PRESENCE_SCHEMA,
    handler=_handle_get_user_presence,
    check_fn=check_schedule_requirements,
    requires_env=[_TENANT_ID_ENV, _CLIENT_ID_ENV, _CLIENT_SECRET_ENV],
    is_async=True,
    emoji="\U0001f7e2",
)

registry.register(
    name="get_user_calendar_status",
    toolset="schedule",
    schema=GET_USER_CALENDAR_STATUS_SCHEMA,
    handler=_handle_get_user_calendar_status,
    check_fn=check_schedule_requirements,
    requires_env=[_TENANT_ID_ENV, _CLIENT_ID_ENV, _CLIENT_SECRET_ENV],
    is_async=True,
    emoji="\U0001f4c5",
)

registry.register(
    name="get_user_out_of_office",
    toolset="schedule",
    schema=GET_USER_OUT_OF_OFFICE_SCHEMA,
    handler=_handle_get_user_out_of_office,
    check_fn=check_schedule_requirements,
    requires_env=[_TENANT_ID_ENV, _CLIENT_ID_ENV, _CLIENT_SECRET_ENV],
    is_async=True,
    emoji="\U0001f334",
)
