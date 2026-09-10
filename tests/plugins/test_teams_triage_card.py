"""The ConnectWise webhook lane's card is built in Python, not by the model.

The model used to author the whole ```adaptivecard fence. Across the stored
deliveries in state.db, 7 of 22 card attempts (32%) carried JSON the Teams
adapter could not parse, and each one degraded to raw JSON posted in a
help-desk channel. These tests pin the replacement contract: the model emits a
verdict word plus prose, Python emits the JSON, and nothing the model can write
produces an unparseable card.

The first version of that contract only held when the model played along. It
attached a card solely on ``new_ticket`` and solely when the reply opened with
a verdict token, and across 229 stored callbacks the verdict was present twice
- so on the other path the model's own reply, JSON and all, shipped verbatim.
The contract these tests pin now is unconditional: every delivered callback
carries exactly one Python-built card whatever its event class and whatever
the model wrote, and the verdict only chooses the styling.
"""

import json

import pytest

from plugins.platforms.teams.ticket_card import (
    VERDICT_BLOCKED,
    VERDICT_ROUTINE,
    _issue_text,
    build_triage_card,
    parse_verdict,
    render_triage_message,
)

TICKET = {
    "event": "new_ticket",
    "ticket_id": 94744,
    "summary": "New Shared Credentials Found in Use",
    "issue": "Auvik saw the same local admin password on four switches at the branch.",
    "company": "Catchall",
    "contact": "Auvik System",
    "status": "New",
    "priority": "Priority 4 - Low",
    "owner": "",
    "unassigned": True,
    "url": "https://na.example/ticket?service_recid=94744",
}

# Layout accessors. Every test reaches into the same fixed layout, and the
# layout being fixed is the property under test: an icon+title ColumnSet, a
# fact grid, the note, then the action row. The grid used to be a Table
# element; Teams does not reliably render Table even at schema 1.5+ (that is
# what produced the go.skype.com/cards.unsupported fallback in production),
# so it is now a Container of ColumnSet rows, the same shape build_ticket_card
# uses.
HEADER = 0
GRID = 1


def _card_from(message):
    """Pull the single fenced card out of a rendered message and parse it."""
    assert message.count("```adaptivecard") == 1, message
    body = message.split("```adaptivecard", 1)[1].rsplit("```", 1)[0]
    return json.loads(body)


def _facts(card):
    """The fact grid as {label: value}. First row is status/priority."""
    return {
        r["columns"][0]["items"][0]["text"]: r["columns"][1]["items"][0]["text"]
        for r in card["body"][GRID]["items"]
    }


def _title(card):
    return card["body"][HEADER]["columns"][1]["items"][0]["text"]


def _title_color(card):
    return card["body"][HEADER]["columns"][1]["items"][0]["color"]


def _icon(card):
    return card["body"][HEADER]["columns"][0]["items"][0]["images"][0]["url"]


def _note(card):
    """The issue-note TextBlock, or None when the card carries no note."""
    blocks = [b for b in card["body"] if b["type"] == "TextBlock"]
    return blocks[0] if blocks else None


def _action(card, action_type):
    for block in card["body"]:
        if block["type"] != "ActionSet":
            continue
        for action in block["actions"]:
            if action["type"] == action_type:
                return action
    return None


# ── verdict parsing ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "first_line,expected",
    [
        ("BLOCKED", VERDICT_BLOCKED),
        ("ROUTINE", VERDICT_ROUTINE),
        ("blocked", VERDICT_BLOCKED),
        ("**BLOCKED**", VERDICT_BLOCKED),
        ("ROUTINE:", VERDICT_ROUTINE),
        ("[BLOCKED]", VERDICT_BLOCKED),
    ],
)
def test_verdict_token_is_recognized_through_common_decoration(first_line, expected):
    verdict, prose = parse_verdict(f"{first_line}\nKara can't get into SharePoint.")
    assert verdict == expected
    assert prose == "Kara can't get into SharePoint."


@pytest.mark.parametrize(
    "content",
    [
        "",
        "Just a sentence with no verdict at all.",
        "MAYBE\nsome prose",
        "The ticket is BLOCKED, apparently.",
        None,
        12345,
        "{...half a json card...",
    ],
)
def test_unrecognized_output_never_raises_and_yields_no_verdict(content):
    verdict, prose = parse_verdict(content)
    assert verdict is None
    assert isinstance(prose, str)


# ── card construction ────────────────────────────────────────────────────

def test_blocked_verdict_marks_the_title():
    """The verdict can only prefix the title - it cannot restyle or reorder
    the card, so a model turn can never change the card's shape."""
    card = build_triage_card(TICKET, blocked=True)
    assert _title(card).startswith("BLOCKED - Ticket #94744 : ")
    assert _title_color(card) == "Attention"


