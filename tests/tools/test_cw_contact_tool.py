"""Tests for tools/cw_contact_tool.py."""

from __future__ import annotations

import json

import pytest

from cron.cw_client import CWError
from tools import cw_contact_tool as tool
from tools.registry import registry


class _FakeCWClient:
    """Stand-in for cron.cw_client.CWClient.get -- no real HTTP call.

    ``responses`` maps a path (ignoring query params) to either a payload
    (dict for a single ticket, list for a ticket search) or an exception
    instance to raise when that path is requested.
    """

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[dict] = []

    def get(self, path, **params):
        self.calls.append({"path": path, "params": params})
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


def _ticket(ticket_id=96886, contact_name="Erica Martin", contact_email="EMartin@HENSSLER.com"):
    return {
        "id": ticket_id,
        "summary": "DocuSign is not in my Authenticator app options.",
        "board": {"name": "Triage"},
        "status": {"name": "Closed"},
        "company": {"name": "Henssler Financial"},
        "contactName": contact_name,
        "contactEmailAddress": contact_email,
        "contactPhoneNumber": "6787973739",
    }


@pytest.mark.anyio
class TestGetTicketContact:
    async def test_happy_path_returns_contact(self):
        client = _FakeCWClient({"/service/tickets/96886": _ticket()})
        result = await tool.get_ticket_contact("96886", client=client)
        assert result == {
            "success": True,
            "ticket_id": 96886,
            "summary": "DocuSign is not in my Authenticator app options.",
            "board": "Triage",
            "status": "Closed",
            "company": "Henssler Financial",
            "contact_name": "Erica Martin",
            "contact_email": "EMartin@HENSSLER.com",
            "contact_phone": "6787973739",
        }

    async def test_ticket_not_found_is_a_clear_message_not_a_stack_trace(self):
        client = _FakeCWClient({
            "/service/tickets/999999999": CWError("CW GET /service/tickets/999999999 failed: 404", status=404),
        })
        result = await tool.get_ticket_contact("999999999", client=client)
        assert result["success"] is False
        assert "No ConnectWise ticket found" in result["error"]

    async def test_non_numeric_ticket_number_is_a_clear_message(self):
        client = _FakeCWClient({})
        result = await tool.get_ticket_contact("not-a-number", client=client)
        assert result["success"] is False
        assert "ticket_number is required" in result["error"]

    async def test_permission_error_returns_clear_message_not_raise(self):
        client = _FakeCWClient({
            "/service/tickets/96886": CWError("CW GET /service/tickets/96886 failed: 403 Forbidden", status=403),
        })
        result = await tool.get_ticket_contact("96886", client=client)
        assert result["success"] is False
        assert "insufficient API member permissions" in result["error"]


@pytest.mark.anyio
class TestFindTicketsByContact:
    async def test_happy_path_by_email(self):
        client = _FakeCWClient({"/service/tickets": [_ticket(), _ticket(97581)]})
        result = await tool.find_tickets_by_contact("EMartin@HENSSLER.com", client=client)
        assert result["success"] is True
        assert len(result["tickets"]) == 2
        assert client.calls[0]["params"]["conditions"] == 'contactEmailAddress="EMartin@HENSSLER.com"'

    async def test_no_match_is_a_clear_message(self):
        client = _FakeCWClient({"/service/tickets": []})
        result = await tool.find_tickets_by_contact("nobody at all", client=client)
        assert result["success"] is False
        assert "No ConnectWise tickets found" in result["error"]

    async def test_ambiguous_partial_name_lists_distinct_contacts(self):
        client = _FakeCWClient({
            "/service/tickets": [
                _ticket(1, "Jane Doe", "jane.doe@henssler.com"),
                _ticket(2, "Jane Smith", "jane.smith@henssler.com"),
            ]
        })
        result = await tool.find_tickets_by_contact("Jane", client=client)
        assert result["success"] is False
        assert "matches more than one contact" in result["error"]
        assert "jane.doe@henssler.com" in result["error"]
        assert "jane.smith@henssler.com" in result["error"]

    async def test_empty_contact_is_a_clear_message(self):
        client = _FakeCWClient({})
        result = await tool.find_tickets_by_contact("   ", client=client)
        assert result["success"] is False
        assert "contact is required" in result["error"]

    async def test_permission_error_returns_clear_message_not_raise(self):
        client = _FakeCWClient({
            "/service/tickets": CWError("CW GET /service/tickets failed: 401 Unauthorized", status=401),
        })
        result = await tool.find_tickets_by_contact("Erica Martin", client=client)
        assert result["success"] is False
        assert "insufficient API member permissions" in result["error"]


