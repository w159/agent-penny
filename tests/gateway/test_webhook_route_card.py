"""The webhook lane hands the model's verdict to a Python card builder.

A route opting in with ``card: cw_ticket`` gets its Adaptive Card built by
``ticket_card.render_triage_message`` at send() time, from the payload the
route script produced. These tests cover the wiring, not the card shape
(that lives in tests/plugins/test_teams_triage_card.py): the payload reaches
the builder, ``[SILENT]`` still suppresses delivery before the builder runs,
routes without ``card:`` are untouched, and a builder blowing up degrades to
the model's own text rather than losing the message.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter, _apply_route_card


CHAT_ID = "webhook:cw:delivery-1"

PAYLOAD = {
    "event": "new_ticket",
    "ticket_id": 94822,
    "summary": "Cannot get into SharePoint",
    "company": "Henssler Financial",
    "contact": "Karis Simpson",
    "priority": "Priority 3 - Medium",
    "owner": "",
    "unassigned": True,
    "url": "https://na.example/ticket?service_recid=94822",
}


def _adapter_with_delivery(delivery):
    adapter = WebhookAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}})
    )
    adapter._delivery_info[CHAT_ID] = delivery

    target = AsyncMock()
    target.send = AsyncMock(return_value=SendResult(success=True))
    runner = MagicMock()
    runner.adapters = {Platform("telegram"): target}
    runner._authorization_adapter = MagicMock(return_value=target)
    runner.config.get_home_channel.return_value = None
    adapter.gateway_runner = runner
    return adapter, target


def _card_delivery():
    return {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "19:abc@thread.v2"},
        "card": "cw_ticket",
        "payload": PAYLOAD,
    }


@pytest.mark.asyncio
async def test_verdict_reply_reaches_teams_as_exactly_one_valid_card():
    adapter, target = _adapter_with_delivery(_card_delivery())

    result = await adapter.send(
        CHAT_ID, "BLOCKED\nKaris is locked out of SharePoint and nobody owns it."
    )

    assert result.success
    sent = target.send.await_args.args[1]
    # One activity, no prose beside it: the adapter posts each segment
    # outside the fence as its own Teams message, and the model's prose is
    # never rendered - only its verdict token picks the card's styling.
    assert sent.startswith("```adaptivecard")
    assert sent.rstrip().endswith("```")
    body = sent.split("```adaptivecard", 1)[1].rsplit("```", 1)[0]
    card = json.loads(body)
    assert card["body"][0]["columns"][1]["items"][0]["text"].startswith("BLOCKED - ")
    action_set = next(b for b in card["body"] if b["type"] == "ActionSet")
    claim = next(a for a in action_set["actions"] if a["type"] == "Action.Execute")
    assert claim["data"]["ticket_id"] == 94822


@pytest.mark.asyncio
async def test_silent_reply_still_suppresses_delivery_entirely():
    adapter, target = _adapter_with_delivery(_card_delivery())

    result = await adapter.send(CHAT_ID, "[SILENT]")

    assert result.success
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_with_trailing_explanation_still_suppresses():
    adapter, target = _adapter_with_delivery(_card_delivery())

    result = await adapter.send(
        CHAT_ID, "[SILENT]\nRoutine close, nothing worth interrupting anyone for."
    )

    assert result.success
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_without_a_card_key_delivers_content_unchanged():
    delivery = _card_delivery()
    delivery.pop("card")
    adapter, target = _adapter_with_delivery(delivery)

    await adapter.send(CHAT_ID, "ROUTINE\nA plain message.")

    assert target.send.await_args.args[1] == "ROUTINE\nA plain message."


def test_unknown_builder_name_leaves_content_alone():
    assert _apply_route_card("hello", {"card": "nope", "payload": PAYLOAD}) == "hello"


def test_builder_failure_degrades_to_the_models_text():
    with patch(
        "plugins.platforms.teams.ticket_card.render_triage_message",
        side_effect=RuntimeError("boom"),
    ):
        content = "ROUTINE\nSomething happened."
        assert _apply_route_card(content, _card_delivery()) == content


def test_missing_payload_does_not_raise():
    """A card is still attached - it just has nothing but placeholders in it.

    Delivering prose with no card would reintroduce the shape this lane
    replaced, so an empty payload degrades the card's CONTENT, never its
    presence.
    """
    out = _apply_route_card("ROUTINE\nprose", {"card": "cw_ticket"})
    assert out.startswith("```adaptivecard")
    assert out.count("```adaptivecard") == 1


@pytest.mark.asyncio
async def test_posting_the_route_stores_card_and_payload_for_send():
    """send() can only build a card if _handle_webhook stored the payload.

    Drives a real POST through the handler so the storage step is exercised
    rather than asserted about, then replays the model's reply through send().
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from gateway.platforms.webhook import _INSECURE_NO_AUTH

    routes = {
        "cw": {
            "secret": _INSECURE_NO_AUTH,
            "card": "cw_ticket",
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "19:abc@thread.v2"},
            "prompt": "{summary}",
        }
    }
    adapter = WebhookAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": routes})
    )
    target = AsyncMock()
    target.send = AsyncMock(return_value=SendResult(success=True))
    runner = MagicMock()
    runner.adapters = {Platform("telegram"): target}
    runner._authorization_adapter = MagicMock(return_value=target)
    runner.config.get_home_channel.return_value = None
    adapter.gateway_runner = runner

    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)

    with patch.object(adapter, "handle_message", new=AsyncMock()):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/webhooks/cw",
                data=json.dumps(PAYLOAD),
                headers={"Content-Type": "application/json", "X-Request-ID": "d-1"},
            )
            assert resp.status == 202

    stored = adapter._delivery_info["webhook:cw:d-1"]
    assert stored["card"] == "cw_ticket"
    assert stored["payload"]["ticket_id"] == 94822

    await adapter.send("webhook:cw:d-1", "ROUTINE\nKaris cannot see her documents.")
    sent = target.send.await_args.args[1]
    card = json.loads(sent.split("```adaptivecard", 1)[1].rsplit("```", 1)[0])
    action_set = next(b for b in card["body"] if b["type"] == "ActionSet")
    claim = next(a for a in action_set["actions"] if a["type"] == "Action.Execute")
    assert claim["data"]["ticket_id"] == 94822
