"""Regression replay of the seven real malformed-card incidents.

Each fixture below is the output of ``scripts/cw_callback_handler.py`` run
against the actual ConnectWise callback payload (from
~/.hermes/logs/cw_callback_payloads.jsonl) behind one stored delivery in
state.db ``delivery_obligations`` whose ```adaptivecard fence failed
``json.loads``. Between 2026-08-03 and 2026-08-10, 7 of 22 card attempts
(32%) failed that way, and the recorded parse errors are pinned here so a
future change that hands card authorship back to the model fails loudly.

The failures were plain JSON syntax slips no prompt can prevent: a dropped
``"value":`` key (94741) and a body array closed one bracket early (the rest).
Building the card with ``json.dumps`` removes the class rather than the
instances.
"""

import json
import re

import pytest

from plugins.platforms.teams.ticket_card import render_triage_message

_FENCE = re.compile(
    r"```(?:adaptivecard|adaptive[_-]?card)\b[ \t\r]*\n?(.*?)```", re.DOTALL | re.IGNORECASE
)

_URL = (
    "https://na.myconnectwise.net/v4_6_release/services/system_io/Service/"
    "fv_sr100_request.rails?service_recid="
)

# ticket_id -> (recorded parse error, model verdict the old card had styled for,
#               handler facts)
REAL_FAILURES = {
    94741: (
        "Expecting ':' delimiter: line 1 column 616 (char 615)",
        False,
        {
            "event": "new_ticket",
            "ticket_id": 94741,
            "summary": "One Drive not syncing - free up space",
            "company": "Catchall",
            "contact": "Kara Furness",
            "priority": "Priority 4 - Low",
            "owner": "",
            "unassigned": True,
        },
    ),
    94744: (
        "Expecting ',' delimiter: line 1 column 746 (char 745)",
        False,
        {
            "event": "new_ticket",
            "ticket_id": 94744,
            "summary": "New Shared Credentials Found in Use - Henssler Financial Group",
            "company": "Catchall",
            "contact": "Auvik System",
            "priority": "Priority 4 - Low",
            "owner": "",
            "unassigned": True,
        },
    ),
    94766: (
        "Expecting ',' delimiter: line 1 column 814 (char 813)",
        True,
        {
            "event": "new_ticket",
            "ticket_id": 94766,
            "summary": (
                "Eric Stephens started as our maintenance technician with a "
                "Henssler email. He needs his IT onboardin"
            ),
            "company": "Henssler Financial",
            "contact": "Hannah Hall",
            "priority": "Priority 3 - Medium",
            "owner": "",
            "unassigned": True,
        },
    ),
    94775: (
        "Expecting ',' delimiter: line 1 column 785 (char 784)",
        False,
        {
            "event": "new_ticket",
            "ticket_id": 94775,
            "summary": (
                "Incident 0NF2JA-P6KM08: Wi-Fi signal on COLLAB-AREA3 "
                "(Collaboration Area 3) - Needs action"
            ),
            "company": "Henssler Financial",
            "contact": "",
            "priority": "Priority 4 - Low",
            "owner": "",
            "unassigned": True,
        },
    ),
    94792: (
        "Expecting ',' delimiter: line 1 column 724 (char 723)",
        False,
        {
            "event": "new_ticket",
            "ticket_id": 94792,
            "summary": "Laptop stuck at startup",
            "company": "Henssler Financial",
            "contact": "Sabrina Kim",
            "priority": "Priority 3 - Medium",
            "owner": "Jarvis Williams",
            "unassigned": False,
        },
    ),
    94822: (
        "Expecting ',' delimiter: line 1 column 801 (char 800)",
        False,
        {
            "event": "new_ticket",
            "ticket_id": 94822,
            "summary": (
                "I am having issues getting into my sharepoint. I can pull up "
                "the page but there isn't any documents "
            ),
            "company": "Henssler Financial",
            "contact": "Karis Simpson",
            "priority": "Priority 3 - Medium",
            "owner": "",
            "unassigned": True,
        },
    ),
    94852: (
        "Expecting ',' delimiter: line 1 column 748 (char 747)",
        False,
        {
            "event": "new_ticket",
            "ticket_id": 94852,
            "summary": "cant connect schwab accounts in emoney for a client ",
            "company": "Henssler Financial",
            "contact": "Amy Yang",
            "priority": "Priority 3 - Medium",
            "owner": "",
            "unassigned": True,
        },
    ),
}


@pytest.mark.parametrize("ticket_id", sorted(REAL_FAILURES))
def test_real_incident_now_produces_exactly_one_valid_card(ticket_id):
    recorded_error, blocked, facts = REAL_FAILURES[ticket_id]
    facts = {**facts, "url": f"{_URL}{ticket_id}"}

    message = render_triage_message(
        ("BLOCKED" if blocked else "ROUTINE") + "\nSomeone should look at this.",
        facts,
    )

    fences = _FENCE.findall(message)
    assert len(fences) == 1, f"{recorded_error} incident produced {len(fences)} cards"
    card = json.loads(fences[0])  # the old path raised exactly here

    action_set = next(b for b in card["body"] if b["type"] == "ActionSet")
    actions = {a["type"]: a for a in action_set["actions"]}
    assert actions["Action.OpenUrl"]["url"] == facts["url"]
    # The claim button is for tickets nobody owns; an assigned one gets only
    # the link, so a click cannot take work away from the person holding it.
    if facts["unassigned"]:
        assert actions["Action.Execute"]["data"]["ticket_id"] == ticket_id
        assert actions["Action.Execute"]["verb"] == "penny_cw_assign"
    else:
        assert "Action.Execute" not in actions

    title = card["body"][0]["columns"][1]["items"][0]["text"]
    assert title.startswith("BLOCKED - ") is blocked
    # The fact grid used to be a Table element (cells); it is now a Container
    # of ColumnSet rows (columns) - Teams does not reliably render Table even
    # at schema 1.5+, which is what produced the cards.unsupported fallback.
    rows = {
        r["columns"][0]["items"][0]["text"]: r["columns"][1]["items"][0]["text"]
        for r in card["body"][1]["items"]
    }
    if facts["contact"]:
        assert rows["Contact"] == facts["contact"]
    else:
        assert "Contact" not in rows
    assert rows["Owner"] == (facts["owner"] or "**UNASSIGNED**")
