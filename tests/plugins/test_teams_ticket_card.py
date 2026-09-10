"""Tests for the Python-owned ticket Adaptive Card builder.

Cards used to be model-authored JSON in a ```adaptivecard``` fence; a bad
model turn could ship a malformed or oversized card. ``build_ticket_card``
builds the card content in plain Python from a ticket dict instead, so the
shape is fixed and never depends on the model getting JSON right.
"""

import json

from plugins.platforms.teams.ticket_card import (
    build_ticket_card,
    build_trend_card,
    build_triage_card,
    render_card_fence,
    split_autonomous_message_by_ticket,
)


def _ticket(**overrides):
    base = {
        "number": "1234",
        "priority": "High",
        "board": "Service Board",
        "owner": "Jane Doe",
        "summary": "Printer offline in the east wing",
        "url": "https://na.myconnectwise.net/x?service_recid=1234",
    }
    base.update(overrides)
    return base


def _rows(card):
    """The fact grid's rows as {label: value}.

    The grid is a Container of ColumnSet rows (not a Table - Teams mobile
    does not reliably render Table even at schema 1.5+; see the comment on
    ``_fact_row`` in ticket_card.py), so each row's label/value sit at
    columns[0]/[1] items[0] text, the same shape ``_label_value_row`` uses
    for the trend card.
    """
    grid = next(b for b in card["body"] if b["type"] == "Container")
    return {
        r["columns"][0]["items"][0]["text"]: r["columns"][1]["items"][0]["text"]
        for r in grid["items"]
    }


def _title(card):
    return card["body"][0]["columns"][1]["items"][0]["text"]


def _icon(card):
    return card["body"][0]["columns"][0]["items"][0]["images"][0]["url"]


def _title_color(card):
    return card["body"][0]["columns"][1]["items"][0]["color"]


