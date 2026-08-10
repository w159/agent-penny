"""Fence-to-card rendering contract for the Teams adapter.

Both Teams send paths used to post ```adaptivecard fences verbatim, so a cron
ticket card arrived as a wall of raw JSON text. These tests pin the three
outcomes that matter, on both paths:

  * a fenced card becomes a real card/attachment, not text
  * content with no fence is unchanged plain text
  * malformed card JSON degrades to text carrying the original payload,
    rather than the message being dropped
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_teams import TeamsAdapter, _make_config, _teams_mod

_CARD_JSON = {
    "type": "AdaptiveCard",
    "version": "1.4",
    "body": [{"type": "TextBlock", "text": "#1001 needs attention"}],
}
_FENCE = "```adaptivecard\n" + json.dumps(_CARD_JSON) + "\n```"


def _make_adapter():
    adapter = TeamsAdapter(_make_config())
    adapter._app = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id="m1")),
        reply=AsyncMock(return_value=SimpleNamespace(id="m1")),
    )
    return adapter


# ---------------------------------------------------------------------------
# Live gateway path: TeamsAdapter.send()
# ---------------------------------------------------------------------------

class TestSendRendersFencedCards:
    @pytest.mark.asyncio
    async def test_fenced_card_is_sent_as_a_card_not_text(self):
        adapter = _make_adapter()
        adapter._send_card = AsyncMock(return_value=SimpleNamespace(id="card-1"))

        result = await adapter.send("chat", _FENCE)

        assert result.success is True
        adapter._send_card.assert_awaited_once()
        # The card object carries the parsed JSON, and nothing went out as text.
        sent_card = adapter._send_card.await_args.args[1]
        assert sent_card._data == _CARD_JSON
        adapter._app.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_content_without_a_fence_is_unchanged_plain_text(self):
        adapter = _make_adapter()
        adapter._send_card = AsyncMock()

        result = await adapter.send("chat", "just a plain note")

        assert result.success is True
        adapter._send_card.assert_not_awaited()
        assert adapter._app.send.await_count == 1
        assert adapter._app.send.await_args[0][1] == "just a plain note"

    @pytest.mark.asyncio
    async def test_malformed_card_json_falls_back_to_the_original_text(self):
        adapter = _make_adapter()
        adapter._send_card = AsyncMock()
        broken = "```adaptivecard\n{not valid json,,,}\n```"

        result = await adapter.send("chat", broken)

        assert result.success is True
        adapter._send_card.assert_not_awaited()
        posted = adapter._app.send.await_args[0][1]
        assert "{not valid json,,,}" in posted, "the message must not be dropped"

    @pytest.mark.asyncio
    async def test_prose_around_the_fence_still_reaches_an_interactive_reader(self):
        adapter = _make_adapter()
        adapter._send_card = AsyncMock(return_value=SimpleNamespace(id="card-1"))

        await adapter.send("chat", "Heads up.\n\n" + _FENCE + "\n\nThat is all.")

        adapter._send_card.assert_awaited_once()
        posted = [c[0][1] for c in adapter._app.send.await_args_list]
        assert posted == ["Heads up.", "That is all."]


# ---------------------------------------------------------------------------
# Out-of-process cron path: _standalone_send's activity builder
# ---------------------------------------------------------------------------

class TestStandaloneActivityRendersFencedCards:
    def test_fenced_card_becomes_an_adaptive_card_attachment(self):
        activity = _teams_mod._build_standalone_activity(_FENCE)

        assert activity["attachments"] == [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": _CARD_JSON,
        }]
        assert "text" not in activity, "the fence must not also go out as text"

    def test_no_fence_keeps_todays_markdown_activity(self):
        activity = _teams_mod._build_standalone_activity("plain cron output")

        assert activity == {
            "type": "message",
            "text": "plain cron output",
            "textFormat": "markdown",
        }

    def test_malformed_card_json_falls_back_to_the_original_message(self):
        broken = "```adaptivecard\n{nope}\n```"

        activity = _teams_mod._build_standalone_activity(broken)

        assert activity["text"] == broken
        assert "attachments" not in activity

    def test_prose_around_the_fence_is_carried_alongside_the_attachment(self):
        activity = _teams_mod._build_standalone_activity("Heads up.\n\n" + _FENCE)

        assert len(activity["attachments"]) == 1
        assert activity["text"] == "Heads up."
