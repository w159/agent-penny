#!/usr/bin/env python3
"""Tests for tools/behavior_change_tool.py -- visible self-improvement flow."""
import json
from unittest.mock import MagicMock

import pytest

from cron import behavior_db
from gateway.session_context import clear_session_vars, set_session_vars
from tools import behavior_change_tool as tool

JARVIS = "6ff84f43-2fac-4366-926d-382cb712deae"
ERNESTO = "010a8823-1689-473a-8cde-3a26c406beae"
INTRUDER = "not-authorized-user"
ALLOWED_CSV = f"{JARVIS},{ERNESTO}"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "behavior.db"
    monkeypatch.setattr(behavior_db, "DB_PATH", path)
    return path


@pytest.fixture(autouse=True)
def allow_all(monkeypatch):
    monkeypatch.setenv("TEAMS_ALLOWED_USERS", ALLOWED_CSV)


@pytest.fixture
def teams_session():
    """Bind a Teams session context for the duration of a test."""
    tokens = set_session_vars(
        platform="teams", chat_id="19:group-chat@thread.tacv2",
        user_id=JARVIS, message_id="msg-1",
    )
    yield
    clear_session_vars(tokens)


@pytest.fixture
def mock_send(monkeypatch):
    mock = MagicMock(return_value=json.dumps({"success": True}))
    monkeypatch.setattr("tools.send_message_tool.send_message_tool", mock)
    return mock


def _as_ernesto():
    """Rebind the session to Ernesto (the approver) mid-test."""
    return set_session_vars(
        platform="teams", chat_id="19:group-chat@thread.tacv2",
        user_id=ERNESTO, message_id="msg-2",
    )


def test_propose_returns_beh_id_and_stays_pending(db_path, teams_session, mock_send):
    result = json.loads(tool.propose_behavior_change_tool({
        "kind": "knob", "text": "stop posting cards for closed tickets",
        "scope": "cards", "key": "cards.post_on_closed_ticket", "value": False,
    }))
    assert result["proposal_id"].startswith("BEH-")
    assert result["status"] == "pending"

    from cron import behavior_store
    assert "cards.post_on_closed_ticket" not in behavior_store.render_active_rules()
    assert behavior_store.count_active() == 0


def test_propose_message_includes_requester_diff_id_and_instructions(db_path, teams_session, mock_send):
    result = json.loads(tool.propose_behavior_change_tool({
        "kind": "instruction", "text": "never post cards after 6pm",
        "scope": "cards",
    }))
    msg = result["chat_message"]
    assert JARVIS in msg
    assert "never post cards after 6pm" in msg
    assert result["proposal_id"] in msg
    assert "approve" in msg.lower()


def test_authorized_approver_activates_rule(db_path, teams_session, mock_send):
    proposed = json.loads(tool.propose_behavior_change_tool({
        "kind": "knob", "text": "min tickets 5", "scope": "triage",
        "key": "trend.alert_min_tickets", "value": 5,
    }))
    beh_id = proposed["proposal_id"]

    tokens = _as_ernesto()
    try:
        result = json.loads(tool.approve_behavior_change_tool({"proposal_id": beh_id}))
    finally:
        clear_session_vars(tokens)

    assert result["status"] == "active"
    from cron import behavior_store
    assert "trend.alert_min_tickets = 5" in behavior_store.render_active_rules()


def test_unauthorized_approver_rejected_and_visible(db_path, teams_session, mock_send):
    proposed = json.loads(tool.propose_behavior_change_tool({
        "kind": "instruction", "text": "escalate P1s immediately", "scope": "triage",
    }))
    beh_id = proposed["proposal_id"]
    mock_send.reset_mock()

    tokens = set_session_vars(
        platform="teams", chat_id="19:group-chat@thread.tacv2",
        user_id=INTRUDER, message_id="msg-3",
    )
    try:
        result = tool.approve_behavior_change_tool({"proposal_id": beh_id})
    finally:
        clear_session_vars(tokens)

    assert "error" in json.loads(result)
    from cron import behavior_store
    assert behavior_store.count_active() == 0
    # The refusal was posted visibly to the group chat.
    assert mock_send.called
    posted_message = mock_send.call_args[0][0]["message"]
    assert "not authorized" in posted_message.lower()
    assert INTRUDER in posted_message


