"""Tests for the webhook adapter's per-subject suppression (route ``dedupe``).

A webhook provider can send many genuinely distinct events about one thing —
a ConnectWise ticket emits a callback per field edit — and each one becomes an
independent agent turn.  The ``dedupe`` route block collapses them: one
subject speaks once per window, with a narrow escalation break-through.

Covers:
- Distinct events on one subject collapse to a single agent turn
- Suppression is per subject, not per route
- State survives a restart (fresh adapter + fresh DB connection)
- Escalation breaks through once, and only for an allowlisted value
- A subject key that does not resolve fails open (delivers)
- The existing delivery_id retry dedupe still applies alongside
- The window is consumed at DELIVERY time, so silence costs nothing
"""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import (
    WebhookAdapter,
    _INSECURE_NO_AUTH,
    _suppression_session_key,
)
from hermes_state import SessionDB


ROUTE = "tickets"

# Anything that is not a silence marker reaches the destination.
SPEAKS = "Ticket needs a human."
SILENT = "[SILENT]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeChannel:
    """Stand-in for a connected chat adapter; records what actually landed.

    Registered as ``slack`` rather than ``teams`` only because Teams arrives
    through the plugin registry, which is not loaded under test; both take
    the same ``_deliver_cross_platform`` path.
    """

    def __init__(self, ok=True):
        self.ok = ok
        self.sent: list = []

    async def send(self, chat_id, content, metadata=None):
        if not self.ok:
            return SendResult(success=False, error="teams rejected it")
        self.sent.append(content)
        return SendResult(success=True)


class _FakeRunner(GatewayAuthorizationMixin):
    def __init__(self, channel):
        self.adapters = {Platform("slack"): channel}
        self._profile_adapters = {}


def _make_adapter(
    db_path,
    dedupe=None,
    *,
    deliver="log",
    replies=None,
    channel=None,
    reservation_seconds=None,
) -> tuple[WebhookAdapter, list]:
    """Adapter whose fake agent answers each turn from *replies*.

    The default reply is real content: the window is only consumed when a
    message actually goes out, so a handler that says nothing would leave
    every window open and make the suppression tests vacuous.
    """
    route = {
        "secret": _INSECURE_NO_AUTH,
        "prompt": "{event} on {ticket_id}",
        "deliver": deliver,
    }
    if deliver != "log":
        route["deliver_extra"] = {"chat_id": "19:test@thread.v2"}
    if dedupe is not None:
        route["dedupe"] = dedupe
    extra = {"host": "127.0.0.1", "port": 0, "routes": {ROUTE: route}}
    if reservation_seconds is not None:
        extra["inflight_reservation_seconds"] = reservation_seconds
    config = PlatformConfig(
        enabled=True,
        extra=extra,
    )
    adapter = WebhookAdapter(config)
    # Point the suppression index at a throwaway DB instead of ~/.hermes/state.db.
    adapter._suppression_db = SessionDB(db_path=db_path)
    if reservation_seconds is not None:
        adapter._inflight_reservation_seconds = reservation_seconds
    if channel is not None:
        adapter.gateway_runner = _FakeRunner(channel)
    turns: list = []
    pending = list(replies or [])

    async def _capture(event):
        turns.append(event.message_id)
        answer = pending.pop(0) if pending else SPEAKS
        await adapter.send(event.source.chat_id, answer)

    adapter.handle_message = _capture
    return adapter, turns


def _app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _post(adapter, events, *, tag="d") -> list:
    """POST each event under its own delivery id; return the status strings."""
    statuses = []
    async with TestClient(TestServer(_app(adapter))) as cli:
        for i, body in enumerate(events):
            resp = await cli.post(
                f"/webhooks/{ROUTE}",
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": f"{tag}-{i}",
                },
            )
            statuses.append((await resp.json()).get("status"))
    await asyncio.sleep(0.05)
    return statuses


def _event(ticket_id, event="updated"):
    return {"ticket_id": ticket_id, "event": event}


DEDUPE = {
    "key": "{ticket_id}",
    "window_seconds": 3600,
    "breakthrough_key": "{event}",
    "breakthrough_values": ["reopened"],
}


