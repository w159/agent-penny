"""Tests for cron/ops_memory.py's extraction, injection, and cap/rotation
behavior.

extract_operational_memory() used to be a stub that always returned empty
updates ("will be replaced by LLM call") — nothing was ever learned from a
run. These tests cover the deterministic replacement: it must capture real
ticket mentions from a job's own output, capture nothing when there's
nothing to capture, never invent a ticket absent from the input, still
respect the size cap/rotation, and the newly-injected files (active
outages, ROLE.md lessons) must actually reach a built job prompt.
"""
import re
from datetime import datetime, timezone

import pytest

import cron.ops_memory as ops_memory


@pytest.fixture(autouse=True)
def _isolated_ops_files(tmp_path, monkeypatch):
    """Point every ops_memory file constant at a scratch directory so tests
    never touch the real memories/ops/ files (which describe real people).
    """
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr(ops_memory, "OPS_DIR", tmp_path)
    monkeypatch.setattr(ops_memory, "ROSTER_FILE", tmp_path / "roster.md")
    monkeypatch.setattr(ops_memory, "TICKETS_FILE", tmp_path / "tickets.md")
    monkeypatch.setattr(ops_memory, "EVENTS_FILE", tmp_path / "events.md")
    monkeypatch.setattr(ops_memory, "OUTAGES_FILE", tmp_path / "active_outages.md")
    monkeypatch.setattr(ops_memory, "SECURITY_FILE", tmp_path / "security_watch.md")
    monkeypatch.setattr(ops_memory, "ROLE_FILE", tmp_path / "ROLE.md")
    monkeypatch.setattr(ops_memory, "PATTERNS_FILE", tmp_path / "patterns.md")
    monkeypatch.setattr(ops_memory, "DREAMS_FILE", tmp_path / "DREAMS.md")
    monkeypatch.setattr(ops_memory, "LOCK_FILE", tmp_path / ".ops.lock")
    monkeypatch.setattr(ops_memory, "ARCHIVE_DIR", archive_dir)
    # Trend detection hits a real log file / real tickets — neutralize it so
    # these tests isolate the extraction path under test.
    monkeypatch.setattr(
        "cron.trend_detection.run_trend_detection",
        lambda: {"event_updates": [], "stall_findings": []},
    )
    return tmp_path


class TestTicketMentionExtraction:
    def test_captures_real_ticket_mention_from_output(self, tmp_path, capsys):
        output = (
            "Board's quiet-ish. #94689 (Matthew Reed, printing to F360) is "
            "still open, needs a look."
        )
        ops_memory.extract_operational_memory("job-1", output, "triage-board-sweep")

        content = ops_memory.TICKETS_FILE.read_text(encoding="utf-8")
        assert "## #94689" in content
        assert "Matthew Reed" in content  # verbatim context line, not invented
        assert "triage-board-sweep" in content

        log = capsys.readouterr().err
        assert "captured 1 ticket mention(s)" in log

    def test_captures_nothing_from_output_with_no_new_facts(self, tmp_path, capsys):
        output = "Quiet morning. Nothing new to report. Board looks fine."
        ops_memory.extract_operational_memory("job-2", output, "triage-board-watcher")

        assert not ops_memory.TICKETS_FILE.exists()
        log = capsys.readouterr().err
        assert "captured 0 ticket mention(s)" in log

    def test_never_writes_a_ticket_absent_from_the_input(self, tmp_path):
        output = "Only #11111 was mentioned today."
        ops_memory.extract_operational_memory("job-3", output, "triage-board-sweep")

        content = ops_memory.TICKETS_FILE.read_text(encoding="utf-8")
        assert "#11111" in content
        assert "#22222" not in content  # never appeared in output — must not appear

    def test_existing_ticket_gets_appended_note_not_overwritten(self, tmp_path):
        ops_memory.TICKETS_FILE.write_text(
            "## #55555 - Printer offline\n"
            "- **Status:** Waiting Client Response\n"
            "- **Owner:** Ernesto Velarde\n",
            encoding="utf-8",
        )
        output = "Following up: #55555 still waiting on the client."
        ops_memory.extract_operational_memory("job-4", output, "triage-board-sweep")

        content = ops_memory.TICKETS_FILE.read_text(encoding="utf-8")
        # Real fields preserved — extraction must not blow away richer data.
        assert "Waiting Client Response" in content
        assert "Ernesto Velarde" in content
        assert "**Seen:**" in content

    def test_rerun_on_same_output_does_not_duplicate_the_note(self, tmp_path):
        output = "#66666 still open."
        ops_memory.extract_operational_memory("job-5a", output, "triage-board-sweep")
        first = ops_memory.TICKETS_FILE.read_text(encoding="utf-8")
        ops_memory.extract_operational_memory("job-5b", output, "triage-board-sweep")
        second = ops_memory.TICKETS_FILE.read_text(encoding="utf-8")
        assert first == second  # idempotent — no duplicate "Seen" line


