"""Tests for cron/board_watch.py's Python-built card delivery.

The watcher's fired deltas used to become a free-text prompt block
(``build_board_prompt_block``) that the model narrated into its own final
response. That is the same "model authors the outbound content" pattern
that produced a malformed card JSON in production on 2026-08-04
(``Expecting ',' delimiter: line 1 column 746``). ``deliver_board_deltas``
replaces that for the actual Teams delivery: it builds one Adaptive Card
per fired ticket in plain Python (never asking the model to write JSON)
and fans them out through ``send_paced``, one distinct send per ticket.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from cron.board_watch import BoardDeltaCandidate, BoardDeltaResult, deliver_board_deltas
from cron.scheduler import _build_job_prompt, _deliver_result
from cron.trend_detection import Ticket, _significant_tokens

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _candidate(ticket_id, reason="new", summary="Cannot log in", board="Triage"):
    return BoardDeltaCandidate(
        ticket_id=ticket_id,
        contact="Nicole McFarland",
        priority="Priority 1 - Emergency",
        status="New",
        summary=summary,
        reason=reason,
        board=board,
    )


def _card_json_from_message(message: str) -> dict:
    """Pull the JSON object out of a ```adaptivecard fence."""
    inner = message.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(inner)


class TestDeliverBoardDeltasFanOut:
    @pytest.mark.asyncio
    async def test_five_fired_tickets_produce_five_distinct_sends(self):
        candidates = [_candidate(90000 + i) for i in range(5)]
        result = BoardDeltaResult(fired=candidates, deferred_count=0, total_open=5, silent=False)

        sent = []

        async def fake_send(message):
            sent.append(message)
            return {"ok": True}

        results = await deliver_board_deltas(result, fake_send, delay_s=0)

        assert len(sent) == 5
        assert len(results) == 5
        numbers = {
            _card_json_from_message(m)["body"][0]["columns"][1]["items"][0]["text"].split(" : ")[0]
            for m in sent
        }
        assert numbers == {f"Ticket #{90000 + i}" for i in range(5)}

    @pytest.mark.asyncio
    async def test_silent_result_sends_nothing(self):
        result = BoardDeltaResult(fired=[], deferred_count=0, total_open=0, silent=True)

        async def fake_send(message):
            raise AssertionError("should not be called")

        results = await deliver_board_deltas(result, fake_send, delay_s=0)
        assert results == []


class TestDeliverBoardDeltasEscaping:
    @pytest.mark.asyncio
    async def test_summary_with_quotes_and_apostrophes_round_trips_cleanly(self):
        # Regression guard for the 2026-08-04 production break: a model-
        # authored card literally emitted `\"access denied\"` and broke JSON
        # parsing. Python's json.dumps handles this correctly by
        # construction; this test proves the card is still valid JSON and
        # the original text survives intact.
        tricky_summary = 'User says "access denied" and can\'t log in'
        candidates = [_candidate(90001, summary=tricky_summary)]
        result = BoardDeltaResult(fired=candidates, deferred_count=0, total_open=1, silent=False)

        sent = []

        async def fake_send(message):
            sent.append(message)

        await deliver_board_deltas(result, fake_send, delay_s=0)

        assert len(sent) == 1
        # json.loads succeeding at all is the load-bearing assertion: the
        # 2026-08-04 break was exactly a card whose JSON failed to parse.
        card = _card_json_from_message(sent[0])
        summary_text = card["body"][0]["columns"][1]["items"][0]["text"]
        assert '"access denied"' in summary_text
        assert "can't log in" in summary_text


def _board_watch_job(**overrides):
    job = {
        "id": "board-watcher-001",
        "name": "board-watcher",
        "board_watch": True,
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }
    job.update(overrides)
    return job


def _fired_result(n=2):
    candidates = [_candidate(90000 + i) for i in range(n)]
    return BoardDeltaResult(fired=candidates, deferred_count=0, total_open=n, silent=False)


