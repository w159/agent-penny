"""Tests for the same-turn tool-refusal -> false-success-claim guard in
agent/turn_finalizer.py and the self-improvement loop it drives via
cron/behavior_store.py.

Regression target: the 2026-09-15 vision-toolset incident where a ``patch``
call to config.yaml was refused ("Refusing to write to Hermes config file...")
and the model's final response nonetheless said "These settings are now in
.../config.yaml ... No further action needed-config is live." with no
corrective footer ever reaching the delivered message.
"""
from __future__ import annotations

import logging

import pytest

from agent.turn_finalizer import (
    _append_file_mutation_footer,
    _final_response_claims_unverified_success,
    _propose_tool_refusal_correction,
    _TOOL_REFUSAL_CORRECTION_TEXT,
)

logger = logging.getLogger("test_tool_refusal_self_correction")

JERRY = "6a04ad0e-d7dd-4892-9109-c2a1e8025517"


class _FakeAgent:
    """Minimal stand-in exposing just what ``_append_file_mutation_footer`` reads."""

    def __init__(self, failed):
        self._turn_failed_file_mutations = failed

    def _file_mutation_verifier_enabled(self):
        return True

    @staticmethod
    def _format_file_mutation_failure_footer(failed):
        from agent.turn_explainers import TurnExplainersMixin
        return TurnExplainersMixin._format_file_mutation_failure_footer(failed)


def _failed_config_write():
    return {
        "/home/yoda/.hermes/config.yaml": {
            "tool": "patch",
            "error_preview": (
                "Refusing to write to Hermes config file: /home/yoda/.hermes/config.yaml "
                "Agent cannot modify security-sensitive configuration."
            ),
        }
    }


# ---------------------------------------------------------------------------
# (a) a same-turn refusal is never left described as success in the contract
#     this guard builds: the footer is always appended and always carries the
#     corrective command, regardless of how confidently the model wrote.
# ---------------------------------------------------------------------------

class TestFooterNeverLeavesFalseClaimUncorrected:
    def test_false_success_claim_gets_corrective_footer(self):
        agent = _FakeAgent(_failed_config_write())
        claim = (
            "These settings are now in /home/yoda/.hermes/config.yaml under the "
            "vision: block. No further action needed-config is live."
        )
        result = _append_file_mutation_footer(agent, claim, logger)
        assert "File-mutation verifier" in result
        assert "1 file(s) were NOT modified" in result
        # The footer is a command to self-correct next turn, not a passive footnote.
        assert "your NEXT reply" in result
        assert "must open by correcting" in result

    def test_footer_never_silently_dropped(self):
        """The footer must reach the return value on every call -- this is the
        exact failure mode from the incident: the mechanism existed but the
        delivered message carried no trace of it."""
        agent = _FakeAgent(_failed_config_write())
        result = _append_file_mutation_footer(agent, "All done.", logger)
        assert result != "All done."
        assert "File-mutation verifier" in result

    def test_no_failures_leaves_response_untouched(self):
        agent = _FakeAgent({})
        assert _append_file_mutation_footer(agent, "All done.", logger) == "All done."


class TestUnverifiedSuccessClaimDetection:
    def test_detects_the_incident_wording(self):
        text = (
            "These settings are now in /home/yoda/.hermes/config.yaml under the "
            "vision: block. No further action needed-config is live."
        )
        assert _final_response_claims_unverified_success(text) is True

    def test_honest_failure_report_is_not_flagged(self):
        text = (
            "I tried to patch config.yaml but the write was refused (security guard). "
            "The edit did not land -- you'll need to make this change yourself."
        )
        assert _final_response_claims_unverified_success(text) is False

    def test_plain_text_is_not_flagged(self):
        assert _final_response_claims_unverified_success("Ticket #123 is closed.") is False


# ---------------------------------------------------------------------------
# (b) the self-improvement hook persists a correction record when the
#     discrepancy pattern fires, using behavior_store's own audit trail.
# ---------------------------------------------------------------------------