class TestSizeCapAndRotation:
    def test_write_with_cap_archives_and_truncates_oversized_content(self, tmp_path):
        big = "## #1\n" + ("x" * (ops_memory.MAX_FILE_SIZE + 5000))
        ops_memory.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        ops_memory._write_with_cap(ops_memory.TICKETS_FILE, big)

        written = ops_memory.TICKETS_FILE.read_bytes()
        assert len(written) <= ops_memory.MAX_FILE_SIZE
        archives = list(ops_memory.ARCHIVE_DIR.glob("tickets_*.md"))
        assert len(archives) == 1
        assert len(archives[0].read_bytes()) > ops_memory.MAX_FILE_SIZE

    def test_rotate_archives_removes_only_old_files(self, tmp_path):
        import time

        ops_memory.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        old_file = ops_memory.ARCHIVE_DIR / "tickets_old.md"
        new_file = ops_memory.ARCHIVE_DIR / "tickets_new.md"
        old_file.write_text("old", encoding="utf-8")
        new_file.write_text("new", encoding="utf-8")

        old_mtime = time.time() - (91 * 86400)
        import os

        os.utime(old_file, (old_mtime, old_mtime))

        ops_memory._rotate_archives()

        assert not old_file.exists()
        assert new_file.exists()

    def test_extraction_still_respects_cap_end_to_end(self, tmp_path):
        # Seed tickets.md right at the cap so one more mention forces rotation.
        ops_memory.TICKETS_FILE.write_text(
            "## #1\n" + ("y" * ops_memory.MAX_FILE_SIZE), encoding="utf-8"
        )
        ops_memory.extract_operational_memory(
            "job-6", "#77777 new mention", "triage-board-sweep"
        )
        assert len(ops_memory.TICKETS_FILE.read_bytes()) <= ops_memory.MAX_FILE_SIZE


class TestRoleLessonsExtraction:
    def test_load_role_lessons_extracts_only_the_lessons_section(self, tmp_path):
        ops_memory.ROLE_FILE.write_text(
            "# ROLE.md\n\nSome narrative comms-style text that should NOT travel.\n\n"
            "### Lessons Learned (cumulative)\n\n"
            "1. **2026-08-04:** Flag active outages same day.\n\n"
            "---\n\n*trailing footer*",
            encoding="utf-8",
        )
        lessons = ops_memory._load_role_lessons()
        assert "Flag active outages same day" in lessons
        assert "should NOT travel" not in lessons
        assert "trailing footer" not in lessons


