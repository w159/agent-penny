"""Tests that the webhook lane is marked autonomous, so Teams caps it.

The 1500-char cap on unsolicited Teams messages was built on the cron lane:
``gateway/delivery.py`` stamps ``AUTONOMOUS_DELIVERY_METADATA_KEY`` when a send
carries a ``job_id``, and the Teams adapter trims anything so marked to a
single post.  Webhook deliveries never pass through that stamping code, so a
6000-char ConnectWise summary still arrived as two or three Teams posts.

Every webhook route is event-driven by construction: an HTTP POST from an
external service, answered with 202 before the run even starts, delivered to a
chat nobody is waiting in.  The module already classifies the whole lane that
way for silence handling (``_is_webhook_silence_response``), so the marker
goes on every cross-platform webhook send rather than a per-route opt-in.

Covers:
- Agent-mediated webhook delivery to Teams lands as one capped post
- ``deliver_only`` webhook delivery to Teams lands as one capped post
- The held-back note counts what was actually dropped
- A short webhook message is delivered byte-identical
- Non-Teams targets still receive the message unchanged
"""

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    AUTONOMOUS_DELIVERY_METADATA_KEY,
    BasePlatformAdapter,
    SendResult,
)
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH

# Importing the Teams test module installs the SDK mock and exposes a loaded
# TeamsAdapter, exactly as tests/gateway/test_teams_autonomous_message_cap.py
# does; re-doing that bootstrap here would just duplicate it.
from tests.gateway.test_teams import TeamsAdapter, _make_config as _teams_config


ROUTE = "tickets"
CAP = 1500
TEAMS_CHAT = "19:d72b9e0d737b4dda960814e674c260b7@thread.v2"

# Shape taken from the real ConnectWise sweeps the user complained about.
_TICKET = (
    "[#{n} - Ticket {n} that somebody filed and nobody picked up]"
    "(https://na.myconnectwise.net/x?service_recid={n}) - Henssler Financial - "
    "*New* - unassigned, no tech has touched it, Medium priority"
)


def _sweep(ticket_count, lead="Several tickets are sitting unassigned."):
    items = [_TICKET.format(n=9000 + i) for i in range(ticket_count)]
    return lead + "\n\n" + "\n\n".join(items)


def _blocks(text):
    return [b for b in re.split(r"\n[ \t]*\n", text) if b.strip()]


class _RecordingChannel:
    """Non-Teams target: records the metadata it was handed."""

    def __init__(self):
        self.calls: list = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"content": content, "metadata": metadata})
        return SendResult(success=True)


class _FakeRunner:
    def __init__(self, adapters):
        self.adapters = adapters
        self._profile_adapters = {}


@pytest.fixture(autouse=True)
def _teams_in_registry():
    """Teams reaches ``send()``'s deliver-type check through the plugin registry.

    In production the Teams plugin registers itself at startup; without that,
    the agent-mediated path rejects ``deliver: teams`` as unknown before any
    delivery happens, and the test would prove nothing about the cap.
    """
    from gateway.platform_registry import PlatformEntry, platform_registry

    already = platform_registry.is_registered("teams")
    if not already:
        platform_registry.register(
            PlatformEntry(
                name="teams",
                label="Teams",
                adapter_factory=lambda config: None,
                check_fn=lambda: True,
            )
        )
    yield
    if not already:
        platform_registry.unregister("teams")


def _make_teams():
    """A real TeamsAdapter with only the SDK client faked out."""
    adapter = TeamsAdapter(_teams_config())
    adapter._app = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id="m1")),
        reply=AsyncMock(return_value=SimpleNamespace(id="m1")),
    )
    return adapter