class TestBuildTicketCard:
    def test_schema_valid_shape(self):
        card = build_ticket_card(_ticket())
        assert card["type"] == "AdaptiveCard"
        # 1.5 for Action.Execute's Universal Action Model (the claim
        # button), not for the fact grid, which uses ColumnSet and needs
        # no more than 1.2.
        assert card["version"] == "1.5"
        assert card["targetWidth"] == "Wide"
        assert isinstance(card["body"], list) and card["body"]
        assert "fallbackText" in card and card["fallbackText"]

    def test_no_table_element_anywhere_in_the_card(self):
        """Regression guard: Table is what produced the
        go.skype.com/cards.unsupported fallback in production (Teams does
        not reliably render Table even when the card declares schema
        1.5+). Walk the whole body recursively, not just the top level,
        so a Table nested inside a future Column/Container is still
        caught."""

        def _contains_table(node):
            if isinstance(node, dict):
                if node.get("type") == "Table":
                    return True
                return any(_contains_table(v) for v in node.values())
            if isinstance(node, list):
                return any(_contains_table(v) for v in node)
            return False

        card = build_ticket_card(_ticket())
        assert not _contains_table(card["body"])

    def test_heading_is_ticket_number_and_summary_beside_an_icon(self):
        card = build_ticket_card(_ticket())
        assert _title(card) == "Ticket #1234 : Printer offline in the east wing"
        assert _icon(card).startswith("https://")

    def test_status_and_priority_are_the_first_table_row(self):
        card = build_ticket_card(_ticket(status="In Progress"))
        grid = next(b for b in card["body"] if b["type"] == "Container")
        first = grid["items"][0]
        assert first["columns"][0]["items"][0]["text"] == "In Progress"
        assert first["columns"][1]["items"][0]["text"] == "High"

    def test_status_defaults_rather_than_dropping_the_row(self):
        rows = _rows(build_ticket_card(_ticket()))
        assert rows["Unknown"] == "High"

    def test_owner_row_renders_when_the_caller_knows_the_owner(self):
        assert _rows(build_ticket_card(_ticket()))["Owner"] == "Jane Doe"

    def test_rows_the_caller_has_no_data_for_are_omitted(self):
        """The cron lane has no note, timestamp, or assignee - and says so by
        leaving the rows out rather than printing Unknown three times."""
        ticket = _ticket()
        del ticket["owner"]
        rows = _rows(build_ticket_card(ticket))
        assert "Owner" not in rows
        assert "Last Updated" not in rows
        assert "Updated By" not in rows

    def test_last_updated_is_converted_to_eastern(self):
        rows = _rows(build_ticket_card(_ticket(last_updated="2026-08-14T15:54:30Z")))
        assert rows["Last Updated"] == "08/14/2026 11:54"

    def test_unparseable_timestamp_is_shown_rather_than_dropped(self):
        rows = _rows(build_ticket_card(_ticket(last_updated="last tuesday")))
        assert rows["Last Updated"] == "last tuesday"

    def test_updated_by_renders_when_present(self):
        rows = _rows(build_ticket_card(_ticket(updated_by="jwilliams")))
        assert rows["Updated By"] == "jwilliams"

    def test_new_and_active_tickets_use_the_new_design(self):
        for status in ("New", "Re-Opened", "In Progress (Silent)", "Customer Updated"):
            card = build_ticket_card(_ticket(status=status))
            assert _title_color(card) == "Attention", status
            assert "baby" in _icon(card), status

    def test_closed_statuses_use_the_closed_design(self):
        for status in ("Closed", "Resolved*", "Completed*", "Cancelled*", "Close Pending"):
            assert _title_color(build_ticket_card(_ticket(status=status))) == "Warning", status

    def test_closed_flag_beats_the_status_name(self):
        card = build_ticket_card(_ticket(status="Some Custom Status", closed_flag=True))
        assert _title_color(card) == "Warning"

    def test_on_hold_uses_the_onhold_design(self):
        assert _title_color(build_ticket_card(_ticket(status="On-Hold"))) == "Accent"

    def test_any_waiting_status_uses_the_waiting_design(self):
        for status in ("Waiting Client Response*", "Waiting 3rd Party", "Waiting parts/repair"):
            assert _title_color(build_ticket_card(_ticket(status=status))) == "Good", status

    def test_creation_event_outranks_the_status_name(self):
        card = build_ticket_card(_ticket(status="Closed", event="new_ticket"))
        assert _title_color(card) == "Attention"

    def test_view_ticket_button_uses_the_supplied_url(self):
        actions = next(b for b in build_ticket_card(_ticket()) ["body"] if b["type"] == "ActionSet")["actions"]
        assert actions[0]["type"] == "Action.OpenUrl"
        assert actions[0]["title"] == "VIEW TICKET"
        assert actions[0]["url"] == "https://na.myconnectwise.net/x?service_recid=1234"

    def test_missing_url_falls_back_to_the_connectwise_ticket_url(self):
        ticket = _ticket()
        del ticket["url"]
        actions = next(b for b in build_ticket_card(ticket)["body"] if b["type"] == "ActionSet")["actions"]
        assert actions[0]["url"].endswith("service_recid=1234")

    def test_no_narrative_prose_no_emoji(self):
        card = build_ticket_card(_ticket(summary="Great news everybody! \U0001F389 Fixed!"))
        rendered = json.dumps(card)
        assert "\U0001F389" not in rendered


class TestMissingFieldsDegradeGracefully:
    def test_missing_owner_becomes_unassigned(self):
        rows = _rows(build_ticket_card(_ticket(owner=None)))
        assert rows["Owner"] == "**UNASSIGNED**"

    def test_a_ticket_with_no_number_gets_no_view_button_it_cannot_build(self):
        ticket = _ticket()
        del ticket["url"]
        del ticket["number"]
        actions = [b for b in build_ticket_card(ticket)["body"] if b["type"] == "ActionSet"]
        urls = [a for block in actions for a in block["actions"] if a["type"] == "Action.OpenUrl"]
        assert not urls

    def test_empty_ticket_dict_does_not_raise(self):
        card = build_ticket_card({})
        assert card["type"] == "AdaptiveCard"

    def test_no_field_ever_renders_literal_none(self):
        """Checks rendered TEXT, not raw JSON: "spacing": "None" is an Adaptive
        Card enum the source layout uses and is not a leaked Python value."""
        card = build_ticket_card({"number": None, "priority": None, "board": None, "owner": None, "summary": None})

        def texts(node):
            if isinstance(node, dict):
                if "text" in node and isinstance(node["text"], str):
                    yield node["text"]
                for value in node.values():
                    yield from texts(value)
            elif isinstance(node, list):
                for item in node:
                    yield from texts(item)

        rendered = list(texts(card)) + [card["fallbackText"]]
        assert rendered
        assert not [t for t in rendered if "None" in t]

    def test_no_note_block_when_the_caller_carries_no_note_field(self):
        card = build_ticket_card(_ticket())
        texts = [b.get("text", "") for b in card["body"] if b["type"] == "TextBlock"]
        assert not texts


