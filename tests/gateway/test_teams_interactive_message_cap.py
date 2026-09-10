"""Tests for the Teams cap on interactive (reply-to-a-human) messages.

A 2528-char single reply was the "mile-long message" the owner complained
about. ``_cap_autonomous_message`` only trimmed unsolicited cron output;
interactive replies went out uncapped via ``truncate_message()``'s chunking.
``_cap_interactive_message`` closes that gap using the same
``trim_to_item_boundary`` helper (item/word-boundary cut, explicit "not
shown" marker, never mid-sentence).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import AUTONOMOUS_DELIVERY_METADATA_KEY

from tests.gateway.test_teams import TeamsAdapter, _make_config

CAP = TeamsAdapter.INTERACTIVE_MESSAGE_CHAR_CAP


def _make_adapter(**extra):
    adapter = TeamsAdapter(_make_config(**extra))
    adapter._app = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id="m1")),
        reply=AsyncMock(return_value=SimpleNamespace(id="m1")),
    )
    return adapter


class TestInteractiveMessageCap:
    @pytest.mark.asyncio
    async def test_reply_over_cap_is_trimmed_at_a_boundary_with_marker(self):
        adapter = _make_adapter()
        # Real-shape 2528-char reply: a lead line plus sentence-separated
        # ticket narration, matching the owner-reported wall of text.
        sentence = "Ticket #94325 was resolved last month and is not a trend. "
        source = "Status update:\n\n" + (sentence * 45)
        assert len(source) > 2500

        result = await adapter.send("chat", source, metadata={"thread_id": "42"})

        assert result.success
        assert adapter._app.send.await_count == 1
        posted = adapter._app.send.await_args[0][1]
        assert len(posted) <= CAP
        assert posted.endswith("not shown")
        # Whatever text precedes the marker must be a verbatim prefix of the
        # source -- the cut never rewrites or splices content mid-sentence.
        body = posted.rsplit("+", 1)[0].rstrip("\n")
        assert source.startswith(body), "retained text must be a verbatim prefix"

    @pytest.mark.asyncio
    async def test_reply_under_cap_passes_through_byte_identical(self):
        adapter = _make_adapter()
        source = "Short answer: ticket #94325 is resolved."

        result = await adapter.send("chat", source, metadata={"thread_id": "42"})

        assert result.success
        posted = adapter._app.send.await_args[0][1]
        assert posted == source

    @pytest.mark.asyncio
    async def test_autonomous_cap_unchanged_by_interactive_cap(self):
        # _cap_autonomous_message's own 1500-char cap and behavior must be
        # untouched: an autonomous single-ticket send still trims under its
        # own cap, not the (smaller) interactive cap.
        adapter = _make_adapter()
        sentence = "Extra detail about this one ticket. "
        # Repeat count sized against the cap itself (not a fixed literal) so
        # this fixture keeps exceeding AUTONOMOUS_MESSAGE_CHAR_CAP if that
        # value is retuned again.
        repeats = (TeamsAdapter.AUTONOMOUS_MESSAGE_CHAR_CAP // len(sentence)) + 10
        source = "One ticket needs attention.\n\n[#9000 - Ticket](url) - " + (
            sentence * repeats
        )
        assert len(source) > TeamsAdapter.AUTONOMOUS_MESSAGE_CHAR_CAP

        result = await adapter.send(
            "chat", source, metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
        )

        assert result.success
        posted = adapter._app.send.await_args[0][1]
        assert len(posted) <= TeamsAdapter.AUTONOMOUS_MESSAGE_CHAR_CAP