class TestPromptMemoryInjection:
    def test_load_prompt_memory_includes_outages_and_lessons(self, tmp_path):
        ops_memory.OUTAGES_FILE.write_text(
            "## ACTIVE: Outlook Sign-Out Storm\n- 4 users affected.\n", encoding="utf-8"
        )
        ops_memory.ROLE_FILE.write_text(
            "### Lessons Learned (cumulative)\n\n1. Flag outages same day.\n\n---\n",
            encoding="utf-8",
        )
        memory = ops_memory.load_prompt_memory()
        assert "Outlook Sign-Out Storm" in memory
        assert "Flag outages same day" in memory

    def test_injected_files_appear_in_built_job_prompt(self, tmp_path, monkeypatch):
        import cron.scheduler as scheduler_mod

        ops_memory.OUTAGES_FILE.write_text(
            "## ACTIVE: Test Outage\n- Something is down.\n", encoding="utf-8"
        )
        ops_memory.ROLE_FILE.write_text(
            "### Lessons Learned (cumulative)\n\n1. Nudge stalled tickets.\n\n---\n",
            encoding="utf-8",
        )
        # raising=False: this helper is guardrail-team territory and doesn't
        # exist yet in this codebase. Keeping the monkeypatch (rather than
        # deleting it) means the test stays correct once that helper lands.
        monkeypatch.setattr(
            scheduler_mod, "_warn_if_escalations_flag_missing", lambda: None, raising=False
        )

        job = {"id": "any-id", "prompt": "Check the board", "operational_memory": True}
        result = scheduler_mod._build_job_prompt(job)

        assert "Test Outage" in result
        assert "Nudge stalled tickets" in result

    def test_tickets_appear_in_built_job_prompt(self, tmp_path, monkeypatch):
        """The outages/ROLE.md test above predates tickets.md injection --
        this covers the actual remembered-ticket-lore path from the design
        doc (item 2 in the task: reads wired into the job/detector prompt).
        """
        import cron.scheduler as scheduler_mod

        ops_memory.TICKETS_FILE.write_text(
            "## #90653 — Printer bounces\n"
            f"- **Last activity:** {datetime.now(timezone.utc).date().isoformat()} — bounced again\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            scheduler_mod, "_warn_if_escalations_flag_missing", lambda: None, raising=False
        )

        job = {"id": "any-id", "prompt": "Check the board", "operational_memory": True}
        result = scheduler_mod._build_job_prompt(job)

        assert "#90653" in result
        assert "NOT live state" in result  # format_memory_for_prompt's caveat wrapper

    def test_operational_memory_off_by_default(self, tmp_path, monkeypatch):
        """A job that never sets `operational_memory` must not see any of
        this - the flag is explicit opt-in (mirrors board_watch/outage_routing).
        """
        import cron.scheduler as scheduler_mod

        ops_memory.TICKETS_FILE.write_text(
            "## #90653 — Printer bounces\n- **Last activity:** 2026-08-28 — bounced again\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            scheduler_mod, "_warn_if_escalations_flag_missing", lambda: None, raising=False
        )

        job = {"id": "any-id", "prompt": "Check the board"}
        result = scheduler_mod._build_job_prompt(job)

        assert "#90653" not in result