class TestRenderCardFence:
    def test_fence_matches_adapter_detection_regex(self):
        # Mirrors adapter.py's _CARD_FENCE_RE without importing the adapter
        # module (which requires the microsoft_teams SDK mock to be loaded).
        import re

        pattern = re.compile(
            r"```(?:adaptivecard|adaptive[_-]?card)\s*\n(.*?)```",
            re.DOTALL | re.IGNORECASE,
        )
        card = build_ticket_card(_ticket())
        fenced = render_card_fence(card)
        match = pattern.search(fenced)
        assert match is not None
        parsed = json.loads(match.group(1).strip())
        assert parsed == card

    def test_triple_backtick_in_summary_does_not_break_fence_cron_lane(self):
        """A ticket summary carrying a raw ```code fence``` must not close
        the outer ```adaptivecard fence early. Covers the cron/board-watch
        lane (build_ticket_card)."""
        ticket = _ticket(summary="Repro: run ```kubectl get pods``` and watch it hang")
        fenced = render_card_fence(build_ticket_card(ticket))
        # Only the real opening and closing fence markers should remain.
        assert fenced.count("```") == 2
        assert fenced.startswith("```adaptivecard\n")
        assert fenced.rstrip().endswith("```")
        inner = fenced.split("\n", 1)[1].rsplit("```", 1)[0]
        parsed = json.loads(inner.strip())
        assert "kubectl get pods" in json.dumps(parsed)

    def test_fence_language_tag_in_summary_does_not_break_fence_webhook_lane(self):
        """A ticket summary containing the literal fence opener/language tag
        (```adaptivecard) must not let the outer fence be swallowed or
        prematurely closed. Covers the ConnectWise webhook lane
        (build_triage_card)."""
        ticket = _ticket(
            summary="User pasted ```adaptivecard\n{\"type\": \"AdaptiveCard\"}\n``` into the note"
        )
        fenced = render_card_fence(build_triage_card(ticket))
        assert fenced.count("```") == 2
        inner = fenced.split("\n", 1)[1].rsplit("```", 1)[0]
        parsed = json.loads(inner.strip())
        assert parsed["type"] == "AdaptiveCard"
        assert "adaptivecard" in json.dumps(parsed)


def _trend_kv_rows(card):
    """The Spread/Techs/Devices block as {label: value}, read off ColumnSet
    rows (not Table - Teams mobile only renders Adaptive Cards up to schema
    1.2, and Table needs 1.5+, so the trend card uses ColumnSet instead)."""
    container = next(
        b for b in card["body"]
        if b["type"] == "Container" and b.get("id") == "trend-spread"
    )
    rows = {}
    for row in container["items"]:
        label = row["columns"][0]["items"][0]["text"]
        value = row["columns"][1]["items"][0]["text"]
        rows[label] = value
    return rows


def _trend_member_rows_text(card):
    """The member-ticket block as a list of (left, right) text pairs."""
    container = next(
        b for b in card["body"]
        if b["type"] == "Container" and b.get("id") == "trend-members"
    )
    return [
        (row["columns"][0]["items"][0]["text"], row["columns"][1]["items"][0]["text"])
        for row in container["items"]
    ]


def _trend(**overrides):
    base = {
        "trend_id": "TREND-20260818-a1b2c3",
        "title": "Windows quality updates failing and leaving endpoints in "
        "Startup Repair or BitLocker recovery",
        "confidence": "high",
        "first_seen": "2026-08-04",
        "last_seen": "2026-08-13",
        "ticket_count": 9,
        "device_count": 8,
        "user_count": 8,
        "techs": ["Ernesto Velarde", "Jarvis Williams", "Scarlet Mendoza"],
        "devices": ["GWH-PW0AYB5J", "GWH-PF3RS30T"],
        "why_related": "One sentence naming the shared cause and why the "
        "tickets do not look related on the surface.",
        "recommended_action": "One sentence naming the deeper review being asked for.",
        "tickets": [
            {
                "id": 94792,
                "date": "2026-08-05",
                "summary": "Laptop stuck at startup",
                "evidence": "Tried running a startup repair and uninstalling "
                "the latest quality update.",
            }
        ],
        "escalation_level": 0,
    }
    base.update(overrides)
    return base


