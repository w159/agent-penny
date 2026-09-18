"""Tests for tools/graph_schedule_tool.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools import graph_schedule_tool as tool
from tools.microsoft_graph_client import MicrosoftGraphAPIError
from tools.registry import registry


class _FakeClient:
    """Stand-in for MicrosoftGraphClient.get_json -- no real HTTP call.

    ``responses`` maps a path (ignoring query params) to either a payload
    dict or an exception instance to raise when that path is requested.
    """

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[dict] = []

    async def get_json(self, path, *, params=None, headers=None):
        self.calls.append({"path": path, "params": params})
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


def _user(person_id="u-1", name="Jane Doe", mail="jane.doe@henssler.com"):
    return {"id": person_id, "displayName": name, "mail": mail, "userPrincipalName": mail}


def _users_page(*users):
    return {"value": list(users)}


NOW = datetime(2026, 9, 18, 18, 0, 0, tzinfo=timezone.utc)


def _event(subject, start_offset_minutes, end_offset_minutes, sensitivity="normal"):
    start = NOW + timedelta(minutes=start_offset_minutes)
    end = NOW + timedelta(minutes=end_offset_minutes)
    return {
        "subject": subject,
        "sensitivity": sensitivity,
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
        "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
    }


@pytest.mark.anyio
class TestGetUserPresence:
    async def test_happy_path_resolves_by_email(self):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/presence": {"availability": "Busy", "activity": "InAMeeting"},
        })
        result = await tool.get_user_presence("jane.doe@henssler.com", client=client)
        assert result == {
            "success": True,
            "person": {"id": "u-1", "displayName": "Jane Doe", "mail": "jane.doe@henssler.com"},
            "availability": "Busy",
            "activity": "InAMeeting",
        }

    async def test_happy_path_resolves_by_display_name(self):
        client = _FakeClient({
            "/users": _users_page(_user()),
            "/users/u-1/presence": {"availability": "Available", "activity": "Available"},
        })
        result = await tool.get_user_presence("Jane Doe", client=client)
        assert result["success"] is True
        assert result["availability"] == "Available"
        # A display-name lookup goes straight to the $filter search, never /users/{upn}.
        assert client.calls[0]["path"] == "/users"

    async def test_permission_missing_returns_clear_message_not_raise(self):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/presence": MicrosoftGraphAPIError(
                403, "GET", "https://graph.microsoft.com/v1.0/users/u-1/presence", "Forbidden"
            ),
        })
        result = await tool.get_user_presence("jane.doe@henssler.com", client=client)
        assert result["success"] is False
        assert "Presence.Read.All" in result["error"]
        assert "insufficient" in result["error"].lower()


@pytest.mark.anyio
class TestGetUserCalendarStatus:
    async def test_in_meeting_reports_subject_and_end_time(self):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/calendarView": {"value": [_event("1:1 with manager", -10, 20)]},
        })
        result = await tool.get_user_calendar_status("jane.doe@henssler.com", client=client, now=NOW)
        assert result["success"] is True
        assert result["in_meeting"] is True
        assert result["subject"] == "1:1 with manager"
        assert result["meeting_ends_at"] is not None
        assert result["next_meeting_starts_at"] is None

    async def test_in_meeting_hides_private_subject(self):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/calendarView": {"value": [_event("Therapy", -5, 15, sensitivity="private")]},
        })
        result = await tool.get_user_calendar_status("jane.doe@henssler.com", client=client, now=NOW)
        assert result["in_meeting"] is True
        assert result["subject"] is None

    async def test_free_now_reports_next_meeting_start(self):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/calendarView": {
                "value": [_event("Standup", 30, 45), _event("Board review", 120, 180)]
            },
        })
        result = await tool.get_user_calendar_status("jane.doe@henssler.com", client=client, now=NOW)
        assert result["in_meeting"] is False
        assert result["subject"] is None
        expected_next = (NOW + timedelta(minutes=30)).isoformat()
        assert result["next_meeting_starts_at"] == expected_next

    async def test_free_with_no_upcoming_meetings(self):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/calendarView": {"value": []},
        })
        result = await tool.get_user_calendar_status("jane.doe@henssler.com", client=client, now=NOW)
        assert result["in_meeting"] is False
        assert result["next_meeting_starts_at"] is None

    async def test_permission_missing_returns_clear_message_not_raise(self):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/calendarView": MicrosoftGraphAPIError(
                403, "GET", "https://graph.microsoft.com/v1.0/users/u-1/calendarView", "Forbidden"
            ),
        })
        result = await tool.get_user_calendar_status("jane.doe@henssler.com", client=client, now=NOW)
        assert result["success"] is False
        assert "Calendars.Read" in result["error"]


@pytest.mark.anyio
class TestGetUserOutOfOffice:
    async def test_happy_path_scheduled_ooo(self):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/mailboxSettings": {
                "automaticRepliesSetting": {
                    "status": "scheduled",
                    "scheduledStartDateTime": {"dateTime": "2026-09-19T00:00:00.0000000", "timeZone": "UTC"},
                    "scheduledEndDateTime": {"dateTime": "2026-09-22T00:00:00.0000000", "timeZone": "UTC"},
                    "internalReplyMessage": "Out until Monday, contact my manager for urgent items.",
                }
            },
        })
        result = await tool.get_user_out_of_office("jane.doe@henssler.com", client=client)
        assert result["success"] is True
        assert result["status"] == "scheduled"
        assert result["scheduled_start_at"] == "2026-09-19T00:00:00.0000000"
        assert result["scheduled_end_at"] == "2026-09-22T00:00:00.0000000"
        assert "Out until Monday" in result["internal_message"]

    async def test_truncates_long_internal_message(self):
        long_message = "x" * 1000
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/mailboxSettings": {
                "automaticRepliesSetting": {"status": "alwaysEnabled", "internalReplyMessage": long_message}
            },
        })
        result = await tool.get_user_out_of_office("jane.doe@henssler.com", client=client)
        assert len(result["internal_message"]) < 1000
        assert result["internal_message"].endswith("[truncated]")

    async def test_permission_missing_returns_clear_message_not_raise(self):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/mailboxSettings": MicrosoftGraphAPIError(
                403, "GET", "https://graph.microsoft.com/v1.0/users/u-1/mailboxSettings", "Forbidden"
            ),
        })
        result = await tool.get_user_out_of_office("jane.doe@henssler.com", client=client)
        assert result["success"] is False
        assert "MailboxSettings.Read" in result["error"]


@pytest.mark.anyio
class TestPersonResolution:
    async def test_not_found_is_a_clear_message_not_a_stack_trace(self):
        client = _FakeClient({"/users": _users_page()})
        result = await tool.get_user_presence("Nobody Here", client=client)
        assert result["success"] is False
        assert "No Henssler staff member found" in result["error"]

    async def test_ambiguous_match_lists_candidates(self):
        client = _FakeClient({
            "/users": _users_page(
                _user("u-1", "Jane Doe", "jane.doe@henssler.com"),
                _user("u-2", "Jane Doe", "jane.doe2@henssler.com"),
            )
        })
        result = await tool.get_user_presence("Jane Doe", client=client)
        assert result["success"] is False
        assert "matches more than one person" in result["error"]
        assert "jane.doe@henssler.com" in result["error"]
        assert "jane.doe2@henssler.com" in result["error"]

    async def test_empty_person_is_a_clear_message(self):
        client = _FakeClient({})
        result = await tool.get_user_presence("   ", client=client)
        assert result["success"] is False
        assert "person is required" in result["error"]

    async def test_permission_missing_on_resolution_itself_returns_clear_message(self):
        """The /users/{upn} or /users lookup can itself 403 (needs User.Read.All,
        distinct from the three named permissions) before any presence/calendar/
        mailbox call ever runs -- this must never escape as a raw exception."""
        client = _FakeClient({
            "/users/jane.doe@henssler.com": MicrosoftGraphAPIError(
                status_code=403, method="GET", url="/users/jane.doe@henssler.com",
                message="Authorization_RequestDenied",
            ),
        })
        result = await tool.get_user_presence("jane.doe@henssler.com", client=client)
        assert result["success"] is False
        assert "User.Read.All" in result["error"]


@pytest.mark.anyio
class TestRegistryDispatch:
    """Exercise the tools through the real registry, not the bare functions only."""

    async def test_presence_tool_dispatches_and_returns_json(self, monkeypatch):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/presence": {"availability": "Away", "activity": "Away"},
        })
        monkeypatch.setattr(tool, "_build_client", lambda: client)
        raw = registry.dispatch("get_user_presence", {"person": "jane.doe@henssler.com"})
        payload = json.loads(raw)
        assert payload["availability"] == "Away"
        assert "error" not in payload

    async def test_missing_person_argument_is_a_tool_error_not_a_crash(self):
        raw = registry.dispatch("get_user_presence", {"person": ""})
        payload = json.loads(raw)
        assert "error" in payload
        assert "person is required" in payload["error"]

    async def test_permission_missing_dispatches_to_tool_error(self, monkeypatch):
        client = _FakeClient({
            "/users/jane.doe@henssler.com": _user(),
            "/users/u-1/mailboxSettings": MicrosoftGraphAPIError(
                403, "GET", "https://graph.microsoft.com/v1.0/users/u-1/mailboxSettings", "Forbidden"
            ),
        })
        monkeypatch.setattr(tool, "_build_client", lambda: client)
        raw = registry.dispatch("get_user_out_of_office", {"person": "jane.doe@henssler.com"})
        payload = json.loads(raw)
        assert "error" in payload
        assert "MailboxSettings.Read" in payload["error"]


class TestToolsetRegistration:
    def test_schedule_toolset_lists_all_three_tools(self):
        import toolsets

        info = toolsets.get_toolset_info("schedule")
        assert info is not None
        assert set(info["resolved_tools"]) == {
            "get_user_presence",
            "get_user_calendar_status",
            "get_user_out_of_office",
        }
