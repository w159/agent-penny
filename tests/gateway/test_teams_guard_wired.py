"""Guard-wiring test: guard_single_ticket_per_autonomous_message must be
called from the live TeamsAdapter.send() path, not merely exist as an
importable primitive.

Before this change, ticket_card.guard_single_ticket_per_autonomous_message
was tested in isolation (tests/plugins/test_teams_one_ticket_per_message.py)
but never invoked from adapter.send() itself, so a real autonomous message
naming 2+ tickets would still go out. This test exercises adapter.send()
directly and would fail if the guard call were removed from send().
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import AUTONOMOUS_DELIVERY_METADATA_KEY
from plugins.platforms.teams.ticket_card import MultiTicketAutonomousError

from tests.gateway.test_teams import TeamsAdapter, _make_config


def _make_adapter(**extra):
    adapter = TeamsAdapter(_make_config(**extra))
    adapter._app = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id="m1")),
        reply=AsyncMock(return_value=SimpleNamespace(id="m1")),
    )
    return adapter


class TestGuardWiredIntoSend:
    @pytest.mark.asyncio
    async def test_autonomous_multi_ticket_send_is_rejected(self):
        adapter = _make_adapter()
        content = "Two tickets need attention: #1001 and #1002."

        with pytest.raises(MultiTicketAutonomousError):
            await adapter.send(
                "chat", content, metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
            )

        # Rejected before ever reaching the network.
        adapter._app.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_autonomous_single_ticket_send_still_goes_out(self):
        adapter = _make_adapter()
        content = "Ticket #1001 needs attention."

        result = await adapter.send(
            "chat", content, metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
        )

        assert result.success
        adapter._app.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_interactive_multi_ticket_reply_is_exempt(self):
        adapter = _make_adapter()
        content = "Here's the whole board: #1001, #1002, #1003."

        result = await adapter.send("chat", content, metadata={"thread_id": "42"})

        assert result.success
        adapter._app.send.assert_awaited()