class TestEntryExpiry:
    """Guards the fix for stale ops-memory injection: an entry older than
    DEFAULT_MAX_AGE_DAYS must never reach the rendered prompt block, a fresh
    one must, the boundary is documented as inclusive, and every surviving
    entry must carry a visible age label rather than bare, undated fact.
    """

    def test_old_entry_excluded_from_filtered_output(self):
        from datetime import datetime, timezone

        content = (
            "## #11111 - Old ticket\n"
            "- **Last activity:** 2026-07-18 — still open\n"
        )
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)  # 41 days later
        filtered, kept, dropped = ops_memory.filter_stale_entries(content, now=now)
        assert kept == 0
        assert dropped == 1
        assert "#11111" not in filtered

    def test_fresh_entry_included_in_filtered_output(self):
        from datetime import datetime, timezone

        content = (
            "## #22222 - Fresh ticket\n"
            "- **Last activity:** 2026-08-27 — opened yesterday\n"
        )
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)  # 1 day later
        filtered, kept, dropped = ops_memory.filter_stale_entries(content, now=now)
        assert kept == 1
        assert dropped == 0
        assert "#22222" in filtered

    def test_boundary_entry_exactly_at_max_age_is_kept(self):
        from datetime import datetime, timezone

        content = (
            "## #33333 - Boundary ticket\n"
            "- **Last activity:** 2026-08-21 — exactly at the edge\n"
        )
        # DEFAULT_MAX_AGE_DAYS is 7; 2026-08-28 minus 2026-08-21 is exactly 7.
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)
        filtered, kept, dropped = ops_memory.filter_stale_entries(
            content, max_age_days=ops_memory.DEFAULT_MAX_AGE_DAYS, now=now
        )
        assert kept == 1  # documented as inclusive: age == max_age_days survives
        assert dropped == 0
        assert "#33333" in filtered

        # One day further out crosses the boundary and must be dropped.
        content_one_more_day = (
            "## #44444 - Just past the edge\n"
            "- **Last activity:** 2026-08-20 — one day past the edge\n"
        )
        filtered2, kept2, dropped2 = ops_memory.filter_stale_entries(
            content_one_more_day, max_age_days=ops_memory.DEFAULT_MAX_AGE_DAYS, now=now
        )
        assert kept2 == 0
        assert dropped2 == 1

    def test_rendered_block_carries_dates(self):
        from datetime import datetime, timezone

        content = (
            "## #55555 - Dated ticket\n"
            "- **Last activity:** 2026-08-25 — waiting on client\n"
        )
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)
        filtered, kept, dropped = ops_memory.filter_stale_entries(content, now=now)
        assert kept == 1
        assert "2026-08-25" in filtered
        assert "3 days ago" in filtered  # explicit age label on the surviving entry

    def test_entry_with_no_date_at_all_is_kept_and_labeled_unknown(self):
        content = "## #66666 - No date anywhere in this entry\n- Some note.\n"
        filtered, kept, dropped = ops_memory.filter_stale_entries(content)
        assert kept == 1
        assert dropped == 0
        assert "no date found in entry" in filtered

    def test_load_operational_memory_prunes_stale_tickets_from_prompt(self, tmp_path):
        ops_memory.TICKETS_FILE.write_text(
            "## #77777 - Very stale\n- **Last activity:** 2020-01-01 — ancient\n",
            encoding="utf-8",
        )
        memory = ops_memory.load_operational_memory()
        assert "#77777" not in memory

    def test_load_prompt_memory_prunes_stale_outages(self, tmp_path):
        ops_memory.OUTAGES_FILE.write_text(
            "## ACTIVE: Ancient Outage\n- Down since 2020-01-01.\n",
            encoding="utf-8",
        )
        memory = ops_memory.load_prompt_memory()
        assert "Ancient Outage" not in memory

    def test_format_memory_for_prompt_states_it_is_not_live_state(self):
        block = ops_memory.format_memory_for_prompt("## #1\n- something\n")
        assert "NOT live state" in block
        assert "ConnectWise" in block


class TestRosterUntouched:
    def test_extraction_never_creates_or_modifies_roster(self, tmp_path):
        output = "Jarvis closed #33333 today, great work."
        ops_memory.extract_operational_memory("job-7", output, "triage-board-sweep")
        # Deliberate scope limit: roster is human/weekly-audit maintained,
        # never auto-written from freeform prose.
        assert not ops_memory.ROSTER_FILE.exists()