class TestDeliverResultBoardWatchIntegration:
    """_deliver_result() must route a stashed BoardDeltaResult through
    deliver_board_deltas as Python-built cards and suppress the model's
    narrative `content` entirely, not send both (r94689: ticket #94689
    posted 7 times because the model also narrated the same tickets)."""

    def test_fired_deltas_send_cards_and_suppress_narrative_text(self):
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        job = _board_watch_job(_board_watch_deltas=_fired_result(2))

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock:
            result = _deliver_result(job, "Narrative text repeating the same tickets.")

        assert result is None
        assert send_mock.call_count == 2
        for call in send_mock.call_args_list:
            sent_content = call.kwargs.get("content") or call.args[3]
            assert "adaptivecard" in sent_content
            assert "Narrative text repeating the same tickets." not in sent_content

    def test_no_fired_deltas_falls_through_to_unchanged_text_delivery(self):
        """Regression guard: board_watch job with nothing fired (or no stash)
        must deliver the model's text exactly as before this change."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        empty_result = BoardDeltaResult(fired=[], deferred_count=0, total_open=0, silent=True)
        job = _board_watch_job(_board_watch_deltas=empty_result)

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock:
            result = _deliver_result(job, "Nothing new to report.")

        assert result is None
        send_mock.assert_called_once()
        sent_content = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][3]
        assert "Nothing new to report." in sent_content
        assert "_board_watch_deltas" not in job

    def test_no_stash_at_all_is_unaffected(self):
        """A plain (non-board-watch) job must behave identically."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        job = {
            "id": "plain-job",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "123"},
        }

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock:
            result = _deliver_result(job, "Business as usual.")

        assert result is None
        send_mock.assert_called_once()
        sent_content = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][3]
        assert "Business as usual." in sent_content

    def test_card_delivery_exception_falls_back_to_text_delivery(self):
        """If deliver_board_deltas raises, the update must not be silently
        dropped, fall back to the normal text path."""
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        job = _board_watch_job(_board_watch_deltas=_fired_result(1))

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("cron.board_watch.deliver_board_deltas", side_effect=RuntimeError("boom")):
            result = _deliver_result(job, "Fallback narrative text.")

        assert result is None
        send_mock.assert_called_once()
        sent_content = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][3]
        assert "Fallback narrative text." in sent_content

    def test_stash_is_cleared_even_when_card_delivery_raises(self):
        job = _board_watch_job(_board_watch_deltas=_fired_result(1))

        with patch("gateway.config.load_gateway_config", side_effect=RuntimeError("no config")), \
             patch("cron.board_watch.deliver_board_deltas", side_effect=RuntimeError("boom")):
            _deliver_result(job, "text")

        assert "_board_watch_deltas" not in job


def _live_ticket(id, priority="Priority 1 - Emergency", status="New", closed=False,
                  board="Triage", contact="Nicole McFarland", summary="Cannot log in"):
    """A trend_detection.Ticket that select_board_deltas will treat as a
    genuinely fresh, fire-worthy row (see TestNewUnassignedUrgentTicket in
    tests/cron/test_board_watch.py for the same fixture shape)."""
    return Ticket(
        id=id,
        summary=summary,
        contact=contact,
        status=status,
        closed=closed,
        priority=priority,
        entered_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(minutes=5),
        tokens=_significant_tokens(summary),
        board=board,
    )


@pytest.fixture
def isolated_board_watch_state(tmp_path, monkeypatch):
    """Point board_watch state at a scratch file so these tests never touch
    the real memories/ops/board_watch_state.json (same isolation pattern as
    tests/cron/test_board_watch.py's autouse fixture, but not autouse here
    since only the _build_job_prompt/run_job tests below actually call
    select_board_deltas)."""
    state_file = tmp_path / "board_watch_state.json"
    monkeypatch.setattr("cron.board_watch.OPS_DIR", tmp_path)
    monkeypatch.setattr("cron.board_watch.STATE_FILE", state_file)
    return state_file


def _seen_at_medium(state_file, ticket_id):
    """Pre-seed the snapshot so the ticket is already known at a non-blocking
    band.

    A first sighting is deliberately silent now (the ConnectWise callback
    lane owns "new"), so the transition these delivery tests need is a
    priority escalation, which requires a prior snapshot to escalate FROM.
    """
    state_file.write_text(
        json.dumps({str(ticket_id): {
            "closed": False, "severity_band": "normal",
            "status": "New", "last_seen": "2026-08-04T11:00:00+00:00",
        }}),
        encoding="utf-8",
    )