def _make_webhook(target_platform, target_adapter, *, reply=None, deliver_only=False):
    route = {
        "secret": _INSECURE_NO_AUTH,
        "prompt": reply if deliver_only else "{event} on {ticket_id}",
        "deliver": target_platform.value,
        "deliver_extra": {"chat_id": TEAMS_CHAT},
    }
    if deliver_only:
        route["deliver_only"] = True
    config = PlatformConfig(
        enabled=True,
        extra={"host": "127.0.0.1", "port": 0, "routes": {ROUTE: route}},
    )
    adapter = WebhookAdapter(config)
    adapter.gateway_runner = _FakeRunner({target_platform: target_adapter})

    if not deliver_only:
        async def _capture(event):
            await adapter.send(event.source.chat_id, reply)

        adapter.handle_message = _capture
    return adapter


async def _post(adapter, body, *, tag="d0"):
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            f"/webhooks/{ROUTE}",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Delivery": tag,
            },
        )
        status = (await resp.json()).get("status")
    await asyncio.sleep(0.05)
    return status


def _teams_posts(teams):
    return [call.args[1] for call in teams._app.send.await_args_list]


# ---------------------------------------------------------------------------
# The cap, end to end through the webhook lane
# ---------------------------------------------------------------------------

class TestWebhookLaneIsCapped:

    @pytest.mark.asyncio
    async def test_agent_lane_oversized_message_becomes_one_capped_post(self):
        source = _sweep(30)
        assert len(BasePlatformAdapter.truncate_message(source)) > 1, (
            "fixture must be long enough to chunk today"
        )
        teams = _make_teams()
        adapter = _make_webhook(Platform("teams"), teams, reply=source)

        assert await _post(adapter, {"event": "new", "ticket_id": 101}) == "accepted"

        posts = _teams_posts(teams)
        assert len(posts) == 1
        assert len(posts[0]) <= CAP

    @pytest.mark.asyncio
    async def test_deliver_only_lane_oversized_message_becomes_one_capped_post(self):
        source = _sweep(30)
        teams = _make_teams()
        adapter = _make_webhook(
            Platform("teams"), teams, reply=source, deliver_only=True
        )

        assert await _post(adapter, {"event": "new", "ticket_id": 101}) == "delivered"

        posts = _teams_posts(teams)
        assert len(posts) == 1
        assert len(posts[0]) <= CAP

    @pytest.mark.asyncio
    async def test_held_back_note_counts_what_was_dropped(self):
        total = 30
        teams = _make_teams()
        adapter = _make_webhook(Platform("teams"), teams, reply=_sweep(total))

        await _post(adapter, {"event": "new", "ticket_id": 101})

        post = _teams_posts(teams)[0]
        shown = len([b for b in _blocks(post) if b.startswith("[#")])
        assert _blocks(post)[-1] == f"+{total - shown} more tickets not shown"

    @pytest.mark.asyncio
    async def test_short_message_is_delivered_unchanged(self):
        short = _sweep(2)
        assert len(short) <= CAP
        teams = _make_teams()
        adapter = _make_webhook(Platform("teams"), teams, reply=short)

        await _post(adapter, {"event": "new", "ticket_id": 101})

        assert _teams_posts(teams) == [short]

    @pytest.mark.asyncio
    async def test_marker_reaches_a_non_teams_target_without_altering_content(self):
        """Adapters that do not read the marker must behave exactly as before."""
        source = _sweep(30)
        channel = _RecordingChannel()
        adapter = _make_webhook(Platform("slack"), channel, reply=source)

        await _post(adapter, {"event": "new", "ticket_id": 101})

        assert len(channel.calls) == 1
        assert channel.calls[0]["content"] == source
        assert channel.calls[0]["metadata"][AUTONOMOUS_DELIVERY_METADATA_KEY] is True

    @pytest.mark.asyncio
    async def test_thread_id_still_travels_with_the_marker(self):
        """The marker is additive: Telegram forum topics must keep working."""
        channel = _RecordingChannel()
        adapter = _make_webhook(Platform("slack"), channel, reply="hi")
        adapter._routes[ROUTE]["deliver_extra"]["thread_id"] = "77"

        await _post(adapter, {"event": "new", "ticket_id": 101})

        metadata = channel.calls[0]["metadata"]
        assert metadata["thread_id"] == "77"
        assert metadata[AUTONOMOUS_DELIVERY_METADATA_KEY] is True
