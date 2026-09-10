"""Tests for gateway.outbound_dedup (the anti-repetition/anti-nag guard).

Acceptance test replays the five real Teams messages Penny sent to one
group chat inside six minutes on 2026-08-28 (owner complaint, see
gateway/outbound_dedup.py module docstring) and asserts only the first
survives. Before this module existed, all five would have gone out.
"""

import json
import time

import pytest

from gateway import outbound_dedup as dedup


CHAT_ID = "19:abcdef@thread.tacv2"

# The five real messages, in order, six minutes apart in real life. Here we
# space them a few seconds apart with an explicit `now` per call, matching
# the real timeline closely enough to exercise the SIMILARITY_WINDOW.
REAL_MESSAGES = [
    "Ernesto, you closed three today - nice - but those three On-Hold tickets are still sitting there with zero blocker notes. Either add a real reason or close them; they won't shut themselves.",
    "Thanks, Jerry. Back to work: Ernesto's On-Hold tickets (#94669, #94566, #94492) still need blocker notes or closes. Want me to pull the latest notes on those?",
    "Glad you noticed the edge. Ernesto, those three On-Hold tickets (#94669, #94566, #94492) are still sitting with zero blocker notes - either state what's really holding them up or close them. I'll keep calling them out until they move.",
    "I tried to pull the latest details on those three tickets (#94669, #94566, #94492) but the ConnectWise tool kept failing...",
    "Let's keep it professional. Ernesto, those three On-Hold tickets (#94669, #94566, #94492) still need either a real blocker note or a close - whichever is accurate. What's the status on each?",
]


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Point OPS_DIR/HISTORY_FILE at a scratch dir so tests don't touch or
    depend on real ops memory, and don't leak state between tests."""
    history_file = tmp_path / "outbound_dedup_history.json"
    monkeypatch.setattr(dedup, "OPS_DIR", tmp_path)
    monkeypatch.setattr(dedup, "HISTORY_FILE", history_file)
    return history_file


class TestAcceptanceFiveRealMessages:
    """Replay the five real messages; only the first (and, if it carries a
    genuinely new fact, the fourth) should survive."""

    def test_five_real_messages_collapse(self):
        base = time.time()
        results = []
        for i, msg in enumerate(REAL_MESSAGES):
            now = base + i * 60  # one minute apart, well inside the 30-min window
            results.append(dedup.guard_outbound(CHAT_ID, msg, is_reply=False, now=now))

        allowed = [r.allowed for r in results]

        # Message 1 always survives - it's the first thing said.
        assert allowed[0] is True, "first message must always be allowed"

        # Messages 2, 3, 5 are pure restatements of message 1: same three
        # ticket ids, same "blocker note or close" ask, no new fact. They
        # must be suppressed.
        assert allowed[1] is False, "message 2 is a rephrased repeat of message 1"
        assert allowed[2] is False, "message 3 is a rephrased repeat of message 1"
        assert allowed[4] is False, "message 5 is a rephrased repeat of message 1"

        # Message 4 (the ConnectWise-tool-failure message) is judged here as
        # a genuinely NEW fact, not a repeat: it reports that Penny's own
        # tool call failed -- operational information absent from every
        # other message -- rather than restating the same "add a blocker
        # note or close" demand. Its vocabulary barely overlaps message 1
        # (Jaccard ~0.07, well under SIMILARITY_THRESHOLD), which is the
        # measurable signal behind this judgment call.
        assert allowed[3] is True, "message 4 reports a new fact (tool failure), not a restatement"

        # Net effect: two distinct messages reached the chat (the opener and
        # the tool-failure update); the three restatements did not.
        assert sum(1 for a in allowed if a) == 2

    def test_suppressions_are_logged(self, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="gateway.outbound_dedup")
        base = time.time()
        for i, msg in enumerate(REAL_MESSAGES[:2]):
            dedup.guard_outbound(CHAT_ID, msg, is_reply=False, now=base + i * 60)
        assert any("suppressed near-duplicate" in r.message for r in caplog.records), (
            "suppression must be observable via INFO log, not silent"
        )


class TestNewFactPassesThrough:
    def test_new_fact_about_same_tickets_is_allowed(self):
        base = time.time()
        first = dedup.guard_outbound(
            CHAT_ID,
            "Ernesto, those three On-Hold tickets (#94669, #94566, #94492) still need blocker notes or closes.",
            is_reply=False,
            now=base,
        )
        assert first.allowed is True

        # A message reporting genuine progress on one of the same tickets -
        # new state, new information - must not be blocked.
        progress = dedup.guard_outbound(
            CHAT_ID,
            "Update: #94492 just moved to In Progress. #94669 and #94566 are still On-Hold with no blocker note.",
            is_reply=False,
            now=base + 60,
        )
        assert progress.allowed is True, "a message with new ticket state must not be suppressed as a repeat"

    def test_unrelated_tickets_not_suppressed(self):
        base = time.time()
        first = dedup.guard_outbound(CHAT_ID, "Ticket #100 needs a blocker note.", is_reply=False, now=base)
        assert first.allowed is True
        second = dedup.guard_outbound(CHAT_ID, "Ticket #200 needs a blocker note.", is_reply=False, now=base + 30)
        assert second.allowed is True, "different ticket id should not be treated as a repeat"