class TestBuildJobPromptStashesBoardWatchDeltas:
    """Covers the WRITE side of the stash-and-carry mechanism (the read side
   , _deliver_result popping the stash, is covered above and pre-seeds the
    stash by hand). Nothing previously proved that _build_job_prompt itself
    creates job["_board_watch_deltas"]; if that line regressed, every
    existing test in this file would still pass and board-watch cards would
    silently stop firing in production."""

    def test_board_watch_job_stashes_the_computed_delta_result(self, isolated_board_watch_state):
        _seen_at_medium(isolated_board_watch_state, 94689)
        job = {
            "id": "board-watcher-001",
            "name": "board-watcher",
            "board_watch": True,
            "prompt": "Report board changes.",
        }

        with patch(
            "cron.trend_detection.load_tickets_from_cw_log",
            return_value=[_live_ticket(94689)],
        ):
            _build_job_prompt(job)

        assert "_board_watch_deltas" in job
        stashed = job["_board_watch_deltas"]
        from cron.board_watch import BoardDeltaResult
        assert isinstance(stashed, BoardDeltaResult)
        assert not stashed.silent
        assert len(stashed.fired) == 1
        assert stashed.fired[0].ticket_id == 94689

    def test_stash_and_prompt_block_are_built_from_the_same_object(self, isolated_board_watch_state):
        """The stash and the model-facing prompt block must never be able to
        diverge, assert build_board_prompt_block is invoked with the exact
        object that ends up stashed on the job."""
        job = {
            "id": "board-watcher-001",
            "name": "board-watcher",
            "board_watch": True,
            "prompt": "Report board changes.",
        }

        from cron.board_watch import build_board_prompt_block as real_build_block

        seen = {}

        def spy_build_block(result):
            seen["arg"] = result
            return real_build_block(result)

        with patch(
            "cron.trend_detection.load_tickets_from_cw_log",
            return_value=[_live_ticket(94689)],
        ), patch("cron.board_watch.build_board_prompt_block", side_effect=spy_build_block):
            _build_job_prompt(job)

        assert seen["arg"] is job["_board_watch_deltas"]

    def test_job_without_board_watch_leaves_no_stash(self, isolated_board_watch_state):
        job = {
            "id": "plain-job",
            "name": "plain",
            "prompt": "Just answer a question.",
        }

        with patch(
            "cron.trend_detection.load_tickets_from_cw_log",
            return_value=[_live_ticket(94689)],
        ):
            _build_job_prompt(job)

        assert "_board_watch_deltas" not in job


class TestRunJobFullChainDeliversCardsWithoutPreseededStash:
    """End-to-end: drive run_one_job (execute -> save -> deliver -> mark,
    the same body the ticker uses to fire a due job, see
    tests/cron/test_run_one_job.py) for a board_watch job, with the model
    returning a narrative that repeats the same ticket _build_job_prompt
    already found. Proves the whole chain, stash creation in
    _build_job_prompt through pop-and-card-send in _deliver_result, fires
    without any test hand-seeding job["_board_watch_deltas"].

    run_job() alone (without run_one_job's wrapper) does NOT deliver:
    execute and deliver are split across two functions, with delivery only
    reachable through run_one_job (or tick()). An earlier version of this
    test called run_job() directly and silently asserted nothing, because
    delivery never ran.
    """

    def test_fired_delta_sends_card_and_suppresses_model_narrative(
        self, tmp_path, isolated_board_watch_state
    ):
        from gateway.config import Platform

        _seen_at_medium(isolated_board_watch_state, 94689)

        job = {
            "id": "board-watcher-001",
            "name": "board-watcher",
            "board_watch": True,
            "prompt": "Report board changes.",
            "model": "test-model",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "123"},
        }

        fake_db = MagicMock()
        fake_db.get_compression_tip.side_effect = lambda session_id: session_id

        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {
            "final_response": "Ticket #94689 is new on Triage, priority 1."
        }

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

        import cron.scheduler as sched

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "test-key",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent", return_value=mock_agent), \
             patch(
                 "cron.trend_detection.load_tickets_from_cw_log",
                 return_value=[_live_ticket(94689)],
             ), \
             patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch.object(sched, "create_execution", return_value={"id": "ex-test"}), \
             patch.object(sched, "save_job_output", return_value="/tmp/board-watcher-001.txt"), \
             patch.object(sched, "mark_job_run", return_value=None), \
             patch(
                 "tools.send_message_tool._send_to_platform",
                 new=AsyncMock(return_value={"success": True}),
             ) as send_mock:
            ok = sched.run_one_job(job)

        assert ok is True
        # The Python-built card went out, not the model's narrative text.
        send_mock.assert_called_once()
        sent_content = send_mock.call_args.kwargs.get("content") or send_mock.call_args[0][3]
        assert "adaptivecard" in sent_content
        assert "Ticket #94689 is new on Triage, priority 1." not in sent_content
        # The stash was consumed by delivery, not left dangling on the job.
        assert "_board_watch_deltas" not in job
