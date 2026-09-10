"""Tests for the Teams cap on unsolicited (autonomous) messages.

An hourly cron sweep used to arrive as up to nine consecutive Teams posts,
because ``truncate_message()`` chunks rather than cuts.  The cap turns an
unsolicited message into exactly one post that ends by saying what it held
back.  A reply to a human is deliberately left alone: someone who asks for the
whole board should get the whole board.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import (
    AUTONOMOUS_DELIVERY_METADATA_KEY,
    BasePlatformAdapter,
    trim_to_item_boundary,
)
from plugins.platforms.teams.ticket_card import MultiTicketAutonomousError

# Importing the existing Teams test module installs the SDK mock and exposes a
# loaded TeamsAdapter; re-doing that bootstrap here would just duplicate it.
from tests.gateway.test_teams import TeamsAdapter, _make_config


CAP = 1500

# Shape taken from real delivered sweeps (state.db messages.id=6945 and
# cron/output/7ba4aa6a7262/*.md): blank-line-separated blocks, ticket blocks
# opening with a "[#<number> - ..." markdown link.
_TICKET = (
    "[#{n} - Ticket {n} that somebody filed and nobody picked up]"
    "(https://na.myconnectwise.net/x?service_recid={n}) - Henssler Financial - "
    "*New* - unassigned, no tech has touched it, Medium priority"
)


def _sweep(ticket_count, lead="Several tickets are sitting unassigned."):
    """Build an oversized sweep with a known number of ticket items."""
    items = [_TICKET.format(n=9000 + i) for i in range(ticket_count)]
    return lead + "\n\n" + "\n\n".join(items)


def _blocks(text):
    return [b for b in re.split(r"\n[ \t]*\n", text) if b.strip()]


def _make_adapter(**extra):
    adapter = TeamsAdapter(_make_config(**extra))
    adapter._app = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id="m1")),
        reply=AsyncMock(return_value=SimpleNamespace(id="m1")),
    )
    return adapter


# ---------------------------------------------------------------------------
# The trimmer itself
# ---------------------------------------------------------------------------

class TestTrimToItemBoundary:
    def test_content_under_cap_is_returned_byte_identical(self):
        short = _sweep(2)
        assert len(short) <= CAP
        out = trim_to_item_boundary(short, CAP)
        assert out is short
        assert out.encode("utf-8") == short.encode("utf-8")
        assert "not shown" not in out

    def test_oversized_content_fits_the_cap(self):
        out = trim_to_item_boundary(_sweep(20), CAP)
        assert len(out) <= CAP

    def test_trim_lands_on_a_whole_item(self):
        source = _sweep(20)
        out = trim_to_item_boundary(source, CAP)
        retained = _blocks(out)[:-1]  # last block is the held-back note
        source_blocks = _blocks(source)
        assert retained, "expected at least one retained item"
        for block in retained:
            assert block in source_blocks, "item was cut mid-item"

    def test_held_back_count_matches_what_was_dropped(self):
        total = 20
        out = trim_to_item_boundary(_sweep(total), CAP)
        shown = len([b for b in _blocks(out) if b.startswith("[#")])
        note = _blocks(out)[-1]
        assert note == f"+{total - shown} more tickets not shown"

    def test_singular_note_when_one_ticket_held_back(self):
        # Cap chosen so exactly one of two tickets survives.
        source = _sweep(2, lead="Two tickets.")
        cap = len("Two tickets.") + len("\n\n") + len(_TICKET.format(n=9000)) + 40
        out = trim_to_item_boundary(source, cap)
        assert out.endswith("+1 more ticket not shown")

    def test_note_makes_no_claim_when_no_tickets_were_dropped(self):
        # Dropped tail is prose only, so the note must not invent a count.
        source = "Lead line.\n\n" + ("More prose that runs on. " * 200)
        out = trim_to_item_boundary(source, CAP)
        assert out.endswith("+more not shown")
        assert "ticket" not in _blocks(out)[-1]

    @pytest.mark.parametrize("value", ["", "   \n\n  "])
    def test_empty_content_is_untouched(self, value):
        assert trim_to_item_boundary(value, CAP) is value

    def test_zero_cap_disables_trimming(self):
        source = _sweep(20)
        assert trim_to_item_boundary(source, 0) is source

    def test_single_item_larger_than_cap_cuts_on_a_word_boundary(self):
        source = "word " * 900  # one block, no item boundary before the cap
        out = trim_to_item_boundary(source, CAP)
        assert len(out) <= CAP
        assert out.endswith("+more not shown")
        head = out.split("...")[0]
        assert head.endswith("word"), "cut landed mid-word"

    def test_content_with_no_boundary_at_all_still_fits(self):
        out = trim_to_item_boundary("x" * 5000, CAP)
        assert len(out) <= CAP
        assert out.endswith("+more not shown")

    def test_cap_too_small_for_a_note_still_returns_one_capped_message(self):
        out = trim_to_item_boundary(_sweep(20), 5)
        assert len(out) <= 5

    def test_trailing_rollup_block_is_preserved_over_middle_tickets(self):
        # Real shape (2026-09-04 Madison Todd incident): intro + N ticket
        # blocks + a reconciling roll-up sentence that is NOT itself a
        # ticket. When the whole thing does not fit, the roll-up must
        # survive -- dropping middle tickets, not swapping the model's own
        # summary for the generic note -- because it carries real,
        # specific information ("4 auto-routed, 6 on hold, 3 in progress")
        # a robotic "+N more" note cannot.
        rollup = "Four auto-routed and closed. Rest of the board is quietly fine."
        source = _sweep(6) + "\n\n" + rollup
        cap = len(_sweep(2)) + len("\n\n") + len(rollup) + 5
        out = trim_to_item_boundary(source, cap)
        assert len(out) <= cap
        assert out.endswith(rollup), "the model's own roll-up must survive the trim"
        assert "not shown" in out, "dropped middle tickets must still be acknowledged"

    def test_trailing_rollup_falls_back_to_head_only_trim_when_it_cannot_fit(self):
        # Degenerate case: even intro + roll-up alone exceeds the cap, so
        # there is nothing sane to preserve -- fall back to the original
        # head-only behavior rather than returning something broken.
        rollup = "Roll-up. " * 200
        source = _sweep(6) + "\n\n" + rollup
        cap = 40
        out = trim_to_item_boundary(source, cap)
        assert len(out) <= cap
        assert not out.endswith(rollup)


# ---------------------------------------------------------------------------
# The Teams send path
# ---------------------------------------------------------------------------

class TestTeamsAutonomousCap:
    @pytest.mark.asyncio
    async def test_autonomous_multi_ticket_sweep_is_split_not_rejected(self):
        # Fixed 2026-09-04: a real triage-nag run named 3 tickets in one
        # autonomous send (session cron_triage-nag-001_20260904_133005) and
        # got hard-rejected outright, discarding three good, already-clean
        # per-ticket paragraphs. Cleanly splittable content (one ticket per
        # blank-line-separated block, exactly the fact-block shape every
        # real emitter produces) is now split and delivered as one message
        # per ticket instead of rejected. Capped at 3 tickets here (not 30)
        # to stay under outbound_dedup's own 3-per-10-minutes rate limit,
        # which is a separate, still-active anti-flood guard -- see
        # test_split_beyond_the_rate_limit_still_suppresses_the_excess below
        # for what happens past that limit.
        adapter = _make_adapter()
        source = _sweep(3)

        result = await adapter.send(
            "chat", source, metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
        )

        assert result.success
        assert adapter._app.send.await_count == 3
        posted_texts = [c.args[1] for c in adapter._app.send.await_args_list]
        assert sum(t.count("service_recid=") for t in posted_texts) == 3, (
            "every ticket must appear in exactly one of the split messages"
        )
        assert not any("not shown" in t for t in posted_texts), (
            "a cleanly split send has nothing held back to report"
        )

    @pytest.mark.asyncio
    async def test_split_beyond_the_rate_limit_still_suppresses_the_excess(self):
        # The per-ticket split must not become a flood loophole: the
        # existing outbound_dedup rate limit (3 autonomous messages per
        # chat per 10 minutes) still applies to each split part
        # individually, so a message naming more tickets than the rate
        # limit allows still only gets the first few out.
        adapter = _make_adapter()
        source = _sweep(5)

        result = await adapter.send(
            "chat", source, metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
        )

        assert result.success  # suppression is not a send failure
        assert adapter._app.send.await_count == 3, (
            "rate limit must cap actual sends even though 5 tickets were split"
        )

    @pytest.mark.asyncio
    async def test_autonomous_single_ticket_over_cap_is_still_trimmed(self):
        # Single-ticket autonomous content still goes through
        # _cap_autonomous_message's trimming — only the multi-ticket case
        # was retired.
        adapter = _make_adapter()
        source = "One ticket needs attention.\n\n" + _TICKET.format(n=9000) + (
            " Extra detail." * 200
        )
        assert len(source) > CAP

        result = await adapter.send(
            "chat", source, metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
        )

        assert result.success
        assert adapter._app.send.await_count == 1
        posted = adapter._app.send.await_args[0][1]
        assert len(posted) <= CAP
        assert posted.endswith("not shown")

    @pytest.mark.asyncio
    async def test_interactive_send_is_capped_to_one_post(self):
        """A reply to a human is trimmed to the interactive cap, not chunked."""
        adapter = _make_adapter()
        source = _sweep(30)

        result = await adapter.send("chat", source, metadata={"thread_id": "42"})

        assert result.success
        assert adapter._app.send.await_count == 1
        posted = adapter._app.send.await_args[0][1]
        assert len(posted) <= TeamsAdapter.INTERACTIVE_MESSAGE_CHAR_CAP
        assert posted.endswith("not shown")

    @pytest.mark.asyncio
    async def test_send_without_metadata_is_capped(self):
        adapter = _make_adapter()
        result = await adapter.send("chat", _sweep(30))
        assert result.success
        assert adapter._app.send.await_count == 1
        posted = adapter._app.send.await_args[0][1]
        assert len(posted) <= TeamsAdapter.INTERACTIVE_MESSAGE_CHAR_CAP

    @pytest.mark.asyncio
    async def test_autonomous_multi_ticket_message_under_cap_is_split(self):
        # Being under the char cap doesn't change anything: the ticket-count
        # guard (and now the split) is independent of AUTONOMOUS_MESSAGE_CHAR_CAP.
        adapter = _make_adapter()
        source = _sweep(2)
        assert len(source) <= CAP

        result = await adapter.send(
            "chat", source, metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
        )

        assert result.success
        assert adapter._app.send.await_count == 2
        posted_texts = [c.args[1] for c in adapter._app.send.await_args_list]
        assert sum(t.count("service_recid=") for t in posted_texts) == 2

    @pytest.mark.asyncio
    async def test_autonomous_single_ticket_message_under_cap_is_delivered_verbatim(self):
        adapter = _make_adapter()
        source = _sweep(1)
        assert len(source) <= CAP

        await adapter.send(
            "chat", source, metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
        )

        assert adapter._app.send.await_count == 1
        assert adapter._app.send.await_args[0][1] == source

    def test_cap_is_configurable_from_platform_extra(self):
        # Not hardcoded to a literal: the class default is a reviewed,
        # intentionally-tuned value (raised 2026-09-04 from 1500 to 2800 to
        # fit a SOUL.md-compliant 6-ticket sweep message), not a constant
        # this test should freeze in place.
        assert _make_adapter()._autonomous_cap == TeamsAdapter.AUTONOMOUS_MESSAGE_CHAR_CAP
        assert _make_adapter(autonomous_message_cap=800)._autonomous_cap == 800
        assert _make_adapter(autonomous_message_cap=0)._autonomous_cap == 0

    def test_invalid_configured_cap_falls_back_to_the_default(self):
        adapter = _make_adapter(autonomous_message_cap="not-a-number")
        assert adapter._autonomous_cap == TeamsAdapter.AUTONOMOUS_MESSAGE_CHAR_CAP

    @pytest.mark.asyncio
    async def test_configured_zero_cap_does_not_bypass_the_multi_ticket_guard(self):
        # cap=0 only disables char-count trimming; the ticket-count guard
        # (and the split it now triggers) is independent of it.
        adapter = _make_adapter(autonomous_message_cap=0)
        result = await adapter.send(
            "chat", _sweep(2), metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
        )
        assert result.success
        assert adapter._app.send.await_count == 2

    @pytest.mark.asyncio
    async def test_configured_zero_cap_restores_chunking_for_single_ticket(self):
        adapter = _make_adapter(autonomous_message_cap=0)
        source = "One ticket.\n\n" + _TICKET.format(n=9000) + (" Extra detail." * 2000)
        assert len(BasePlatformAdapter.truncate_message(source)) > 1, (
            "fixture must be long enough to chunk today"
        )
        await adapter.send(
            "chat", source, metadata={AUTONOMOUS_DELIVERY_METADATA_KEY: True}
        )
        assert adapter._app.send.await_count > 1


# ---------------------------------------------------------------------------
# Other platforms must be untouched
# ---------------------------------------------------------------------------

class TestOtherPlatformsUnaffected:
    def test_truncate_message_still_chunks_and_preserves_everything(self):
        source = _sweep(30)
        chunks = BasePlatformAdapter.truncate_message(source)
        assert len(chunks) > 1
        joined = "".join(chunks)
        assert joined.count("service_recid=") == 30
        assert "not shown" not in joined

    def test_a_non_teams_adapter_does_not_cap(self):
        """The cap lives on the Teams adapter, not on the shared base class."""
        assert not hasattr(BasePlatformAdapter, "AUTONOMOUS_MESSAGE_CHAR_CAP")
        assert not hasattr(BasePlatformAdapter, "_cap_autonomous_message")