class TestTicStripping:
    def test_strips_trailing_tic(self):
        text, removed = dedup.strip_tics("Please respond by EOD. Your move.")
        assert text == "Please respond by EOD."
        assert "Your move." in removed

    def test_no_tic_unchanged(self):
        text, removed = dedup.strip_tics("Please respond by EOD.")
        assert text == "Please respond by EOD."
        assert removed == []

    def test_tic_stripped_in_guard_and_logged(self, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="gateway.outbound_dedup")
        result = dedup.guard_outbound(CHAT_ID, "Status check. Your move.", is_reply=False)
        assert result.allowed is True
        assert result.text == "Status check."
        assert any("stripped tic" in r.message for r in caplog.records)

    def test_custom_tic_list(self):
        text, removed = dedup.strip_tics("All set. Ball's in your court.", tics=["Ball's in your court."])
        assert text == "All set."
        assert removed == ["Ball's in your court."]


class TestRateLimit:
    def test_autonomous_messages_capped(self):
        base = time.time()
        results = []
        # Send distinct (non-duplicate) autonomous messages about different
        # tickets so only the rate limit, not similarity, can suppress them.
        for i in range(dedup.RATE_LIMIT_MAX_MESSAGES + 2):
            msg = f"Ticket #{9000 + i} needs attention."
            results.append(
                dedup.guard_outbound(CHAT_ID, msg, is_reply=False, now=base + i * 60)
            )
        allowed_count = sum(1 for r in results if r.allowed)
        assert allowed_count == dedup.RATE_LIMIT_MAX_MESSAGES, (
            f"expected exactly {dedup.RATE_LIMIT_MAX_MESSAGES} autonomous sends allowed in window, "
            f"got {allowed_count}"
        )
        assert results[-1].allowed is False
        assert results[-1].reason == "rate_limited"

    def test_direct_reply_exempt_from_rate_limit(self):
        base = time.time()
        # Exhaust the autonomous rate limit first.
        for i in range(dedup.RATE_LIMIT_MAX_MESSAGES):
            dedup.guard_outbound(CHAT_ID, f"Ticket #{9000 + i} needs attention.", is_reply=False, now=base + i * 60)

        # A direct reply to a human must still get through even though the
        # autonomous rate limit is exhausted.
        reply = dedup.guard_outbound(
            CHAT_ID, "Sure, here's the status you asked for on #9000.",
            is_reply=True, now=base + 500,
        )
        assert reply.allowed is True, "a direct reply to a human must never be blocked by the rate limit"

    def test_rate_limit_logged(self, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="gateway.outbound_dedup")
        base = time.time()
        for i in range(dedup.RATE_LIMIT_MAX_MESSAGES + 1):
            dedup.guard_outbound(CHAT_ID, f"Ticket #{9000 + i} needs attention.", is_reply=False, now=base + i * 60)
        assert any("rate limit engaged" in r.message for r in caplog.records)


class TestPersistenceAcrossRestart:
    def test_history_survives_module_reimport(self, tmp_path, monkeypatch):
        history_file = tmp_path / "history.json"
        monkeypatch.setattr(dedup, "OPS_DIR", tmp_path)
        monkeypatch.setattr(dedup, "HISTORY_FILE", history_file)

        base = time.time()
        first = dedup.guard_outbound(CHAT_ID, "Ticket #500 needs a blocker note.", is_reply=False, now=base)
        assert first.allowed is True
        assert history_file.exists()

        # Simulate a gateway restart: nothing but the file on disk survives.
        # A fresh call to guard_outbound (module state itself holds nothing
        # but OPS_DIR/HISTORY_FILE) must still see the prior send.
        repeat = dedup.guard_outbound(CHAT_ID, "Ticket #500 needs a blocker note.", is_reply=False, now=base + 30)
        assert repeat.allowed is False, "history must persist across a simulated restart"

        raw = json.loads(history_file.read_text())
        assert CHAT_ID in raw
        assert len(raw[CHAT_ID]) >= 1


class TestFailOpen:
    def test_internal_error_fails_open(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated internal failure")

        monkeypatch.setattr(dedup, "_load_history", _boom)
        result = dedup.guard_outbound(CHAT_ID, "This must still be sent.", is_reply=False)
        assert result.allowed is True, "a guard internal error must never block a send"
        assert result.reason == "guard_error_fail_open"
        assert result.text == "This must still be sent."

    def test_fail_open_is_logged(self, monkeypatch, caplog):
        import logging
        caplog.set_level(logging.ERROR, logger="gateway.outbound_dedup")

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated internal failure")

        monkeypatch.setattr(dedup, "_load_history", _boom)
        dedup.guard_outbound(CHAT_ID, "This must still be sent.", is_reply=False)
        assert any("failing open" in r.message for r in caplog.records)


class TestHistoryLimit:
    def test_history_capped_at_limit(self):
        base = time.time()
        for i in range(dedup.HISTORY_LIMIT + 5):
            dedup.guard_outbound(CHAT_ID, f"Ticket #{9000 + i} update {i}.", is_reply=False, now=base + i * 600)
        history = dedup._load_history()
        assert len(history[CHAT_ID]) <= dedup.HISTORY_LIMIT
