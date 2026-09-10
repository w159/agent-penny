"""Tests closing the autonomous card+text duplicate-post bug.

TeamsAdapter.send()'s mixed text+card path used to send the Adaptive Card as
an attachment AND every non-empty narrative text segment as a separate Teams
message. For cron/board-watch (autonomous) sends this meant the same ticket
landed twice: once as a card, once as repeated plain text. Interactive sends
(a human asking a question) must keep the old mixed prose+card behavior.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import AUTONOMOUS_DELIVERY_METADATA_KEY
from tests.gateway.test_teams import TeamsAdapter, _make_config, _teams_mod

_CARD = '{"type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "text": "hi"}]}'


def _mixed_content(lead="Heads up, a ticket needs attention."):
    return lead + "\n\n```adaptivecard\n" + _CARD + "\n```\n\nFollow up note."


def _make_adapter(**extra):
    adapter = TeamsAdapter(_make_config(**extra))
    adapter._app = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id="m1")),
        reply=AsyncMock(return_value=SimpleNamespace(id="m1")),
    )
    return adapter


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(_teams_mod.asyncio, "sleep", _instant)


class TestAutonomousCardSendSuppressesNarrativeText:
    @pytest.mark.anyio
    async def test_autonomous_send_with_card_sends_only_the_card(self):
        adapter = _make_adapter()
        adapter._send_text = AsyncMock(wraps=adapter._send_text)
        adapter._send_card_with_retry = AsyncMock(return_value=SimpleNamespace(id="card-1"))

        result = await adapter.send(
            "chat",
            _mixed_content(),
            metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True},
        )

        assert result.success is True
        adapter._send_card_with_retry.assert_awaited_once()
        adapter._send_text.assert_not_awaited()

    @pytest.mark.anyio
    async def test_autonomous_send_logs_suppressed_segments(self, caplog):
        adapter = _make_adapter()
        adapter._send_card_with_retry = AsyncMock(return_value=SimpleNamespace(id="card-1"))

        with caplog.at_level(logging.INFO, logger=_teams_mod.logger.name):
            await adapter.send(
                "chat",
                _mixed_content(),
                metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True},
            )

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "suppressed" in r.getMessage() and "narrative text segment" in r.getMessage()
            for r in info_records
        ), f"expected an INFO suppression log, got: {[r.getMessage() for r in caplog.records]}"

    @pytest.mark.anyio
    async def test_interactive_send_with_card_keeps_mixed_text_and_card(self):
        """No metadata at all == interactive; a human asking a question still
        gets the surrounding prose plus the card, unchanged."""
        adapter = _make_adapter()
        adapter._send_text = AsyncMock(wraps=adapter._send_text)
        adapter._send_card_with_retry = AsyncMock(return_value=SimpleNamespace(id="card-1"))

        result = await adapter.send("chat", _mixed_content(), metadata=None)

        assert result.success is True
        adapter._send_card_with_retry.assert_awaited_once()
        assert adapter._send_text.await_count == 2, "lead-in and follow-up text must both still send"

    @pytest.mark.anyio
    async def test_interactive_send_metadata_without_autonomous_flag_keeps_mixed(self):
        adapter = _make_adapter()
        adapter._send_text = AsyncMock(wraps=adapter._send_text)
        adapter._send_card_with_retry = AsyncMock(return_value=SimpleNamespace(id="card-1"))

        result = await adapter.send("chat", _mixed_content(), metadata={"importance": "high"})

        assert result.success is True
        assert adapter._send_text.await_count == 2

    @pytest.mark.anyio
    async def test_autonomous_card_failure_still_falls_back_to_text(self):
        """Suppressing narrative text must never suppress the malformed-card
        degrade-to-code-block fallback: real content must never be dropped."""
        adapter = _make_adapter()

        async def always_fail(chat_id, card, importance=None):
            raise RuntimeError("boom")

        adapter._send_card = always_fail
        adapter._send_text = AsyncMock(wraps=adapter._send_text)

        result = await adapter.send(
            "chat",
            _mixed_content(),
            metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True},
        )

        assert result.success is True
        adapter._send_text.assert_awaited_once()
        sent_text = adapter._send_text.await_args.args[1]
        assert _CARD in sent_text, "the fallback code block must carry the card JSON through"

    @pytest.mark.anyio
    async def test_autonomous_send_without_card_is_unchanged(self):
        """No card present: plain-text fast path is untouched by this change."""
        adapter = _make_adapter()
        adapter._send_text = AsyncMock(wraps=adapter._send_text)

        result = await adapter.send(
            "chat",
            "just a plain autonomous note, no ticket card",
            metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True},
        )

        assert result.success is True
        adapter._send_text.assert_awaited_once()