def test_bare_yes_resolves_no_proposal_id():
    assert tool.extract_beh_id_from_reply("yes") is None
    assert tool.extract_beh_id_from_reply("yes, do it") is None
    assert tool.extract_beh_id_from_reply("approve BEH-12") == "BEH-12"
    assert tool.extract_beh_id_from_reply("BEH-12 approved") == "BEH-12"
    assert tool.extract_beh_id_from_reply("yes to BEH-12") == "BEH-12"


def test_approve_tool_rejects_missing_id_argument(db_path, teams_session, mock_send):
    result = json.loads(tool.approve_behavior_change_tool({"proposal_id": ""}))
    assert "error" in result
    result = json.loads(tool.approve_behavior_change_tool({"proposal_id": "yes"}))
    assert "error" in result


def test_invalid_knob_key_produces_helpful_error_not_a_stored_rule(db_path, teams_session, mock_send):
    result = json.loads(tool.propose_behavior_change_tool({
        "kind": "knob", "text": "made up setting", "scope": "triage",
        "key": "not.a.real.knob", "value": 1,
    }))
    assert "error" in result
    assert "unknown knob key" in result["error"]

    from cron import behavior_store
    assert behavior_store.history(limit=10) == []


def test_requesters_own_message_does_not_auto_approve(db_path, teams_session, mock_send):
    proposed = json.loads(tool.propose_behavior_change_tool({
        "kind": "instruction", "text": "stop posting after 6pm", "scope": "cards",
    }))
    from cron import behavior_store
    row = behavior_store.history(limit=1)[0]
    # The requester (Jarvis, from the teams_session fixture) proposed it, and
    # that proposal call alone never activates the rule -- only a distinct
    # approve() call by an authorized approver can.
    assert row["status"] == "pending"
    assert row["requested_by"] == JARVIS
    assert behavior_store.count_active() == 0
    assert proposed["status"] == "pending"


def test_visibility_message_emitted_through_send_message_seam_for_propose(db_path, teams_session, mock_send):
    tool.propose_behavior_change_tool({
        "kind": "instruction", "text": "never post cards after 6pm", "scope": "cards",
    })
    assert mock_send.called
    call_args = mock_send.call_args[0][0]
    assert call_args["action"] == "send"
    assert call_args["target"] == "teams:19:group-chat@thread.tacv2"
    assert "never post cards after 6pm" in call_args["message"]


def test_visibility_message_emitted_through_send_message_seam_for_approve(db_path, teams_session, mock_send):
    proposed = json.loads(tool.propose_behavior_change_tool({
        "kind": "instruction", "text": "escalate P1s immediately", "scope": "triage",
    }))
    mock_send.reset_mock()

    tokens = _as_ernesto()
    try:
        tool.approve_behavior_change_tool({"proposal_id": proposed["proposal_id"]})
    finally:
        clear_session_vars(tokens)

    assert mock_send.called
    call_args = mock_send.call_args[0][0]
    assert call_args["action"] == "send"
    assert "Rule is now active" in call_args["message"]
    assert "approve_behavior_change(proposal_id=" in call_args["message"]


def test_non_teams_session_does_not_post_visibility_message(db_path, mock_send):
    tokens = set_session_vars(platform="cli", chat_id="", user_id=JARVIS)
    try:
        tool.propose_behavior_change_tool({
            "kind": "instruction", "text": "never post cards after 6pm", "scope": "cards",
        })
    finally:
        clear_session_vars(tokens)
    assert not mock_send.called
