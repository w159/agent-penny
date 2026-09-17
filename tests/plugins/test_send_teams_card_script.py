"""The operator smoke tool for the webhook card lane must keep working.

scripts/send_teams_card.py is deliberately trivial, but it is also an
import-time regression check for the Python card builder: if
plugins.platforms.teams.ticket_card loses build_ticket_card, or the fence
renderer stops producing one parseable card, this file fails before an
operator ever runs the tool.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "send_teams_card.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("send_teams_card", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TICKET = {
    "event": "new_ticket",
    "ticket_id": 94822,
    "summary": "Cannot get into SharePoint",
    "issue": "Karis reports the tenant login rejects her password since this morning.",
    "company": "Henssler Financial",
    "contact": "Karis Simpson",
    "status": "New",
    "priority": "Priority 3 - Medium",
    "owner": "",
    "unassigned": True,
    "url": "https://na.example/ticket?service_recid=94822",
}


def test_script_imports_cleanly(script):
    """The import itself is the regression check for the card builder."""
    assert script.main


def test_build_renders_one_valid_card_from_a_realistic_ticket(script):
    message = script._build(TICKET)
    assert message.count("```adaptivecard") == 1
    body = message.split("```adaptivecard", 1)[1].rsplit("```", 1)[0]
    card = json.loads(body)
    assert card["type"] == "AdaptiveCard"
    assert card["body"]


def test_build_rejects_a_non_object_ticket(script):
    with pytest.raises(SystemExit, match="must be an object"):
        script._validate_ticket([1, 2, 3])