class TestBuildTrendCard:
    def test_schema_valid_shape(self):
        card = build_trend_card(_trend())
        assert card["type"] == "AdaptiveCard"
        assert card["version"] == "1.5"
        assert card["targetWidth"] == "Wide"
        assert isinstance(card["body"], list) and card["body"]
        assert "fallbackText" in card and card["fallbackText"]

    def test_backtick_injection_does_not_break_fence(self):
        trend = _trend()
        trend["tickets"][0]["summary"] = "Summary with a ```code fence``` inside it"
        card = build_trend_card(trend)
        fenced = render_card_fence(card)
        assert fenced.count("```") == 2  # only the opening and closing fence markers

    def test_markdown_image_in_evidence_is_stripped(self):
        trend = _trend()
        trend["tickets"][0]["evidence"] = "See attached ![Screenshot](https://x/img.png) for detail."
        card = build_trend_card(trend)
        blob = json.dumps(card)
        assert "![Screenshot]" not in blob
        assert "img.png" not in blob

    def test_twenty_member_tickets_produce_six_rows_plus_rollup(self):
        trend = _trend(
            tickets=[
                {"id": i, "date": "2026-08-05", "summary": f"Ticket {i}", "evidence": ""}
                for i in range(20)
            ]
        )
        card = build_trend_card(trend)
        rows = _trend_member_rows_text(card)
        assert len(rows) == 7  # 6 shown + 1 roll-up
        assert rows[-1] == ("", "+14 more")

    def test_member_row_left_cell_is_ticket_id_and_date_compact(self):
        card = build_trend_card(_trend())
        rows = _trend_member_rows_text(card)
        assert rows[0][0] == "#94792 (2026-08-05)"
        assert rows[0][1] == "Laptop stuck at startup"
        left_column = next(
            b for b in card["body"] if b["type"] == "Container" and b.get("id") == "trend-members"
        )["items"][0]["columns"][0]
        assert left_column.get("width") == "auto"
        assert left_column["items"][0].get("wrap") is False

    def test_no_table_element_in_trend_card(self):
        """Guard against regression: Table needs schema 1.5+, and Teams
        mobile only renders up to 1.2, so the trend card must not emit one."""
        card = build_trend_card(_trend())
        assert "Table" not in json.dumps(card)

    def test_spread_techs_devices_rows(self):
        card = build_trend_card(_trend())
        rows = _trend_kv_rows(card)
        assert rows["Spread"] == (
            "9 tickets - 8 devices - 8 users - 2026-08-04 to 2026-08-13 - confidence high"
        )
        assert rows["Techs"] == "Ernesto Velarde, Jarvis Williams, Scarlet Mendoza"
        assert rows["Devices"] == "GWH-PW0AYB5J, GWH-PF3RS30T"

    def test_techs_row_omitted_when_empty(self):
        card = build_trend_card(_trend(techs=[]))
        rows = _trend_kv_rows(card)
        assert "Techs" not in rows

    def test_why_related_is_not_truncated_at_the_old_issue_cap(self):
        long_why = (
            "Ten tickets in 21 days describe one failure in different words: "
            "BitLocker recovery prompts, startup repair loops, blue screens, "
            "forced resets. Eight distinct machines, no shared configuration "
            "item, so nothing links them in a board scan. Rate tripled: 3 "
            "tickets Jul 29-Aug 8 vs 10 tickets Aug 9-Aug 18."
        )
        assert len(long_why) > 300  # longer than the ticket-note cap on purpose
        card = build_trend_card(_trend(why_related=long_why))
        why_block = next(
            b for b in card["body"]
            if b["type"] == "TextBlock" and b["text"].startswith("Why these are related:")
        )
        assert why_block["text"] == "Why these are related: " + long_why

    def test_empty_devices_omits_devices_row(self):
        card = build_trend_card(_trend(devices=[]))
        blob = json.dumps(card)
        assert "Devices" not in blob

    def test_missing_keys_still_produce_valid_card(self):
        card = build_trend_card({})
        assert card["type"] == "AdaptiveCard"
        assert isinstance(card["body"], list) and card["body"]

    def test_escalation_level_changes_card_visibly(self):
        calm = build_trend_card(_trend(escalation_level=0))
        overdue = build_trend_card(_trend(escalation_level=3))
        assert json.dumps(calm) != json.dumps(overdue)
        assert "UNACKNOWLEDGED" in overdue["body"][0]["text"]
        assert "UNACKNOWLEDGED" not in calm["body"][0]["text"]

    def test_fallback_text_contains_title_and_trend_id(self):
        card = build_trend_card(_trend())
        assert "TREND-20260818-a1b2c3" in card["fallbackText"]
        assert "Windows quality updates failing" in card["fallbackText"]

    def test_ack_action_and_board_link_present(self):
        card = build_trend_card(_trend())
        action_set = next(b for b in card["body"] if b["type"] == "ActionSet")
        types = {a["type"] for a in action_set["actions"]}
        assert types == {"Action.Submit", "Action.OpenUrl"}
        submit = next(a for a in action_set["actions"] if a["type"] == "Action.Submit")
        assert submit["data"] == {"action": "ack_trend", "trend_id": "TREND-20260818-a1b2c3"}

    def test_default_kwargs_produce_identical_card_to_before(self):
        """No kwargs passed -- existing callers (cron/trend_pass.py, every
        other test above) must see byte-identical output."""
        trend = _trend()
        assert build_trend_card(trend) == build_trend_card(trend, kind=None, mention_upns=None)
        assert build_trend_card(trend) == build_trend_card(trend, kind=None, mention_upns=[])

    def test_kind_new_renders_new_trend_label(self):
        card = build_trend_card(_trend(escalation_level=0), kind="new")
        assert card["body"][0]["text"].startswith("NEW TREND")

    def test_kind_update_renders_blast_radius_label(self):
        card = build_trend_card(_trend(escalation_level=0), kind="update")
        assert card["body"][0]["text"].startswith("TREND UPDATE - BLAST RADIUS GREW")

    def test_kind_escalation_renders_unacknowledged_label_with_hours(self):
        card = build_trend_card(_trend(escalation_level=1), kind="escalation", hours_since_raised=12.0)
        assert card["body"][0]["text"].startswith("UNACKNOWLEDGED TREND (12h)")

    def test_kind_escalation_without_hours_omits_hour_count(self):
        card = build_trend_card(_trend(escalation_level=1), kind="escalation")
        assert card["body"][0]["text"].startswith("UNACKNOWLEDGED TREND")
        assert "(None" not in card["body"][0]["text"]

    def test_user_mention_entity_present_for_single_upn(self):
        # Per Microsoft Learn (cards-format.md, "Mention support within
        # Adaptive Cards"): channel/team mentions are NOT supported in bot
        # messages, only user mentions by Entra Object ID or UPN. This is
        # the supported shape.
        card = build_trend_card(_trend(), mention_upns=["jmorgan@henssler.com"])
        entities = card["msteams"]["entities"]
        assert len(entities) == 1
        entity = entities[0]
        assert entity["type"] == "mention"
        assert entity["text"] == "<at>jmorgan</at>"
        assert entity["mentioned"] == {"id": "jmorgan@henssler.com", "name": "jmorgan"}
        # The visible <at> token must appear verbatim in a TextBlock in the
        # body -- Teams only supports mentions in TextBlock/FactSet, and the
        # entity's `text` must match the rendered message body exactly.
        assert "<at>jmorgan</at>" in json.dumps(card["body"])

    def test_user_mention_entities_present_for_multiple_upns(self):
        card = build_trend_card(
            _trend(), mention_upns=["jmorgan@henssler.com", "tech@henssler.com"]
        )
        entities = card["msteams"]["entities"]
        assert len(entities) == 2
        tokens = {e["text"] for e in entities}
        assert tokens == {"<at>jmorgan</at>", "<at>tech</at>"}
        ids = {e["mentioned"]["id"] for e in entities}
        assert ids == {"jmorgan@henssler.com", "tech@henssler.com"}
        body_text = json.dumps(card["body"])
        assert "<at>jmorgan</at>" in body_text
        assert "<at>tech</at>" in body_text

    def test_no_mention_entity_when_mention_upns_omitted(self):
        card = build_trend_card(_trend())
        assert "msteams" not in card

    def test_no_mention_entity_when_mention_upns_empty(self):
        card = build_trend_card(_trend(), mention_upns=[])
        assert "msteams" not in card


