"""Tests that the delivery router marks scheduler pushes as autonomous.

Only the delivery lane knows whether a human is waiting on a message.  Cron
output arrives with a ``job_id`` (cron/scheduler.py), so that is the signal the
router turns into ``AUTONOMOUS_DELIVERY_METADATA_KEY`` for adapters that cap
unsolicited chatter.  Anything without a job_id keeps today's behaviour.
"""

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.platforms.base import AUTONOMOUS_DELIVERY_METADATA_KEY


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return {"success": True}


# Teams is a plugin platform, so the enum member is created dynamically —
# Platform("teams") resolves it, Platform.TEAMS does not exist at import time.
TEAMS = Platform("teams")
TEAMS_CHAT = "teams:19abcdef"


def _router(adapter):
    return DeliveryRouter(GatewayConfig(), adapters={TEAMS: adapter})


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_FILTER_SILENCE_NARRATION", raising=False)


@pytest.mark.asyncio
async def test_cron_delivery_is_marked_autonomous():
    adapter = RecordingAdapter()
    target = DeliveryTarget.parse(TEAMS_CHAT)

    await _router(adapter)._deliver_to_platform(
        target, "Board sweep result.", metadata={"job_id": "7ba4aa6a7262"}
    )

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["metadata"][AUTONOMOUS_DELIVERY_METADATA_KEY] is True


@pytest.mark.asyncio
async def test_delivery_without_job_id_is_not_marked():
    adapter = RecordingAdapter()
    target = DeliveryTarget.parse(TEAMS_CHAT)

    await _router(adapter)._deliver_to_platform(
        target, "Answering your question.", metadata={"thread_id": "42"}
    )

    assert len(adapter.calls) == 1
    assert AUTONOMOUS_DELIVERY_METADATA_KEY not in (adapter.calls[0]["metadata"] or {})


@pytest.mark.asyncio
async def test_delivery_with_no_metadata_is_not_marked():
    adapter = RecordingAdapter()
    target = DeliveryTarget.parse(TEAMS_CHAT)

    await _router(adapter)._deliver_to_platform(target, "Hello.", metadata=None)

    assert len(adapter.calls) == 1
    assert AUTONOMOUS_DELIVERY_METADATA_KEY not in (adapter.calls[0]["metadata"] or {})


@pytest.mark.asyncio
async def test_marking_does_not_mutate_the_caller_dict():
    adapter = RecordingAdapter()
    target = DeliveryTarget.parse(TEAMS_CHAT)
    caller_metadata = {"job_id": "7ba4aa6a7262"}

    await _router(adapter)._deliver_to_platform(
        target, "Board sweep result.", metadata=caller_metadata
    )

    assert caller_metadata == {"job_id": "7ba4aa6a7262"}
