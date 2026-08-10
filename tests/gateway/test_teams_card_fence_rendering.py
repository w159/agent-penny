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


# ---------------------------------------------------------------------------
# Fence-shape tolerance: _CARD_FENCE_RE / _split_card_segments
# ---------------------------------------------------------------------------

# Verbatim wire content from the 2026-08-10 incident (ticket 95140), recovered
# from state.db delivery_obligations. The model copied a one-line fence out of
# a YAML-folded prompt example, the newline-only regex saw no card, and all
# 1118 characters of this went to the Teams channel as literal text.
_WIRE_LEAD = (
    "Karis Simpson's PC just blue-screened and is asking for a reco - looks "
    "like the checkscanner's revenge tour continues, and it's still unassigned."
)
_WIRE_CARD_JSON = (
    '{"type":"AdaptiveCard","$schema":"http://adaptivecards.io/schemas/adaptive-card.json",'
    '"version":"1.4","body":[{"type":"Container","style":"attention","items":[{"type":"TextBlock",'
    '"text":"Blocked - New Triage Ticket","weight":"Bolder","size":"Small","color":"attention",'
    '"spacing":"None"},{"type":"TextBlock","text":"[#95140 - Karis Simpson\'s computer restarted '
    "and has a blue screen when turned back on. It's asking for a reco](https://na.myconnectwise.net"
    '/v4_6_release/services/system_io/Service/fv_sr100_request.rails?service_recid=95140)",'
    '"wrap":true,"weight":"Bolder","size":"Medium"}]},{"type":"FactSet","facts":['
    '{"title":"Company","value":"Henssler Financial"},{"title":"Contact","value":"Courtney Richardson"},'
    '{"title":"Priority","value":"Priority 3 - Medium"},{"title":"Owner","value":"**UNASSIGNED**"}]}],'
    '"actions":[{"type":"Action.Execute","title":"I\'ve got it","verb":"penny_cw_assign",'
    '"data":{"penny_action":"cw_assign","ticket_id":95140}}]}'
)
_WIRE_CONTENT = _WIRE_LEAD + "\n\n```adaptivecard " + _WIRE_CARD_JSON + "```"


def _cards(content):
    return [p for kind, p in _teams_mod._split_card_segments(content) if kind == "card"]


class TestFenceShapeTolerance:
    def test_the_real_1118_char_wire_content_now_yields_a_card(self):
        assert len(_WIRE_CONTENT) == 1118, "fixture must stay byte-identical to the incident"

        cards = _cards(_WIRE_CONTENT)

        assert len(cards) == 1
        assert json.loads(cards[0])["type"] == "AdaptiveCard"
        # The prose still travels as text, and the fence never does.
        texts = [p for kind, p in _teams_mod._split_card_segments(_WIRE_CONTENT) if kind == "text"]
        assert "".join(texts).strip() == _WIRE_LEAD

    def test_one_line_fence_parses(self):
        content = "```adaptivecard " + json.dumps(_CARD_JSON) + "```"

        assert [json.loads(c) for c in _cards(content)] == [_CARD_JSON]

    def test_newline_fence_still_parses_identically(self):
        # The shape ticket_card.render_card_fence produces must not regress.
        from plugins.platforms.teams.ticket_card import render_card_fence

        assert [json.loads(c) for c in _cards(render_card_fence(_CARD_JSON))] == [_CARD_JSON]
        assert [json.loads(c) for c in _cards(_FENCE)] == [_CARD_JSON]

    def test_backticks_inside_a_json_string_value_survive(self):
        card = dict(_CARD_JSON, body=[{"type": "TextBlock", "text": "run `ipconfig ``/all`` now"}])
        content = "```adaptivecard\n" + json.dumps(card) + "\n```"

        parsed = [json.loads(c) for c in _cards(content)]

        assert parsed == [card]

    def test_text_only_message_yields_no_card(self):
        assert _cards("no fence here, just a sentence about adaptivecard stuff") == []

    def test_a_following_plain_code_block_is_not_swallowed(self):
        content = _FENCE + "\nthen\n```\nplain code\n```"

        segments = _teams_mod._split_card_segments(content)

        assert [k for k, _ in segments] == ["card", "text"]
        assert json.loads(segments[0][1]) == _CARD_JSON
        assert "plain code" in segments[1][1]

    def test_a_lookalike_tag_is_not_treated_as_a_card(self):
        assert _cards("```adaptivecardish {\"type\": \"AdaptiveCard\"}```") == []
