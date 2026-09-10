"""Tests for the Teams adapter's card-send retry/error-logging fix.

A card failure used to vanish into a single WARNING and silently degrade to
text (8 HTTP 429s did exactly this on 2026-07-27, undetected). This module
proves: (1) a 429 gets retried with backoff instead of failing on the first
attempt, and (2) once retries are exhausted the fallback-to-text path logs
at ERROR, not WARNING.

Reuses the SDK mock bootstrap from test_teams.py (installing the real
microsoft_teams mock is expensive to duplicate).
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_teams import TeamsAdapter, _make_config, _teams_mod


class _HttpError(Exception):
    """Duck-typed like httpx.HTTPStatusError: exposes .response.status_code."""

    def __init__(self, status_code, retry_after=None):
        super().__init__(f"HTTP {status_code}")
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        self.response = SimpleNamespace(status_code=status_code, headers=headers, text="rate limited")


def _make_adapter(**extra):
    adapter = TeamsAdapter(_make_config(**extra))
    adapter._app = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id="m1")),
        reply=AsyncMock(return_value=SimpleNamespace(id="m1")),
    )
    return adapter


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Retry backoff really sleeps; tests shouldn't take seconds to run."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(_teams_mod.asyncio, "sleep", _instant)


class TestSendCardWithRetry:
    @pytest.mark.anyio
    async def test_429_is_retried_not_failed_on_first_attempt(self):
        adapter = _make_adapter()
        calls = []

        async def flaky(chat_id, card, importance=None):
            calls.append(1)
            if len(calls) == 1:
                raise _HttpError(429)
            return SimpleNamespace(id="ok")

        adapter._send_card = flaky
        result = await adapter._send_card_with_retry("chat", card=object())

        assert result.id == "ok"
        assert len(calls) == 2, "expected exactly one retry after the 429"

    @pytest.mark.anyio
    async def test_5xx_is_retried(self):
        adapter = _make_adapter()
        calls = []

        async def flaky(chat_id, card, importance=None):
            calls.append(1)
            if len(calls) == 1:
                raise _HttpError(503)
            return SimpleNamespace(id="ok")

        adapter._send_card = flaky
        result = await adapter._send_card_with_retry("chat", card=object())

        assert result.id == "ok"
        assert len(calls) == 2

    @pytest.mark.anyio
    async def test_retries_are_capped_then_raises(self):
        adapter = _make_adapter()
        calls = []

        async def always_429(chat_id, card, importance=None):
            calls.append(1)
            raise _HttpError(429)

        adapter._send_card = always_429
        with pytest.raises(_HttpError):
            await adapter._send_card_with_retry("chat", card=object())

        assert len(calls) == adapter._CARD_MAX_RETRIES + 1

    @pytest.mark.anyio
    async def test_non_retryable_status_raises_immediately(self):
        adapter = _make_adapter()
        calls = []

        async def bad_request(chat_id, card, importance=None):
            calls.append(1)
            raise _HttpError(400)

        adapter._send_card = bad_request
        with pytest.raises(_HttpError):
            await adapter._send_card_with_retry("chat", card=object())

        assert len(calls) == 1, "a 400 is not transient, must not be retried"

    @pytest.mark.anyio
    async def test_retry_after_header_is_respected(self, monkeypatch):
        adapter = _make_adapter()
        seen_delays = []

        async def capture_sleep(seconds):
            seen_delays.append(seconds)

        monkeypatch.setattr(_teams_mod.asyncio, "sleep", capture_sleep)

        calls = []

        async def flaky(chat_id, card, importance=None):
            calls.append(1)
            if len(calls) == 1:
                raise _HttpError(429, retry_after=2.5)
            return SimpleNamespace(id="ok")

        adapter._send_card = flaky
        await adapter._send_card_with_retry("chat", card=object())

        assert seen_delays == [2.5]


class TestCardSendFailureLogging:
    @pytest.mark.anyio
    async def test_exhausted_retries_degrade_to_text_and_log_error(self, caplog):
        adapter = _make_adapter()

        async def always_429(chat_id, card, importance=None):
            raise _HttpError(429)

        adapter._send_card = always_429

        with caplog.at_level(logging.ERROR, logger=_teams_mod.logger.name):
            result = await adapter.send("chat", "```adaptivecard\n{\"type\": \"AdaptiveCard\"}\n```")

        assert result.success is True
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "card-send failure after exhausted retries must log at ERROR"
        assert any("429" in r.getMessage() or "429" in str(getattr(r, "status", "")) for r in error_records) or \
            any("status=429" in r.getMessage() for r in error_records)
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING and "adaptive card" in r.getMessage()]
        assert not warning_records, "the old silent WARNING downgrade must be gone"


class TestCardSendFailureFallbackText:
    """A failed card send must never dump raw AdaptiveCard JSON into the chat."""

    @pytest.mark.anyio
    async def test_uses_card_fallback_text_when_present(self):
        adapter = _make_adapter()

        async def always_429(chat_id, card, importance=None):
            raise _HttpError(429)

        adapter._send_card = always_429
        card = {
            "type": "AdaptiveCard",
            "fallbackText": "Ticket #1234 (New, High)",
            "body": [{"type": "TextBlock", "text": "irrelevant if fallbackText is set"}],
        }
        sent = []
        adapter._send_text = AsyncMock(side_effect=lambda chat_id, text, reply_to=None: sent.append(text) or "m1")

        result = await adapter.send("chat", "```adaptivecard\n" + json.dumps(card) + "\n```")

        assert result.success is True
        assert len(sent) == 1
        assert sent[0] == "Ticket #1234 (New, High)"
        assert "AdaptiveCard" not in sent[0]
        assert "$schema" not in sent[0]

    @pytest.mark.anyio
    async def test_falls_back_to_first_textblock_without_fallback_text(self):
        adapter = _make_adapter()

        async def always_429(chat_id, card, importance=None):
            raise _HttpError(429)

        adapter._send_card = always_429
        card = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": [{"type": "TextBlock", "text": "Ticket #5678 needs attention"}],
        }
        sent = []
        adapter._send_text = AsyncMock(side_effect=lambda chat_id, text, reply_to=None: sent.append(text) or "m1")

        result = await adapter.send("chat", "```adaptivecard\n" + json.dumps(card) + "\n```")

        assert result.success is True
        assert sent[0] == "Ticket #5678 needs attention"
        assert "AdaptiveCard" not in sent[0]
        assert "$schema" not in sent[0]

    @pytest.mark.anyio
    async def test_generic_message_when_card_shaped_json_has_no_fallback_or_textblock(self):
        """Card-shaped (parses, has a "type") but neither field is present -> generic notice."""
        adapter = _make_adapter()

        async def always_429(chat_id, card, importance=None):
            raise _HttpError(429)

        adapter._send_card = always_429
        card = {"type": "AdaptiveCard", "body": []}
        sent = []
        adapter._send_text = AsyncMock(side_effect=lambda chat_id, text, reply_to=None: sent.append(text) or "m1")

        result = await adapter.send("chat", "```adaptivecard\n" + json.dumps(card) + "\n```")

        assert result.success is True
        assert sent[0] == "A card could not be displayed here. Check the Triage board directly."
        assert "AdaptiveCard" not in sent[0]
        assert "$schema" not in sent[0]

    @pytest.mark.anyio
    async def test_unparsable_fence_content_is_passed_through_unchanged(self):
        """Fence content that isn't even valid JSON is very likely not a card at
        all (an ordinary code block that happened to open the fence) -- it must
        reach the user unchanged, fenced, exactly like before this fix. Never
        swallow real content behind the "could not be displayed" notice.
        """
        adapter = _make_adapter()
        adapter._send_card = AsyncMock()  # must never be reached
        sent = []
        adapter._send_text = AsyncMock(side_effect=lambda chat_id, text, reply_to=None: sent.append(text) or "m1")

        broken = "{not valid json,,,}"
        result = await adapter.send("chat", "```adaptivecard\n" + broken + "\n```")

        assert result.success is True
        adapter._send_card.assert_not_awaited()
        assert broken in sent[0], "non-card fence content must not be dropped or replaced"