class TestBehaviorPatterns:
    """record_behavior_pattern() - the append-only, human-readable log for
    a workflow correction a real technician's action revealed (design doc
    item 4). Deliberately not an LLM summary: every field is caller-supplied
    verbatim, same discipline as ticket-mention extraction.
    """

    def test_records_a_pattern_with_all_fields(self):
        ops_memory.record_behavior_pattern(
            "schedule_reschedule",
            observed="Tech closed appt #4821 and created #4822 instead of moving it.",
            assumed="PATCH the existing appointment's time in place.",
            why="PATCHing in place destroys calendar history.",
            source="ticket #93102",
        )
        content = ops_memory.PATTERNS_FILE.read_text(encoding="utf-8")
        assert "schedule_reschedule" in content
        assert "closed appt #4821" in content
        assert "PATCH the existing appointment" in content
        assert "destroys calendar history" in content
        assert "#93102" in content

    def test_rerun_with_identical_fields_does_not_duplicate(self):
        kwargs = dict(
            observed="Tech closed appt #1 and created #2.",
            assumed="PATCH in place.",
            why="Preserves history.",
            source="ticket #1",
        )
        ops_memory.record_behavior_pattern("schedule_reschedule", **kwargs)
        first = ops_memory.PATTERNS_FILE.read_text(encoding="utf-8")
        ops_memory.record_behavior_pattern("schedule_reschedule", **kwargs)
        second = ops_memory.PATTERNS_FILE.read_text(encoding="utf-8")
        assert first == second

    def test_missing_required_field_is_not_written(self):
        ops_memory.record_behavior_pattern("x", observed="", assumed="a", why="", source="s")
        assert not ops_memory.PATTERNS_FILE.exists()

    def test_pattern_appears_in_prompt_memory_and_is_not_age_filtered(self):
        ops_memory.record_behavior_pattern(
            "schedule_reschedule",
            observed="Old observation from a while back.",
            assumed="PATCH in place.",
            why="Preserves history.",
            source="ticket #1",
        )
        # Backdate the entry well past every other file's expiry window to
        # prove patterns.md is deliberately exempt from filter_stale_entries.
        content = ops_memory.PATTERNS_FILE.read_text(encoding="utf-8")
        content = content.replace(datetime.now(timezone.utc).date().isoformat(), "2020-01-01")
        ops_memory.PATTERNS_FILE.write_text(content, encoding="utf-8")

        memory = ops_memory.load_prompt_memory()
        assert "Learned Workflow Patterns" in memory
        assert "Preserves history" in memory


def _concurrent_mention_worker(ticket_id: str, ops_dir: str) -> None:
    """Module-level (picklable) worker body for the multiprocessing test
    below - re-points a fresh import of ops_memory at the shared tmp dir,
    since monkeypatch state never crosses a process boundary.
    """
    import cron.ops_memory as om
    from pathlib import Path

    base = Path(ops_dir)
    om.OPS_DIR = base
    om.TICKETS_FILE = base / "tickets.md"
    om.EVENTS_FILE = base / "events.md"
    om.ARCHIVE_DIR = base / "archive"
    om.LOCK_FILE = base / ".ops.lock"
    om._apply_ticket_mention(ticket_id, "2026-09-10", "concurrent-sweep", f"seen #{ticket_id}")


class TestConcurrentWrites:
    """_ops_lock() guards the read -> dedup-check -> write section against
    two SEPARATE PROCESSES racing the same file - cron jobs fire as
    subprocesses, not threads, so the regression this guards is only
    reproducible with multiprocessing, not a threading test.
    """

    def test_concurrent_ticket_mentions_from_separate_processes_all_survive(
        self, tmp_path
    ):
        import multiprocessing

        procs = []
        n = 8
        for i in range(n):
            ticket_id = f"7{i:04d}"
            p = multiprocessing.Process(
                target=_concurrent_mention_worker, args=(ticket_id, str(tmp_path))
            )
            procs.append(p)
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0

        content = (tmp_path / "tickets.md").read_text(encoding="utf-8")
        entries = re.findall(r"## #(\d+)", content)
        expected = {f"7{i:04d}" for i in range(n)}
        # Every process's ticket must survive - none lost to a lost-update
        # race between concurrent read-modify-write cycles.
        assert set(entries) == expected, (
            f"expected all {n} tickets, got {sorted(entries)} "
            f"(missing: {expected - set(entries)})"
        )