def test_routine_verdict_leaves_the_title_alone():
    card = build_triage_card(TICKET, blocked=False)
    assert _title(card) == "Ticket #94744 : New Shared Credentials Found in Use"
    assert "BLOCKED" not in _title(card)


def test_the_verdict_cannot_change_the_card_layout():
    shape = lambda c: [b["type"] for b in c["body"]]
    assert shape(build_triage_card(TICKET, blocked=True)) == shape(
        build_triage_card(TICKET, blocked=False)
    )


def test_claim_button_keeps_its_verb_schema_and_ticket_id():
    card = build_triage_card(TICKET)
    assert card["version"] == "1.5"
    action = _action(card, "Action.Execute")
    assert action["verb"] == "penny_cw_assign"
    assert action["data"] == {"penny_action": "cw_assign", "ticket_id": 94744}


def test_every_card_links_back_to_the_ticket():
    """The link is a button, not markdown buried in the title.

    Deliveries used to carry the ticket link only when the model happened to
    write one into its prose, so half of them had no way back to ConnectWise.
    """
    action = _action(build_triage_card(TICKET), "Action.OpenUrl")
    assert action["title"] == "VIEW TICKET"
    assert action["url"] == TICKET["url"]


def test_issue_note_is_rendered_under_the_facts():
    card = build_triage_card(TICKET)
    assert _note(card)["text"] == TICKET["issue"]
    assert _note(card)["wrap"] is True


def test_missing_issue_note_still_renders_the_block():
    """A placeholder, not a vanished block: this lane always knows whether
    the ticket has a note, so silence here would be ambiguous."""
    card = build_triage_card({**TICKET, "issue": ""})
    assert _note(card)["text"] == "(no issue note on the ticket)"


def test_long_issue_note_is_capped_rather_than_dropped():
    card = build_triage_card({**TICKET, "issue": "x" * 5000})
    text = _note(card)["text"]
    assert len(text) <= 300
    assert text.endswith("...")


def test_new_tickets_show_more_of_the_note_than_status_changes_do():
    """A new ticket is the one a tech reads cold, so it gets six lines to the
    others' three - the same split the source cards make."""
    assert _note(build_triage_card(TICKET))["maxLines"] == 6
    assert _note(build_triage_card({**TICKET, "event": "closed", "status": "Closed"}))["maxLines"] == 3


def test_issue_note_keeps_the_line_breaks_the_submitter_typed():
    card = build_triage_card({**TICKET, "issue": "Line one\nLine two\n\n\n\nLine three"})
    assert _note(card)["text"] == "Line one\nLine two\n\nLine three"


def test_connectwise_nested_bracket_inline_image_is_removed():
    """CW's inline-image markdown: outer ![...] wraps its own [alt] bracket."""
    url = (
        "https://na.myconnectwise.net/v4_6_release/api/inlineimage?"
        "attachmentId=12345"
    )
    text = _issue_text({"issue": f"Screenshot attached.\n![[Image.png]]({url})\nSee above."})
    assert "![[Image.png]]" not in text
    assert url not in text
    assert "Screenshot attached." in text
    assert "See above." in text


def test_plain_markdown_image_is_removed():
    text = _issue_text({"issue": "Before.\n![alt text](https://example.com/pic.png)\nAfter."})
    assert "![alt text]" not in text
    assert "example.com/pic.png" not in text
    assert "Before." in text
    assert "After." in text


def test_escaped_bracket_inline_image_is_removed():
    """The shape email-sourced CW tickets actually carry: ![\\[alt\\]](url).

    Taken verbatim from a live Auvik SaaS Management ticket. The earlier
    nested-bracket pattern did not match this and the logo rendered full
    size in Teams.
    """
    url = "https://files.saas.auvik.com/Auvik-ASM-Logo-Full-color-RGB.png"
    text = _issue_text(
        {"issue": f"![\\[Auvik SaaS Management Logo\\]]({url})\n\nDetected via Auvik for App: Thomson Reuters"}
    )
    assert "files.saas.auvik.com" not in text
    assert "Auvik SaaS Management Logo" not in text
    assert text == "Detected via Auvik for App: Thomson Reuters"


def test_inline_image_with_a_markdown_title_is_removed():
    """Microsoft service-health mail carries ![\\[Microsoft\\]](url "Microsoft")."""
    text = _issue_text(
        {
            "issue": 'Before.\n![\\[Microsoft\\]](https://images.ecomm.microsoft.com/logo.png "Microsoft")\nAfter.'
        }
    )
    assert "images.ecomm.microsoft.com" not in text
    assert "Before." in text
    assert "After." in text


