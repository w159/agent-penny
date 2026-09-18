"""Tests for tools/outbound_contact_gate.py -- the IT-roster gate on
agent-initiated outbound messaging (Jerry's non-negotiable rule: Penny never
contacts anyone outside the established roster without his express, per-
instance approval).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools import outbound_contact_gate as gate

# check_outbound_contact() lazily imports request_connector_action_approval
# from tools.connector_action_gate, so that is the real patch target -- not
# an attribute of this module.
_APPROVE_TARGET = "tools.connector_action_gate.request_connector_action_approval"


@pytest.fixture(autouse=True)
def _reset_dedupe_cache():
    """Each test gets an empty dedupe cache -- entries persist process-wide
    otherwise and would leak between tests."""
    gate._dedupe_cache.clear()
    yield
    gate._dedupe_cache.clear()


@pytest.fixture(autouse=True)
def _outbound_contact_gate_open_by_default():
    """Shadow tests/tools/conftest.py's same-named autouse fixture: THIS file
    is the gate's own dedicated coverage, so it must not be bypassed."""
    yield


class TestEstablishedTarget:
    def test_teams_roster_member_is_established(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "jerry-aad-id,jarvis-aad-id")
        assert gate.is_established_outbound_target("teams", "jerry-aad-id") is True

    def test_teams_group_chat_is_established(self, monkeypatch):
        monkeypatch.setenv("TEAMS_GROUP_ALLOWED_CHATS", "19:abc@thread.v2")
        assert gate.is_established_outbound_target("teams", "19:abc@thread.v2") is True

    def test_teams_home_channel_is_established(self, monkeypatch):
        monkeypatch.setenv("TEAMS_HOME_CHANNEL", "19:home@thread.v2")
        assert gate.is_established_outbound_target("teams", "19:home@thread.v2") is True

    def test_empty_chat_id_is_established(self, monkeypatch):
        """No explicit target -> about to fall back to the home channel, which
        is always established."""
        assert gate.is_established_outbound_target("teams", None) is True
        assert gate.is_established_outbound_target("teams", "") is True

    def test_non_roster_target_is_not_established(self, monkeypatch):
        monkeypatch.delenv("TEAMS_GROUP_ALLOWED_CHATS", raising=False)
        monkeypatch.delenv("TEAMS_HOME_CHANNEL", raising=False)
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "jerry-aad-id")
        # A ConnectWise ticket contact's Teams id, not in the roster.
        assert gate.is_established_outbound_target("teams", "erica-martin-aad-id") is False


class TestCheckOutboundContact:
    def test_roster_member_auto_allowed_without_approval_prompt(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "jerry-aad-id")
        with patch(_APPROVE_TARGET) as mock_request:
            allowed, outcome = gate.check_outbound_contact(
                platform_name="teams", chat_id="jerry-aad-id", message="status update",
            )
        mock_request.assert_not_called()
        assert allowed is True
        assert outcome == "auto_allowed_roster"

    def test_non_roster_target_blocks_pending_approval(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "jerry-aad-id")
        with patch(_APPROVE_TARGET, return_value=(False, "error")) as mock_request:
            allowed, outcome = gate.check_outbound_contact(
                platform_name="teams", chat_id="erica-martin-aad-id",
                message="Following up on your ticket",
            )
        mock_request.assert_called_once()
        assert allowed is False
        assert outcome == "error"

    def test_non_roster_target_approved_by_jerry_proceeds(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "jerry-aad-id")
        with patch(_APPROVE_TARGET, return_value=(True, "approved")):
            allowed, outcome = gate.check_outbound_contact(
                platform_name="teams", chat_id="erica-martin-aad-id", message="hi",
            )
        assert allowed is True
        assert outcome == "approved"

    def test_repeated_identical_blocked_request_is_deduped(self, monkeypatch):
        """A retried identical send must not re-prompt Jerry every time."""
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "jerry-aad-id")
        with patch(_APPROVE_TARGET, return_value=(False, "denied")) as mock_request:
            first = gate.check_outbound_contact(
                platform_name="teams", chat_id="erica-martin-aad-id", message="hi there",
            )
            second = gate.check_outbound_contact(
                platform_name="teams", chat_id="erica-martin-aad-id", message="hi there",
            )
        assert mock_request.call_count == 1  # second call hit the dedupe cache
        assert first == (False, "denied")
        assert second == (False, "denied_cached")

    def test_different_message_is_not_deduped(self, monkeypatch):
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "jerry-aad-id")
        with patch(_APPROVE_TARGET, return_value=(False, "denied")) as mock_request:
            gate.check_outbound_contact(platform_name="teams", chat_id="erica-martin-aad-id", message="msg one")
            gate.check_outbound_contact(platform_name="teams", chat_id="erica-martin-aad-id", message="msg two")
        assert mock_request.call_count == 2

    def test_roster_check_failure_fails_closed(self, monkeypatch):
        with patch.object(gate, "is_established_outbound_target", side_effect=RuntimeError("boom")):
            allowed, outcome = gate.check_outbound_contact(
                platform_name="teams", chat_id="whoever", message="hi",
            )
        assert allowed is False
        assert outcome == "error"
