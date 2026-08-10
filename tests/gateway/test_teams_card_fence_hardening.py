"""Nothing in a ConnectWise ticket, and nothing a model writes, may break a card.

Two defects sat behind this file. Backticks: ``json.dumps`` escapes quotes and
backslashes but not backticks, so a pasted log or stack trace in a ticket field
closed the ```adaptivecard fence early and spilled the rest of the payload into
the help-desk channel as raw text. And the relaxed fence regex (one-line fence
tolerance, added after ticket 95140 posted raw JSON): an unterminated
```adaptivecard opener earlier in a message started a match that closed on the
REAL card's opening fence, so the good card was destroyed by a bad one.

The fence regex cases (a-h) are pinned against the adapter module the gateway
actually loads (``plugin_adapter_teams``), not a second import of the same file.
"""

import json

import pytest

from plugins.platforms.teams.ticket_card import (
    build_ticket_card,
    build_triage_card,
    render_card_fence,
    render_triage_message,
)
from tests.gateway.test_teams import _teams_mod

_split = _teams_mod._split_card_segments

_CARD = {"type": "AdaptiveCard", "version": "1.4",
         "body": [{"type": "TextBlock", "text": "#1001"}]}
_JSON = json.dumps(_CARD)


def _cards(message):
    """Every card segment's parsed payload; raises if any card is unparseable."""
    return [json.loads(payload)
            for kind, payload in _split(message) if kind == "card"]


def _texts(message):
    return [payload for kind, payload in _split(message)
            if kind == "text" and payload.strip()]


# ---------------------------------------------------------------------------
# Fence regex contract, cases a-h
# ---------------------------------------------------------------------------

class TestFenceRegexContract:
    def test_a_one_line_fence_parses(self):
        # The ticket-95140 shape: a YAML-folded example the model copied.
        assert _cards("```adaptivecard " + _JSON + "```") == [_CARD]

    def test_b_python_shape_parses_identically(self):
        assert _cards("```adaptivecard\n" + _JSON + "\n```") == [_CARD]

    def test_c_unterminated_opener_does_not_steal_the_real_card(self):
        for stray in ("```adaptivecard {\"type\": \"Adaptive",  # truncated inline
                      "```adaptivecard\nhalf a card\n"):        # truncated block
            message = stray + "\n\nhere it is\n\n```adaptivecard\n" + _JSON + "\n```"
            segments = _split(message)
            assert _cards(message) == [_CARD], stray
            assert sum(1 for kind, _ in segments if kind == "card") == 1, stray
            # The stray opener stays inert text; no raw JSON tail is left over.
            assert not any(_JSON in payload for payload in _texts(message)), stray

    def test_d_a_following_plain_code_block_is_not_swallowed(self):
        for tail in ("```json\n{\"a\": 1}\n```", "```\nplain\n```"):
            message = "```adaptivecard\n" + _JSON + "\n```\n\n" + tail
            assert _cards(message) == [_CARD], tail
            assert tail in "\n".join(_texts(message)), tail

    def test_e_adaptivecardish_is_not_a_card(self):
        message = "```adaptivecardish\n" + _JSON + "\n```"
        assert _cards(message) == []

    def test_f_two_cards_both_parse(self):
        second = dict(_CARD, version="1.5")
        message = ("```adaptivecard\n" + _JSON + "\n```\n\nand\n\n"
                   "```adaptivecard\n" + json.dumps(second) + "\n```")
        assert _cards(message) == [_CARD, second]

    def test_g_single_backtick_inside_a_string_value_is_fine(self):
        card = dict(_CARD, body=[{"type": "TextBlock", "text": "run `ls -l` first"}])
        message = "```adaptivecard\n" + json.dumps(card) + "\n```"
        assert _cards(message) == [card]

    def test_h_unterminated_fence_with_no_real_card_is_plain_text(self):
        message = "```adaptivecard\n{\"type\": \"Adaptive"
        segments = _split(message)
        assert [kind for kind, _ in segments] == ["text"]
        assert segments[0][1] == message