@pytest.mark.anyio
class TestTicketTrendByRequester:
    async def test_empty_index_is_a_clear_message(self, monkeypatch, tmp_path):
        db_path = tmp_path / "empty.db"
        monkeypatch.setattr("cron.cw_contact_index.DB_PATH", db_path)
        result = await tool.ticket_trend_by_requester(days=30)
        assert result["success"] is False
        assert "hasn't run its first refresh" in result["error"]

    async def test_happy_path_reports_freshness(self, monkeypatch, tmp_path):
        db_path = tmp_path / "index.db"
        monkeypatch.setattr("cron.cw_contact_index.DB_PATH", db_path)
        from cron.cw_contact_index import connect

        conn = connect(db_path)
        conn.execute(
            "INSERT INTO tickets (id, date_entered, contact_name, contact_email, company_name, indexed_at) "
            "VALUES (1, '2026-09-01T00:00:00Z', 'Erica Martin', 'e@x.com', 'Henssler Financial', '2026-09-18T00:00:00+00:00')"
        )
        conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('last_refreshed_at', '2026-09-18T18:00:00+00:00')")
        conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('window_days', '90')")
        conn.close()

        result = await tool.ticket_trend_by_requester(days=365, min_tickets=1)
        assert result["success"] is True
        assert result["requesters"] == [
            {"contact_name": "Erica Martin", "contact_email": "e@x.com", "company": "Henssler Financial", "ticket_count": 1}
        ]
        assert result["index_stale"] is False


@pytest.mark.anyio
class TestRegistryDispatch:
    """Exercise the tools through the real registry, not the bare functions only."""

    async def test_get_ticket_contact_dispatches_and_returns_json(self, monkeypatch):
        client = _FakeCWClient({"/service/tickets/96886": _ticket()})
        monkeypatch.setattr(tool, "CWClient", lambda: client)
        raw = registry.dispatch("get_ticket_contact", {"ticket_number": "96886"})
        payload = json.loads(raw)
        assert payload["contact_name"] == "Erica Martin"
        assert "error" not in payload

    async def test_missing_contact_argument_is_a_tool_error_not_a_crash(self):
        raw = registry.dispatch("find_tickets_by_contact", {"contact": ""})
        payload = json.loads(raw)
        assert "error" in payload
        assert "contact is required" in payload["error"]

    async def test_not_found_dispatches_to_tool_error(self, monkeypatch):
        client = _FakeCWClient({
            "/service/tickets/1": CWError("CW GET /service/tickets/1 failed: 404", status=404),
        })
        monkeypatch.setattr(tool, "CWClient", lambda: client)
        raw = registry.dispatch("get_ticket_contact", {"ticket_number": "1"})
        payload = json.loads(raw)
        assert "error" in payload
        assert "No ConnectWise ticket found" in payload["error"]


class TestToolsetRegistration:
    def test_cw_contact_toolset_lists_all_three_tools(self):
        import toolsets

        info = toolsets.get_toolset_info("cw_contact")
        assert info is not None
        assert set(info["resolved_tools"]) == {
            "get_ticket_contact",
            "find_tickets_by_contact",
            "ticket_trend_by_requester",
        }