class TestSplitAutonomousMessageByTicket:
    """The 2026-09-04 fix: a multi-ticket autonomous send that IS cleanly
    splittable ships as one message per ticket instead of being rejected
    outright. See guard_single_ticket_per_autonomous_message / adapter.py's
    send() for where this plugs in."""

    # Verbatim model output from the real incident (session
    # cron_triage-nag-001_20260904_133005, state.db) that got hard-rejected
    # before this fix even though it was already three clean, independent,
    # well-written per-ticket paragraphs.
    REAL_INCIDENT_TEXT = (
        "[#91590 - Test Disabling Chrome and Edge Password Manager]"
        "(https://na.myconnectwise.net/v4_6_release/services/system_io/Service/"
        "fv_sr100_request.rails?service_recid=91590) \u2014 Henssler Financial. "
        "Eighty-six days, Ernesto. This is literally your lane.\n\n"
        "[#91624 - Elevator phone not working]"
        "(https://na.myconnectwise.net/v4_6_release/services/system_io/Service/"
        "fv_sr100_request.rails?service_recid=91624) \u2014 Henssler Financial, "
        "unassigned, 85 days old.\n\n"
        "[#92701 - HPM Termination - HPM]"
        "(https://na.myconnectwise.net/v4_6_release/services/system_io/Service/"
        "fv_sr100_request.rails?service_recid=92701) \u2014 Henssler Financial. "
        "Sixty-six days, no owner."
    )

    def test_real_incident_text_splits_into_three_clean_messages(self):
        result = split_autonomous_message_by_ticket(self.REAL_INCIDENT_TEXT)
        assert result is not None
        assert len(result) == 3
        assert [m.count("service_recid=") for m in result] == [1, 1, 1]
        assert "91590" in result[0] and "91624" not in result[0] and "92701" not in result[0]
        assert "91624" in result[1]
        assert "92701" in result[2]
        # No content lost: every word of the original survives somewhere.
        assert "Ernesto" in result[0]
        assert "unassigned" in result[1]
        assert "no owner" in result[2]

    def test_leading_ticketless_intro_folds_into_the_first_ticket_message(self):
        content = (
            "Several tickets need attention.\n\n"
            "[#100 - First](url1) \u2014 stale.\n\n"
            "[#200 - Second](url2) \u2014 also stale."
        )
        result = split_autonomous_message_by_ticket(content)
        assert result is not None
        assert len(result) == 2
        assert result[0].startswith("Several tickets need attention.")
        assert "#100" in result[0]
        assert "#200" not in result[0]
        assert result[1] == "[#200 - Second](url2) \u2014 also stale."

    def test_trailing_ticketless_outro_folds_into_the_last_ticket_message(self):
        content = (
            "[#100 - First](url1) \u2014 stale.\n\n"
            "[#200 - Second](url2) \u2014 also stale.\n\n"
            "That's everything for this cycle."
        )
        result = split_autonomous_message_by_ticket(content)
        assert result is not None
        assert len(result) == 2
        assert result[1].endswith("That's everything for this cycle.")
        assert "#100" not in result[1]

    def test_single_block_naming_two_tickets_is_not_split(self):
        """A block that mixes ticket numbers in one paragraph can't be pulled
        apart without guessing which sentence belongs to which ticket --
        the caller must fall back to the hard rejection in this case."""
        content = (
            "[#100 - First](url1) and [#200 - Second](url2) are the same root cause.\n\n"
            "[#300 - Third](url3) \u2014 unrelated, stale."
        )
        assert split_autonomous_message_by_ticket(content) is None

    def test_single_ticket_content_is_not_split(self):
        """Nothing to split when there's only one block -- the normal
        (non-guard) send path already handles this case."""
        assert split_autonomous_message_by_ticket("[#100 - Only one](url1) \u2014 stale.") is None

    def test_content_with_no_ticket_at_all_is_not_split(self):
        content = "First unrelated line.\n\nSecond unrelated line."
        assert split_autonomous_message_by_ticket(content) is None

    def test_empty_content_is_not_split(self):
        assert split_autonomous_message_by_ticket("") is None
        assert split_autonomous_message_by_ticket(None) is None