# ---------------------------------------------------------------------------
# Poisoned ticket data, both lanes
# ---------------------------------------------------------------------------

POISON = "see log ```\n{\"boom\": 1}\"}]} ``` end"

_CRON_FIELDS = ("summary", "company", "contact", "priority", "owner")
_TRIAGE_FIELDS = ("summary", "company", "contact", "priority", "owner")


def _cron_ticket(field):
    ticket = {"number": 95140, "summary": "disk full", "company": "Acme",
              "contact": "Jane", "priority": "High", "owner": "Bob",
              "status": "New", "board": "Service"}
    ticket[field] = POISON
    return ticket


def _triage_ticket(field):
    ticket = {"event": "new_ticket", "ticket_id": 95140, "summary": "disk full",
              "company": "Acme", "contact": "Jane", "priority": "High",
              "owner": "Bob"}
    ticket[field] = POISON
    return ticket


def _assert_exactly_one_clean_card(message):
    segments = _split(message)
    cards = [payload for kind, payload in segments if kind == "card"]
    assert len(cards) == 1, segments
    parsed = json.loads(cards[0])  # must be valid JSON, not truncated
    leftovers = [payload for kind, payload in segments if kind == "text"]
    assert not any("AdaptiveCard" in payload for payload in leftovers), leftovers
    return parsed


class TestPoisonedTicketFields:
    @pytest.mark.parametrize("field", _CRON_FIELDS)
    def test_cron_lane_survives_a_code_fence_in_any_field(self, field):
        source = build_ticket_card(_cron_ticket(field))
        message = render_card_fence(source)
        # The escape is transparent: whatever the tech typed comes back byte
        # for byte, and the only backticks left in the message are the fence.
        assert _assert_exactly_one_clean_card(message) == source
        assert "```" not in message.replace("```adaptivecard", "").replace("\n```", "")

    @pytest.mark.parametrize("field", _TRIAGE_FIELDS)
    def test_webhook_lane_survives_a_code_fence_in_any_field(self, field):
        ticket = _triage_ticket(field)
        message = render_triage_message("BLOCKED\nno tech free", ticket)
        assert _assert_exactly_one_clean_card(message) == build_triage_card(
            ticket, blocked=True
        )
        assert "```" not in message.replace("```adaptivecard", "").replace("\n```", "")

    def test_escaped_payload_round_trips_to_the_original_text(self):
        card = build_triage_card(_triage_ticket("summary"))
        message = render_card_fence(card)
        payload = [p for kind, p in _split(message) if kind == "card"][0]
        assert json.loads(payload) == card

    def test_ticket_summary_used_as_prose_cannot_open_a_stray_fence(self):
        # Verdict-only reply: the summary becomes the message body, outside
        # the card fence, so backtick runs are neutralized there.
        message = render_triage_message("BLOCKED", _triage_ticket("summary"))
        prose = message.split("```adaptivecard")[0]
        assert "```" not in prose
        _assert_exactly_one_clean_card(message)


# ---------------------------------------------------------------------------
# No input produces an empty delivered message
# ---------------------------------------------------------------------------

class TestNeverEmpty:
    @pytest.mark.parametrize("event", ["closed", "reopened", "unknown", "new_ticket", ""])
    @pytest.mark.parametrize("content", ["BLOCKED", "ROUTINE", "ROUTINE\n   ", "", "   "])
    def test_no_reply_shape_delivers_an_empty_message(self, event, content):
        ticket = {"event": event, "ticket_id": 95140, "summary": "disk full"}
        assert render_triage_message(content, ticket).strip()

    def test_verdict_only_on_a_closed_event_delivers_the_summary(self):
        message = render_triage_message(
            "BLOCKED", {"event": "closed", "ticket_id": 95140, "summary": "disk full"}
        )
        assert message == "disk full"

    def test_missing_summary_still_delivers_something(self):
        assert render_triage_message("BLOCKED", {"event": "closed"}) == "(no summary)"