# ===================================================================
# Core behaviour
# ===================================================================

class TestSubjectSuppression:

    @pytest.mark.asyncio
    async def test_repeat_events_on_one_subject_speak_once(self, tmp_path):
        adapter, turns = _make_adapter(tmp_path / "state.db", DEDUPE)
        statuses = await _post(adapter, [_event(101) for _ in range(5)])

        assert statuses == ["accepted"] + ["suppressed"] * 4
        assert len(turns) == 1

    @pytest.mark.asyncio
    async def test_suppression_is_per_subject(self, tmp_path):
        adapter, turns = _make_adapter(tmp_path / "state.db", DEDUPE)
        statuses = await _post(
            adapter, [_event(101), _event(202), _event(101), _event(202)]
        )

        assert statuses == ["accepted", "accepted", "suppressed", "suppressed"]
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_route_without_dedupe_is_unchanged(self, tmp_path):
        adapter, turns = _make_adapter(tmp_path / "state.db", None)
        statuses = await _post(adapter, [_event(101) for _ in range(3)])

        assert statuses == ["accepted"] * 3
        assert len(turns) == 3

    @pytest.mark.asyncio
    async def test_state_survives_restart(self, tmp_path):
        db_path = tmp_path / "state.db"
        first, _ = _make_adapter(db_path, DEDUPE)
        assert await _post(first, [_event(101)], tag="before") == ["accepted"]

        # New adapter object, new connection — nothing in memory carries over.
        second, turns = _make_adapter(db_path, DEDUPE)
        assert second._seen_deliveries == {}
        statuses = await _post(second, [_event(101)], tag="after")

        assert statuses == ["suppressed"]
        assert turns == []

    @pytest.mark.asyncio
    async def test_escalation_breaks_through_once(self, tmp_path):
        adapter, turns = _make_adapter(tmp_path / "state.db", DEDUPE)
        statuses = await _post(
            adapter,
            [
                _event(101, "new_ticket"),
                _event(101, "closed"),
                _event(101, "reopened"),
                _event(101, "reopened"),
                _event(101, "closed"),
            ],
        )

        # The reopen speaks; the second reopen is the same escalation again.
        assert statuses == [
            "accepted",
            "suppressed",
            "accepted",
            "suppressed",
            "suppressed",
        ]
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_unlisted_event_change_does_not_break_through(self, tmp_path):
        adapter, turns = _make_adapter(tmp_path / "state.db", DEDUPE)
        statuses = await _post(
            adapter, [_event(101, "new_ticket"), _event(101, "closed")]
        )

        assert statuses == ["accepted", "suppressed"]
        assert len(turns) == 1

    @pytest.mark.asyncio
    async def test_unresolved_subject_key_delivers(self, tmp_path):
        adapter, turns = _make_adapter(tmp_path / "state.db", DEDUPE)
        # No ticket_id in the payload: better to repeat than to collapse every
        # event on the route into one subject.
        statuses = await _post(adapter, [{"event": "updated"}] * 3)

        assert statuses == ["accepted"] * 3
        assert len(turns) == 3

    @pytest.mark.asyncio
    async def test_zero_window_disables_suppression(self, tmp_path):
        dedupe = dict(DEDUPE, window_seconds=0)
        adapter, turns = _make_adapter(tmp_path / "state.db", dedupe)
        statuses = await _post(adapter, [_event(101) for _ in range(3)])

        assert statuses == ["accepted"] * 3
        assert len(turns) == 3

    @pytest.mark.asyncio
    async def test_retry_dedupe_still_applies(self, tmp_path):
        """A provider retry is still a duplicate, not a suppression."""
        adapter, turns = _make_adapter(tmp_path / "state.db", DEDUPE)
        statuses = []
        async with TestClient(TestServer(_app(adapter))) as cli:
            for _ in range(3):
                resp = await cli.post(
                    f"/webhooks/{ROUTE}",
                    data=json.dumps(_event(101)).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Delivery": "same-id",
                    },
                )
                statuses.append((await resp.json()).get("status"))
        await asyncio.sleep(0.05)

        assert statuses == ["accepted", "duplicate", "duplicate"]
        assert len(turns) == 1

    @pytest.mark.asyncio
    async def test_suppression_is_logged_with_subject_and_remaining(
        self, tmp_path, caplog
    ):
        adapter, _ = _make_adapter(tmp_path / "state.db", DEDUPE)
        with caplog.at_level("INFO", logger="gateway.platforms.webhook"):
            await _post(adapter, [_event(101), _event(101)])

        line = next(
            r.getMessage() for r in caplog.records if "suppressed route=" in r.getMessage()
        )
        assert "subject=101" in line
        assert "window=3600s" in line
        assert "quiet_for_another=" in line


