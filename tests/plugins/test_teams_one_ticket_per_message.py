"""Tests making "one autonomous message per ticket" a structural property.

Headline acceptance test: 5 tickets in -> 5 distinct sends out, and an
autonomous message that still names 2+ tickets is hard-rejected instead of
silently trimmed.
"""

import json

import pytest

from gateway.platforms.base import AUTONOMOUS_DELIVERY_METADATA_KEY
from plugins.platforms.teams.paced_send import send_paced
from plugins.platforms.teams.ticket_card import (
    MultiTicketAutonomousError,
    build_ticket_card,
    guard_single_ticket_per_autonomous_message,
    split_tickets_to_messages,
)


def _tickets(n):
    return [
        {
            "number": str(9000 + i),
            "priority": "Medium",
            "board": "Service Board",
            "owner": None,
            "summary": f"Ticket {9000 + i} needs attention",
            "url": f"https://na.myconnectwise.net/x?service_recid={9000 + i}",
        }
        for i in range(n)
    ]


class TestSplitTicketsToMessages:
    def test_returns_exactly_one_message_per_ticket(self):
        tickets = _tickets(5)
        messages = split_tickets_to_messages(tickets)
        assert len(messages) == 5

    def test_each_message_names_exactly_one_ticket(self):
        tickets = _tickets(5)
        messages = split_tickets_to_messages(tickets)
        for message, ticket in zip(messages, tickets):
            assert f'Ticket #{ticket["number"]} : ' in message

    @pytest.mark.anyio
    async def test_headline_five_tickets_in_five_distinct_sends_out(self):
        """The acceptance test: N tickets in, N sends out, N distinct numbers."""
        tickets = _tickets(5)
        messages = split_tickets_to_messages(tickets)

        sent = []

        async def fake_send(message):
            sent.append(message)
            return {"ok": True}

        results = await send_paced(fake_send, messages, delay_s=0)

        assert len(sent) == 5
        assert len(results) == 5
        # The heading lives in the title column of the icon+title ColumnSet.
        numbers = {
            json.loads(m.split("\n", 1)[1].rsplit("```", 1)[0])["body"][0]["columns"][1][
                "items"
            ][0]["text"].split(" : ")[0]
            for m in sent
        }
        assert numbers == {f"Ticket #{9000 + i}" for i in range(5)}


class TestAutonomousSingleTicketGuard:
    def _autonomous_metadata(self):
        return {AUTONOMOUS_DELIVERY_METADATA_KEY: True}

    def test_rejects_autonomous_message_naming_two_tickets(self):
        content = "Two tickets need attention: #1001 and #1002."
        with pytest.raises(MultiTicketAutonomousError):
            guard_single_ticket_per_autonomous_message(content, self._autonomous_metadata())

    def test_allows_autonomous_message_naming_one_ticket(self):
        content = "Ticket #1001 needs attention."
        guard_single_ticket_per_autonomous_message(content, self._autonomous_metadata())  # no raise

    def test_allows_interactive_multi_ticket_reply(self):
        # "show me the whole board" must not be blocked: no autonomous metadata.
        content = "Here's the whole board: #1001, #1002, #1003."
        guard_single_ticket_per_autonomous_message(content, {})  # no raise

    def test_allows_interactive_multi_ticket_reply_with_no_metadata(self):
        content = "Here's the whole board: #1001, #1002, #1003."
        guard_single_ticket_per_autonomous_message(content, None)  # no raise

    def test_card_fence_for_a_single_ticket_passes_the_guard(self):
        card = build_ticket_card(_tickets(1)[0])
        fence = json.dumps(card)
        guard_single_ticket_per_autonomous_message(fence, self._autonomous_metadata())  # no raise