@pytest.fixture
def behavior_db(monkeypatch, tmp_path):
    db_path = tmp_path / "behavior.db"
    monkeypatch.setattr("cron.behavior_db.DB_PATH", db_path)
    monkeypatch.setenv("TEAMS_ALLOWED_USERS", JERRY)
    return db_path


class TestSelfImprovementPersistence:
    def test_propose_persists_a_pending_instruction_with_audit_reason(self, behavior_db):
        from cron import behavior_store

        claim = "No further action needed-config is live."
        _propose_tool_refusal_correction(None, _failed_config_write(), claim, logger)

        history = behavior_store.history(db_path=behavior_db)
        assert len(history) == 1
        row = history[0]
        assert row["kind"] == "instruction"
        assert row["status"] == "pending"
        assert row["requested_by"] == "file-mutation-verifier"
        assert row["text"] == _TOOL_REFUSAL_CORRECTION_TEXT
        assert behavior_store.count_active(db_path=behavior_db) == 0  # never auto-approved

    def test_audit_trail_carries_the_catch_as_justification(self, behavior_db):
        from cron import behavior_store
        import sqlite3

        claim = "No further action needed-config is live."
        _propose_tool_refusal_correction(None, _failed_config_write(), claim, logger)

        con = sqlite3.connect(str(behavior_db))
        con.row_factory = sqlite3.Row
        audit_rows = [dict(r) for r in con.execute("SELECT * FROM audit").fetchall()]
        con.close()
        assert len(audit_rows) == 1
        assert audit_rows[0]["event"] == "proposed"
        assert "config.yaml" in audit_rows[0]["reason"]
        assert "config is live" in audit_rows[0]["reason"]

    def test_approving_the_proposal_renders_it_into_future_context(self, behavior_db):
        """Proves the fix closes the loop end-to-end: once a human approves the
        auto-proposed rule, render_active_rules() -- the function every turn's
        system prompt calls -- includes it."""
        from cron import behavior_store

        claim = "No further action needed-config is live."
        _propose_tool_refusal_correction(None, _failed_config_write(), claim, logger)
        row = behavior_store.history(db_path=behavior_db)[0]
        behavior_store.approve(row["id"], approved_by=JERRY, db_path=behavior_db)

        rendered = behavior_store.render_active_rules(db_path=behavior_db)
        assert _TOOL_REFUSAL_CORRECTION_TEXT in rendered


# ---------------------------------------------------------------------------
# (c) a duplicate future catch of the SAME pattern does not spam duplicate
#     rules -- dedupe/idempotence.
# ---------------------------------------------------------------------------

class TestDedupeAcrossRepeatedCatches:
    def test_repeated_catches_produce_one_pending_row(self, behavior_db):
        from cron import behavior_store

        for _ in range(5):
            _propose_tool_refusal_correction(
                None, _failed_config_write(), "No further action needed-config is live.", logger,
            )

        history = behavior_store.history(db_path=behavior_db)
        assert len(history) == 1, "five catches of the same pattern must yield exactly one proposal"

    def test_repeated_catches_produce_one_audit_proposed_event(self, behavior_db):
        import sqlite3
        from cron import behavior_store

        for _ in range(3):
            _propose_tool_refusal_correction(
                None, _failed_config_write(), "No further action needed-config is live.", logger,
            )

        con = sqlite3.connect(str(behavior_db))
        proposed = con.execute("SELECT COUNT(*) FROM audit WHERE event='proposed'").fetchone()[0]
        con.close()
        assert proposed == 1
        assert behavior_store.count_active(db_path=behavior_db) == 0

    def test_full_finalizer_call_is_idempotent_across_turns(self, behavior_db):
        """End-to-end through _append_file_mutation_footer (the real call site),
        not just the propose helper directly."""
        from cron import behavior_store

        claim = "These settings are now in config.yaml. No further action needed-config is live."
        for _ in range(3):
            agent = _FakeAgent(_failed_config_write())
            _append_file_mutation_footer(agent, claim, logger)

        assert len(behavior_store.history(db_path=behavior_db)) == 1