# ===================================================================
# Delivery-time accounting
# ===================================================================

class TestWindowIsConsumedAtDeliveryTime:
    """Most events on this lane end in silence, by design.

    The route prompt tells the agent that most events should end with
    ``[SILENT]``.  If a silent turn consumed the subject's window, the next
    genuinely urgent event on that ticket would be suppressed before the
    agent ever saw it — a missed escalation, which is worse than the
    repetition the window exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_silence_does_not_consume_the_window(self, tmp_path):
        adapter, turns = _make_adapter(
            tmp_path / "state.db", DEDUPE, replies=[SILENT, SPEAKS]
        )
        statuses = await _post(adapter, [_event(101), _event(101)])

        assert statuses == ["accepted", "accepted"]
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_delivery_consumes_the_window(self, tmp_path):
        channel = _FakeChannel()
        adapter, turns = _make_adapter(
            tmp_path / "state.db", DEDUPE, deliver="slack", channel=channel
        )
        statuses = await _post(adapter, [_event(101), _event(101)])

        assert statuses == ["accepted", "suppressed"]
        assert len(turns) == 1
        assert channel.sent == [SPEAKS]

    @pytest.mark.asyncio
    async def test_send_the_target_rejected_does_not_consume_the_window(
        self, tmp_path
    ):
        """SendResult.success is the signal, not "no exception was raised"."""
        channel = _FakeChannel(ok=False)
        adapter, turns = _make_adapter(
            tmp_path / "state.db", DEDUPE, deliver="slack", channel=channel
        )
        statuses = await _post(adapter, [_event(101), _event(101)])

        assert statuses == ["accepted", "accepted"]
        assert len(turns) == 2
        assert channel.sent == []

    @pytest.mark.asyncio
    async def test_silence_then_escalation_still_reaches_the_agent(self, tmp_path):
        """The regression this whole change exists for.

        A quiet new-ticket event must not spend the window that a later
        reopen needs.
        """
        adapter, turns = _make_adapter(
            tmp_path / "state.db", DEDUPE, replies=[SILENT, SPEAKS]
        )
        statuses = await _post(
            adapter, [_event(101, "new_ticket"), _event(101, "reopened")]
        )

        assert statuses == ["accepted", "accepted"]
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_one_breakthrough_per_distinct_escalation_value(self, tmp_path):
        channel = _FakeChannel()
        adapter, turns = _make_adapter(
            tmp_path / "state.db", DEDUPE, deliver="slack", channel=channel
        )
        statuses = await _post(
            adapter,
            [
                _event(101, "new_ticket"),
                _event(101, "reopened"),
                _event(101, "reopened"),
                _event(101, "closed"),
            ],
        )

        # One message for the ticket, one more for the reopen, and nothing
        # for the repeat reopen.
        assert statuses == ["accepted", "accepted", "suppressed", "suppressed"]
        assert len(channel.sent) == 2

    @pytest.mark.asyncio
    async def test_three_reopens_still_speak_once(self, tmp_path):
        channel = _FakeChannel()
        adapter, _ = _make_adapter(
            tmp_path / "state.db", DEDUPE, deliver="slack", channel=channel
        )
        statuses = await _post(
            adapter,
            [_event(101, "new_ticket")] + [_event(101, "reopened")] * 3,
        )

        assert statuses == ["accepted", "accepted", "suppressed", "suppressed"]
        assert len(channel.sent) == 2

    @pytest.mark.asyncio
    async def test_a_second_escalation_value_does_not_reopen_the_first(
        self, tmp_path
    ):
        """One message per DISTINCT escalation, not per change of escalation.

        Remembering only the LAST value spoken for meant a route with two
        allowed escalations let the first one break through again as soon as
        the second had been reported in between.
        """
        channel = _FakeChannel()
        dedupe = dict(DEDUPE, breakthrough_values=["reopened", "escalated"])
        adapter, _ = _make_adapter(
            tmp_path / "state.db", dedupe, deliver="slack", channel=channel
        )
        statuses = await _post(
            adapter,
            [
                _event(101, "new_ticket"),
                _event(101, "reopened"),
                _event(101, "escalated"),
                _event(101, "reopened"),
            ],
        )

        # The ticket, the reopen and the escalation each speak once; the
        # repeat reopen is an escalation already reported.
        assert statuses == ["accepted", "accepted", "accepted", "suppressed"]
        assert len(channel.sent) == 3

    @pytest.mark.asyncio
    async def test_interim_message_consumes_the_window_only_once(self, tmp_path):
        """send() also carries interim status notices for the same run.

        The first message that lands starts the window; the final response
        must not push it forward again.
        """
        channel = _FakeChannel()
        adapter, _ = _make_adapter(
            tmp_path / "state.db", DEDUPE, deliver="slack", channel=channel
        )

        async def _two_messages(event):
            await adapter.send(event.source.chat_id, "switching model...")
            await adapter.send(event.source.chat_id, SPEAKS)

        adapter.handle_message = _two_messages
        await _post(adapter, [_event(101)])

        rows = adapter._suppression_db.load_gateway_routing_entries(
            scope="webhook-subject-suppression"
        )
        assert len(rows) == 1
        first, second = channel.sent
        assert (first, second) == ("switching model...", SPEAKS)


# ===================================================================
# Concurrent events on one subject
# ===================================================================

class TestInFlightReservation:
    """Two callbacks about one ticket, seconds apart, must not both speak.

    The window only opens once a message lands, which is what makes silence
    free — but it also left the subject unguarded for the whole length of a
    turn.  On the real ConnectWise log, 31 of ~107 consecutive same-ticket
    event pairs are under 30 seconds apart, and a webhook turn on this gateway
    runs 11s at the median, so both events routinely passed the check and both
    delivered.  A short reservation taken at check time closes that hole.
    """

    @staticmethod
    async def _post_concurrently(adapter, events, *, settle=0.6):
        """Fire every event before any turn finishes, then wait them all out."""
        statuses = []
        async with TestClient(TestServer(_app(adapter))) as cli:
            for i, body in enumerate(events):
                resp = await cli.post(
                    f"/webhooks/{ROUTE}",
                    data=json.dumps(body).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Delivery": f"c-{i}",
                    },
                )
                statuses.append((await resp.json()).get("status"))
            await asyncio.sleep(settle)
        return statuses

    @staticmethod
    def _slow_agent(adapter, answer=SPEAKS, delay=0.25):
        """Replace the fake agent with one whose turn is still running."""
        turns: list = []

        async def _slow(event):
            turns.append(event.message_id)
            await asyncio.sleep(delay)
            await adapter.send(event.source.chat_id, answer)

        adapter.handle_message = _slow
        return turns

    @pytest.mark.asyncio
    async def test_overlapping_events_on_one_subject_deliver_once(self, tmp_path):
        channel = _FakeChannel()
        adapter, _ = _make_adapter(
            tmp_path / "state.db", DEDUPE, deliver="slack", channel=channel
        )
        turns = self._slow_agent(adapter)

        statuses = await self._post_concurrently(
            adapter, [_event(101), _event(101)]
        )

        assert statuses == ["accepted", "suppressed"]
        assert len(turns) == 1
        assert channel.sent == [SPEAKS]

    @pytest.mark.asyncio
    async def test_overlapping_events_on_different_subjects_both_speak(
        self, tmp_path
    ):
        """The hold is per subject, exactly like the window it precedes."""
        channel = _FakeChannel()
        adapter, _ = _make_adapter(
            tmp_path / "state.db", DEDUPE, deliver="slack", channel=channel
        )
        turns = self._slow_agent(adapter)

        statuses = await self._post_concurrently(
            adapter, [_event(101), _event(202)]
        )

        assert statuses == ["accepted", "accepted"]
        assert len(turns) == 2
        assert len(channel.sent) == 2

    @pytest.mark.asyncio
    async def test_silent_turn_frees_the_subject_for_the_next_event(
        self, tmp_path
    ):
        """The reservation must not become a second way for silence to cost.

        The quiet turn ends, releases its hold, and the escalation that
        follows still reaches the agent and still lands.
        """
        channel = _FakeChannel()
        adapter, turns = _make_adapter(
            tmp_path / "state.db",
            DEDUPE,
            deliver="slack",
            channel=channel,
            replies=[SILENT, SPEAKS],
        )
        statuses = await _post(
            adapter, [_event(101, "new_ticket"), _event(101, "reopened")]
        )

        assert statuses == ["accepted", "accepted"]
        assert len(turns) == 2
        assert channel.sent == [SPEAKS]

    @pytest.mark.asyncio
    async def test_rejected_send_frees_the_subject(self, tmp_path):
        """A target that refused the message leaves the channel quiet."""
        channel = _FakeChannel(ok=False)
        adapter, turns = _make_adapter(
            tmp_path / "state.db", DEDUPE, deliver="slack", channel=channel
        )
        statuses = await _post(adapter, [_event(101), _event(101)])

        assert statuses == ["accepted", "accepted"]
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_reservation_expires_when_the_turn_never_finishes(
        self, tmp_path
    ):
        """A process that dies mid-turn must not silence its ticket forever.

        Nothing renews a reservation, so the ticket speaks again once the hold
        runs out on its own — no cleanup code has to survive the crash.
        """
        channel = _FakeChannel()
        adapter, _ = _make_adapter(
            tmp_path / "state.db",
            DEDUPE,
            deliver="slack",
            channel=channel,
            reservation_seconds=1,
        )
        # A turn that never returns and never sends: the same state the index
        # is left in when the gateway is killed while a run is in flight.
        started = asyncio.Event()

        async def _never_finishes(event):
            started.set()
            await asyncio.Event().wait()

        adapter.handle_message = _never_finishes

        async with TestClient(TestServer(_app(adapter))) as cli:
            async def _fire(tag):
                resp = await cli.post(
                    f"/webhooks/{ROUTE}",
                    data=json.dumps(_event(101)).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Delivery": tag,
                    },
                )
                return (await resp.json()).get("status")

            assert await _fire("dead-turn") == "accepted"
            await asyncio.wait_for(started.wait(), 2)
            # Held while the reservation is live...
            assert await _fire("during-hold") == "suppressed"
            # ...and free again once it lapses.
            await asyncio.sleep(1.2)
            adapter.handle_message = lambda e: adapter.send(
                e.source.chat_id, SPEAKS
            )
            assert await _fire("after-expiry") == "accepted"
            await asyncio.sleep(0.1)

        assert channel.sent == [SPEAKS]

    @pytest.mark.asyncio
    async def test_run_that_never_sends_releases_at_end_of_run(self, tmp_path):
        """A turn can end without calling send() at all — an error, a cancel.

        ``on_processing_complete`` is the one hook the base adapter fires on
        every one of those paths, so it is where the hold is finally dropped.
        The fake agent used elsewhere in this file replaces ``handle_message``
        wholesale and so never reaches it; call it the way the base adapter
        would.
        """
        adapter, _ = _make_adapter(tmp_path / "state.db", DEDUPE)
        captured: list = []

        async def _says_nothing(event):
            captured.append(event)

        adapter.handle_message = _says_nothing
        assert await _post(adapter, [_event(101)], tag="quiet") == ["accepted"]

        rows = adapter._suppression_db.load_gateway_routing_entries(
            scope="webhook-subject-suppression"
        )
        assert len(rows) == 1, "the turn should still be holding its subject"

        await adapter.on_processing_complete(captured[0], None)

        rows = adapter._suppression_db.load_gateway_routing_entries(
            scope="webhook-subject-suppression"
        )
        assert rows == {}, "a turn that said nothing leaves no trace"
        assert await _post(adapter, [_event(101)], tag="after") == ["accepted"]

    @pytest.mark.asyncio
    async def test_expired_reservation_row_is_not_pruned_while_live(
        self, tmp_path
    ):
        """Pruning keys off delivered_at, which a held row does not have yet.

        Without the guard, committing subject B would sweep away subject A's
        live reservation and let A speak twice.
        """
        channel = _FakeChannel()
        adapter, _ = _make_adapter(
            tmp_path / "state.db", DEDUPE, deliver="slack", channel=channel
        )
        turns = self._slow_agent(adapter, delay=0.35)

        statuses = await self._post_concurrently(
            adapter,
            # 202 delivers and prunes while 101 is still held.
            [_event(101), _event(202), _event(101)],
            settle=0.9,
        )

        assert statuses == ["accepted", "accepted", "suppressed"]
        assert len(turns) == 2


# ===================================================================
# Break-through state belongs to one window
# ===================================================================

class TestBreakthroughIsScopedToTheWindow:
    """One escalation speaks once per WINDOW, not once per ticket forever.

    The list of escalations already spoken for used to be carried across
    every window the row lived through, so a ticket that reopened, waited
    the whole window out, had any event deliver, and then reopened again was
    dropped in silence.  A reopen is the strongest signal this lane has, so
    swallowing it is worse than the repetition the window exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_reopen_speaks_again_in_the_next_window(self, tmp_path):
        channel = _FakeChannel()
        # One-second window so the rollover is a real elapsed window, not a
        # hand-edited row.
        dedupe = dict(DEDUPE, window_seconds=1)
        adapter, _ = _make_adapter(
            tmp_path / "state.db", dedupe, deliver="slack", channel=channel
        )

        assert await _post(adapter, [_event(101, "reopened")], tag="w1") == [
            "accepted"
        ]
        await asyncio.sleep(1.1)
        # Any delivered event starts the next window and used to carry the
        # first window's escalations into it.
        assert await _post(adapter, [_event(101, "updated")], tag="w2") == [
            "accepted"
        ]
        statuses = await _post(
            adapter,
            [_event(101, "reopened"), _event(101, "reopened")],
            tag="w2b",
        )

        # The reopen breaks through the new window once, and only once.
        assert statuses == ["accepted", "suppressed"]
        assert len(channel.sent) == 3

    def test_escalations_do_not_accumulate_across_windows(self, tmp_path):
        """The row must not grow one entry per window for the life of a ticket."""
        adapter, _ = _make_adapter(tmp_path / "state.db", DEDUPE)
        db = adapter._suppression_db
        session_key = _suppression_session_key(ROUTE, "101")
        pending = {
            "session_key": session_key,
            "subject": "101",
            "route": ROUTE,
            "window": 10,
            "token": "t",
        }
        # 40 deliveries, each a full window after the last.
        for i in range(40):
            adapter._record_subject_delivery(
                db, dict(pending, breakthrough=f"esc-{i}"), 1000.0 + i * 100
            )

        row = json.loads(
            db.load_gateway_routing_entries(scope="webhook-subject-suppression")[
                session_key
            ]
        )
        assert row["breakthroughs"] == ["esc-39"]


# ===================================================================
# Key construction
# ===================================================================

class TestSessionKeyEscaping:

    def test_colon_in_route_or_subject_cannot_collide(self):
        """Bare "route:subject" made these two the same row."""
        assert _suppression_session_key("a:b", "c") != _suppression_session_key(
            "a", "b:c"
        )

    def test_key_is_stable_for_ordinary_names(self):
        assert _suppression_session_key("tickets", "94265") == "tickets:94265"