def test_image_stripping_does_not_swallow_a_following_paragraph():
    """A stray ![ must not eat the rest of the note looking for a closing ](."""
    text = _issue_text({"issue": "Cost is ![ high\nSecond line stays.\nThird line stays."})
    assert "Second line stays." in text
    assert "Third line stays." in text


def test_ordinary_markdown_link_is_not_treated_as_an_image():
    text = _issue_text({"issue": "Check the [Official Status page](https://status.example.com) now."})
    assert text == "Check the [Official Status page](https://status.example.com) now."


def test_bare_image_url_alone_on_its_own_line_is_removed():
    text = _issue_text(
        {"issue": "Here is the error:\nhttps://example.com/uploads/screenshot.png\nThanks."}
    )
    assert "example.com/uploads/screenshot.png" not in text
    assert "Here is the error:" in text
    assert "Thanks." in text


def test_prose_url_mid_sentence_is_left_alone():
    text = _issue_text({"issue": "See https://example.com/docs for details."})
    assert text == "See https://example.com/docs for details."


def test_strip_inline_images_never_raises_on_empty_or_missing_issue():
    assert _issue_text({}) == "(no issue note on the ticket)"
    assert _issue_text({"issue": ""}) == "(no issue note on the ticket)"
    assert _issue_text({"issue": None}) == "(no issue note on the ticket)"


def test_facts_carry_the_ticket_data():
    facts = _facts(build_triage_card(TICKET))
    assert facts["New"] == "Priority 4 - Low"      # status/priority header row
    assert facts["Contact"] == "Auvik System"
    assert facts["Owner"] == "**UNASSIGNED**"
    assert "#94744" in _title(build_triage_card(TICKET))


def test_status_and_priority_lead_the_table():
    rows = build_triage_card(TICKET)["body"][GRID]["items"]
    first = rows[0]["columns"]
    assert first[0]["items"][0]["text"] == "New"
    assert first[1]["items"][0]["text"] == "Priority 4 - Low"


def test_board_and_company_are_not_repeated_on_every_card():
    """One board, one company. Restating both on every post is noise."""
    facts = _facts(build_triage_card(TICKET))
    assert "Board" not in facts
    assert "Company" not in facts


def test_contact_email_and_phone_no_longer_appear_on_the_card():
    """The FactSet is Contact/Priority/Owner only now; email and phone are gone."""
    card = build_triage_card(
        {**TICKET, "contact": "Clay Norman",
         "contact_email": "CNorman@HENSSLER.com", "contact_phone": "6787973751"}
    )
    facts = _facts(card)
    assert facts["Contact"] == "Clay Norman"
    assert "Email" not in facts
    assert "Phone" not in facts


def test_missing_contact_drops_its_row_rather_than_printing_a_placeholder():
    facts = _facts(build_triage_card({**TICKET, "contact": ""}))
    assert "Contact" not in facts
    # The rows that carry the ticket's identity are still there.
    assert facts["Owner"] == "**UNASSIGNED**"


def test_owner_present_is_shown_instead_of_unassigned():
    card = build_triage_card({**TICKET, "owner": "Jarvis Williams", "unassigned": False})
    assert _facts(card)["Owner"] == "Jarvis Williams"


def test_claim_button_is_dropped_once_someone_owns_the_ticket():
    card = build_triage_card({**TICKET, "owner": "Jarvis Williams", "unassigned": False})
    assert _action(card, "Action.Execute") is None
    assert _action(card, "Action.OpenUrl") is not None


def test_closed_tickets_get_no_claim_button():
    card = build_triage_card({**TICKET, "event": "closed"})
    assert _action(card, "Action.Execute") is None


@pytest.mark.parametrize(
    "event,status,color",
    [
        ("new_ticket", "New", "Attention"),
        ("closed", "Closed", "Warning"),
        ("reopened", "Re-Opened", "Attention"),
        ("unknown", "On-Hold", "Accent"),
        ("", "Waiting Client Response*", "Good"),
        (None, "In Progress (Silent)", "Attention"),
    ],
)
def test_every_event_class_renders_one_of_the_four_designs(event, status, color):
    card = build_triage_card({**TICKET, "event": event, "status": status})
    assert _title_color(card) == color
    assert _icon(card).startswith("https://")


@pytest.mark.parametrize(
    "ticket",
    [
        {},
        {"event": "new_ticket"},
        {"ticket_id": None, "summary": None, "company": None},
        {"ticket_id": "not-a-number", "summary": 'quote " and \\ backslash'},
        {"ticket_id": 1, "summary": "x" * 5000, "company": "]}\"',"},
        {"ticket_id": 1, "contact": {"nested": "dict"}},
    ],
)
def test_built_card_always_parses_as_json(ticket):
    """json.dumps is the point: no field content can break the syntax."""
    for blocked in (True, False):
        card = build_triage_card(ticket, blocked=blocked)
        assert json.loads(json.dumps(card)) == card


