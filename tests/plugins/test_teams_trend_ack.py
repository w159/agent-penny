"""Tests for the trend card's ACKNOWLEDGE TREND button routing.

``ticket_card.build_trend_card`` emits ``Action.Submit`` (legacy submit),
which the Teams SDK delivers as a normal ``message`` activity carrying
``value`` -- not the ``adaptiveCard/action`` invoke that Action.Execute uses
-- so this path is wired in ``_on_message``, not ``_on_card_action``.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.teams.adapter import TeamsAdapter


def _adapter() -> TeamsAdapter:
    config = PlatformConfig(enabled=True, extra={})
    return TeamsAdapter(config)


def _activity(value, *, from_id="29:user1", from_name="Jane Doe"):
    return SimpleNamespace(
        value=value,
        from_=SimpleNamespace(id=from_id, aad_object_id=None, name=from_name),
    )


class TestOnTrendAckSubmit:
    @pytest.mark.asyncio
    async def test_known_trend_id_calls_record_acknowledgement_with_actor(self):
        adapter = _adapter()
        activity = _activity({"action": "ack_trend", "trend_id": "TREND-1"})

        fake_module = types.ModuleType("cron.trend_state")
        fake_module.record_acknowledgement = SimpleNamespace(calls=[])

        record_mock = AsyncMock(return_value=True)

        with patch.object(adapter, "send", new=AsyncMock(return_value=None)) as send_mock, \
             patch("cron.trend_state.record_acknowledgement", create=True) as rec_mock:
            rec_mock.return_value = True
            await adapter._on_trend_ack_submit(activity, "chat-123")

        rec_mock.assert_called_once_with("TREND-1", "29:user1", "Jane Doe")
        send_mock.assert_awaited_once()
        text = send_mock.await_args.args[1]
        assert "TREND-1" in text
        assert "Jane Doe" in text
        assert "acknowledged" in text.lower()

    @pytest.mark.asyncio
    async def test_unknown_trend_id_replies_not_tracked_and_does_not_raise(self):
        adapter = _adapter()
        activity = _activity({"action": "ack_trend", "trend_id": "TREND-GONE"})

        with patch.object(adapter, "send", new=AsyncMock(return_value=None)) as send_mock, \
             patch("cron.trend_state.record_acknowledgement", create=True) as rec_mock:
            rec_mock.return_value = False
            await adapter._on_trend_ack_submit(activity, "chat-123")

        rec_mock.assert_called_once_with("TREND-GONE", "29:user1", "Jane Doe")
        send_mock.assert_awaited_once()
        text = send_mock.await_args.args[1]
        assert "TREND-GONE" in text
        assert "no longer being tracked" in text.lower()

    @pytest.mark.asyncio
    async def test_import_error_is_handled_and_reported(self):
        adapter = _adapter()
        activity = _activity({"action": "ack_trend", "trend_id": "TREND-1"})

        real_import = __import__

        def _raising_import(name, *args, **kwargs):
            if name == "cron.trend_state":
                raise ImportError("record_acknowledgement not defined yet")
            return real_import(name, *args, **kwargs)

        with patch.object(adapter, "send", new=AsyncMock(return_value=None)) as send_mock, \
             patch("builtins.__import__", side_effect=_raising_import):
            await adapter._on_trend_ack_submit(activity, "chat-123")

        send_mock.assert_awaited_once()
        text = send_mock.await_args.args[1]
        assert "TREND-1" in text
        assert "fail" in text.lower()

    @pytest.mark.asyncio
    async def test_missing_trend_id_does_not_call_record_acknowledgement(self):
        adapter = _adapter()
        activity = _activity({"action": "ack_trend"})

        with patch.object(adapter, "send", new=AsyncMock(return_value=None)) as send_mock, \
             patch("cron.trend_state.record_acknowledgement", create=True) as rec_mock:
            await adapter._on_trend_ack_submit(activity, "chat-123")

        rec_mock.assert_not_called()
        send_mock.assert_awaited_once()
