"""Tests for plugins/cw_contact_context/ - the structural pre-fetch that injects a
ConnectWise ticket's contact + recent history into ``pre_llm_call`` context whenever a
ticket number appears in the incoming message.

Root-caused against real production data (state.db session 20260915_133141_81527460,
messages 28187-28194): Jerry asked "now, who's the worst end user?" and Penny answered
from the internal-tech/roster frame ("I keep the roasts strictly internal... which
tech's ticket habits") twice before Jerry had to name the tool himself ("for your tools
this would be cw_ticket_contact"). This plugin is the structural half of the fix for the
reliable trigger (a ticket number in the message); SOUL.md's "End users are ConnectWise
ticket contacts" section covers the fuzzier cases.
"""
from __future__ import annotations

import pytest

import plugins.cw_contact_context as plugin


@pytest.fixture(autouse=True)
def _reset_executor_between_tests():
    # The module-level ThreadPoolExecutor is process-wide; nothing to reset, but keep the
    # fixture as an anchor point if that ever changes.
    yield


class TestTicketNumberDetection:
    def test_hash_prefixed_ticket_number_matches(self):
        assert plugin._TICKET_NUMBER_RE.search("did you update ticket #91041?").group(1) == "91041"

    def test_bare_hash_number_matches(self):
        assert plugin._TICKET_NUMBER_RE.search("closed #91041 finally").group(1) == "91041"

    def test_ticket_word_without_hash_matches(self):
        assert plugin._TICKET_NUMBER_RE.search("ticket 91041 is done").group(1) == "91041"

    def test_bare_number_with_no_ticket_or_hash_context_does_not_match(self):
        # A phone number or dollar figure must not trigger a live CW lookup.
        assert plugin._TICKET_NUMBER_RE.search("call me at 91041 dollars") is None

    def test_no_digits_does_not_match(self):
        assert plugin._TICKET_NUMBER_RE.search("who's the worst end user?") is None


class TestExtractText:
    def test_plain_string_passes_through(self):
        assert plugin._extract_text("ticket #123") == "ticket #123"

    def test_none_returns_empty(self):
        assert plugin._extract_text(None) == ""


class TestOnPreLlmCall:
    def test_no_ticket_number_returns_none(self):
        assert plugin._on_pre_llm_call(user_message="who's the worst end user?") is None

    def test_ticket_number_injects_contact_and_history(self, monkeypatch):
        async def _fake_get_ticket_contact(ticket_number, *, client=None):
            assert ticket_number == "91041"
            return {
                "success": True,
                "ticket_id": 91041,
                "summary": "Tamarac session timeout",
                "company": "Henssler Financial",
                "status": "Closed",
                "contact_name": "Adam Ledbetter",
                "contact_email": "ALedbetter@HENSSLER.com",
                "contact_phone": "6787973739",
            }

        async def _fake_find_tickets_by_contact(contact, *, limit=10, client=None):
            assert contact == "ALedbetter@HENSSLER.com"
            return {
                "success": True,
                "contact_query": contact,
                "tickets": [
                    {"ticket_id": 91041, "summary": "Tamarac session timeout", "status": "Closed"},
                    {"ticket_id": 88213, "summary": "VPN drops on wifi", "status": "Open"},
                ],
            }

        monkeypatch.setattr("tools.cw_contact_tool.check_cw_contact_requirements", lambda: True)
        monkeypatch.setattr("tools.cw_contact_tool.get_ticket_contact", _fake_get_ticket_contact)
        monkeypatch.setattr("tools.cw_contact_tool.find_tickets_by_contact", _fake_find_tickets_by_contact)

        result = plugin._on_pre_llm_call(user_message="did jarvis ever close ticket #91041?")

        assert result is not None
        context = result["context"]
        assert "Adam Ledbetter" in context
        assert "ALedbetter@HENSSLER.com" in context
        assert "#88213" in context  # the OTHER ticket surfaced as history
        history_lines = [line for line in context.splitlines() if line.strip().startswith("- #")]
        assert not any("91041" in line for line in history_lines)  # own ticket excluded from "other tickets"

    def test_unconfigured_cw_env_returns_none(self, monkeypatch):
        monkeypatch.setattr("tools.cw_contact_tool.check_cw_contact_requirements", lambda: False)
        result = plugin._on_pre_llm_call(user_message="ticket #91041 status?")
        assert result is None

    def test_ticket_not_found_returns_none(self, monkeypatch):
        async def _fake_get_ticket_contact(ticket_number, *, client=None):
            return {"success": False, "error": "No ConnectWise ticket found with number 91041."}

        monkeypatch.setattr("tools.cw_contact_tool.check_cw_contact_requirements", lambda: True)
        monkeypatch.setattr("tools.cw_contact_tool.get_ticket_contact", _fake_get_ticket_contact)

        result = plugin._on_pre_llm_call(user_message="ticket #91041 status?")
        assert result is None

    def test_lookup_exception_fails_open(self, monkeypatch):
        async def _raising(*_a, **_kw):
            raise RuntimeError("CW API unreachable")

        monkeypatch.setattr("tools.cw_contact_tool.check_cw_contact_requirements", lambda: True)
        monkeypatch.setattr("tools.cw_contact_tool.get_ticket_contact", _raising)

        result = plugin._on_pre_llm_call(user_message="ticket #91041 status?")
        assert result is None


class TestPluginRegistration:
    def test_register_hooks_pre_llm_call(self):
        registered = {}

        class _FakeCtx:
            def register_hook(self, hook_name, callback):
                registered[hook_name] = callback

        plugin.register(_FakeCtx())
        assert registered == {"pre_llm_call": plugin._on_pre_llm_call}