# ── rendered message ─────────────────────────────────────────────────────

def test_new_ticket_message_is_exactly_one_valid_card():
    message = render_triage_message(
        "ROUTINE\nAuvik flagged shared credentials at Catchall; nobody owns it yet.",
        TICKET,
    )
    # The card and nothing else: the adapter turns every segment outside the
    # fence into a second Teams activity, which rang one ticket twice.
    assert message.startswith("```adaptivecard")
    assert message.rstrip().endswith("```")
    assert message.count("```adaptivecard") == 1
    card = _card_from(message)
    assert _action(card, "Action.Execute")["data"]["ticket_id"] == 94744


def test_one_ticket_per_message_holds_even_when_the_model_names_several():
    message = render_triage_message(
        "ROUTINE\nSee also #94741, #94766 and #94775 for related noise.",
        TICKET,
    )
    assert message.count("```adaptivecard") == 1
    card = _card_from(message)
    assert _action(card, "Action.Execute")["data"]["ticket_id"] == 94744


def test_model_authored_fence_is_stripped_so_python_owns_the_only_card():
    message = render_triage_message(
        'BLOCKED\nShe cannot work.\n```adaptivecard\n{"broken": ,}\n```',
        TICKET,
    )
    card = _card_from(message)
    assert _title(card).startswith("BLOCKED - ")
    assert "broken" not in message


@pytest.mark.parametrize("event", ["new_ticket", "closed", "reopened", "unknown", "", None])
def test_every_event_class_carries_a_card(event):
    """Reopened and closed events used to ship as bare prose."""
    message = render_triage_message(
        "ROUTINE\nTicket 94744 closed cleanly.", {**TICKET, "event": event}
    )
    card = _card_from(message)
    assert _note(card)["text"] == TICKET["issue"]
    assert _action(card, "Action.OpenUrl")["url"] == TICKET["url"]


def test_reply_without_a_verdict_still_gets_a_card():
    """The production failure, verbatim.

    30 of 32 stored non-silent replies opened with prose rather than a verdict
    token. The old path returned those untouched, which is how model-authored
    JSON reached the channel.
    """
    message = render_triage_message(
        "Karis Simpson's PC blue-screened and it is still unassigned.", TICKET
    )
    card = _card_from(message)
    assert "BLOCKED" not in _title(card)  # no verdict means routine


def test_model_json_without_a_verdict_never_reaches_teams():
    """The exact shape that shipped raw JSON: no verdict, one-line fence."""
    message = render_triage_message(
        'Karis needs help.\n\n```adaptivecard {"type":"AdaptiveCard","body":[{"text":}]}\n```',
        TICKET,
    )
    assert message.count("```adaptivecard") == 1
    card = _card_from(message)  # raises if the surviving card is the model's
    assert _note(card)["text"] == TICKET["issue"]


def test_a_reply_that_is_only_a_bad_fence_still_produces_one_clean_card():
    message = render_triage_message(
        '```adaptivecard {"type":"AdaptiveCard","body":[{"text":}]}\n```', TICKET
    )
    card = _card_from(message)
    assert "BLOCKED" not in _title(card)  # no verdict means routine
    assert _note(card)["text"] == TICKET["issue"]


@pytest.mark.parametrize(
    "content", ["", None, 0, [], "\n\n", "ROUTINE", "BLOCKED\n\n"]
)
def test_degenerate_model_output_never_raises(content):
    result = render_triage_message(content, TICKET)
    assert isinstance(result, str)


def test_empty_prose_still_produces_a_readable_message():
    card = _card_from(render_triage_message("BLOCKED\n", TICKET))
    assert _title(card).startswith("BLOCKED - ")


def test_every_delivery_is_exactly_one_teams_activity():
    """Nothing outside the fence, on any input.

    The adapter posts each fence-separated segment separately, so any stray
    text here becomes a second notification for the same ticket.
    """
    for reply in (
        "ROUTINE\nA sentence.",
        "No verdict at all.",
        "BLOCKED\n",
        '```adaptivecard {"broken":}\n```',
        "Prose before.\n```adaptivecard\n{}\n```\nand prose after.",
    ):
        message = render_triage_message(reply, TICKET)
        assert message.startswith("```adaptivecard"), message
        assert message.rstrip().endswith("```"), message
        assert message.count("```adaptivecard") == 1, message
