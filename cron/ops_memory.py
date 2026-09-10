#!/usr/bin/env python3
"""
Operational Memory for Agent Penny - read/load + extraction helpers.
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_constants import get_hermes_home

OPS_DIR = get_hermes_home() / "memories" / "ops"
ROSTER_FILE = OPS_DIR / "roster.md"
TICKETS_FILE = OPS_DIR / "tickets.md"
EVENTS_FILE = OPS_DIR / "events.md"
OUTAGES_FILE = OPS_DIR / "active_outages.md"
SECURITY_FILE = OPS_DIR / "security_watch.md"
ROLE_FILE = OPS_DIR / "ROLE.md"

# File size caps (bytes)
MAX_FILE_SIZE = 100 * 1024  # 100 KB per file
ARCHIVE_DIR = OPS_DIR / "archive"

# Entries older than this are dropped at read time rather than injected as
# fact. 7 days (one work week): the extraction path (_apply_ticket_mention,
# apply_stall_flags) re-dates an entry's "latest" note every time a later
# job's output still mentions it, so a ticket that is genuinely still open
# keeps refreshing its own age and surviving the window on its own. An
# entry that goes 7+ days without any job seeing it again is exactly the
# stale-fact case the product owner reported (39 days old, ticket almost
# certainly closed) -- it should stop being asserted as current.
DEFAULT_MAX_AGE_DAYS = 7
# Events describe recurring patterns rather than the state of one ticket, so
# they earn a longer window than ticket notes before they stop being context
# and start being noise.
EVENTS_MAX_AGE_DAYS = 30

_ENTRY_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _entry_latest_date(entry: str):
    """Return the most recent YYYY-MM-DD date found anywhere in an entry's
    text, or None if the entry carries no parseable date at all.

    Entries mix several date-bearing fields (First seen, Last activity, "as
    of <date>", auto-captured "Seen" notes) with no single canonical field,
    so the reliable signal is "the latest date mentioned anywhere in this
    entry" rather than parsing one named field.
    """
    dates = []
    for raw in _ENTRY_DATE_RE.findall(entry):
        try:
            dates.append(datetime.strptime(raw, "%Y-%m-%d").date())
        except ValueError:
            continue
    return max(dates) if dates else None


def _split_entries(content: str) -> list[str]:
    """Split a memory file's body into individual `## `-headed entries,
    dropping any preamble (format templates, doc headers) that precedes the
    first entry -- that text is instructions for humans editing the file,
    not a fact to inject into every prompt.
    """
    entries = re.split(r"\n(?=## )", content.strip())
    return [e.strip() for e in entries if e.lstrip().startswith("## ")]


def _label_entry_age(entry: str, latest, age_days) -> str:
    """Prepend an explicit age note to an entry's header line so the model
    sees "this is N days old" instead of bare, undated fact.
    """
    header, _, rest = entry.partition("\n")
    if latest is not None:
        note = f"  _(as of {latest.isoformat()}, {age_days} day{'s' if age_days != 1 else ''} ago)_"
    else:
        note = "  _(no date found in entry -- verify before relying on this)_"
    labeled = header + note
    return f"{labeled}\n{rest}" if rest else labeled


def filter_stale_entries(content: str, max_age_days: int = DEFAULT_MAX_AGE_DAYS, *, now=None):
    """Read-time expiry filter: drop entries whose most recent date is more
    than `max_age_days` days before `now`, and label every surviving entry
    with its age.

    Boundary is inclusive: an entry exactly `max_age_days` days old is kept
    (age_days <= max_age_days survives; age_days > max_age_days is dropped).

    Filtering at read time rather than pruning on write is the deliberate
    choice here: it can never destroy data (a bug in the filter loses
    nothing, it just over- or under-injects for one run), it applies
    uniformly across tickets/outages/security without needing a write path
    for each, and file growth is already bounded by _write_with_cap's
    archive-and-truncate on every write. Returns (filtered_content,
    kept_count, dropped_count).
    """
    if not content or not content.strip():
        return "", 0, 0

    now = now or datetime.now(timezone.utc)
    today = now.date()
    cutoff_age = timedelta(days=max_age_days)

    kept: list[str] = []
    kept_count = 0
    dropped_count = 0
    for entry in _split_entries(content):
        latest = _entry_latest_date(entry)
        if latest is not None and (today - latest) > cutoff_age:
            dropped_count += 1
            continue
        age_days = (today - latest).days if latest is not None else None
        kept.append(_label_entry_age(entry, latest, age_days))
        kept_count += 1

    return "\n\n".join(kept), kept_count, dropped_count


def load_operational_memory() -> str:
    """Load and concatenate the three learned-fact ops memory files with
    headers. Used both for prompt injection and as the "don't repeat this"
    context an extraction pass reads before writing new facts.

    tickets.md entries are expired at read time (see filter_stale_entries)
    since ticket notes go stale fast and nothing else reconciles them when
    the underlying ticket closes. roster.md is reference data about people
    (roles, who owns what) rather than a dated status log, so it is not
    age-filtered -- pruning it by entry date would drop a person's role
    description just because nobody happened to update it recently.
    events.md is left as-is; it is outside this function's owned scope.
    """
    parts = []
    for path, header in [(ROSTER_FILE, "## People (Roster -- reference data, not time-sensitive)")]:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"{header}\n{content}")

    if TICKETS_FILE.exists():
        raw = TICKETS_FILE.read_text(encoding="utf-8").strip()
        if raw:
            filtered, kept, dropped = filter_stale_entries(raw)
            if dropped:
                print(
                    f"[ops_memory] tickets.md: {kept} entry(ies) injected, "
                    f"{dropped} entry(ies) older than {DEFAULT_MAX_AGE_DAYS}d omitted from prompt",
                    file=sys.stderr,
                )
            if filtered:
                parts.append(f"## Tickets (remembered, not live -- see note above)\n{filtered}")

    if EVENTS_FILE.exists():
        content = EVENTS_FILE.read_text(encoding="utf-8").strip()
        if content:
            # Events are recurring PATTERNS ("Outlook access-denied cluster"),
            # not ticket notes, so they stay useful longer than the 7-day
            # ticket window -- but not forever. Unfiltered, a July pattern was
            # still being injected in late August as if it were current, which
            # is exactly the stale-answer complaint this expiry exists to fix.
            filtered, kept, dropped = filter_stale_entries(
                content, max_age_days=EVENTS_MAX_AGE_DAYS
            )
            if dropped:
                print(
                    f"[ops_memory] events.md: {kept} entry(ies) injected, "
                    f"{dropped} entry(ies) older than {EVENTS_MAX_AGE_DAYS}d omitted from prompt",
                    file=sys.stderr,
                )
            if filtered:
                parts.append(f"## Events (remembered patterns, not live -- see note above)\n{filtered}")

    return "\n\n".join(parts) if parts else ""


def _load_role_lessons() -> str:
    """Pull just the distilled 'Lessons Learned' section out of ROLE.md.

    ROLE.md is read back only by the weekly audit job that writes it — the
    hourly sweep and 15-minute watcher never see the corrections Jerry has
    already given. Injecting the whole file (comms style, board reference,
    personnel list) into every job would add ~10KB of mostly-irrelevant
    prompt per run; the lessons list is the part that changes behavior, so
    that's the only part that travels.
    """
    if not ROLE_FILE.exists():
        return ""
    content = ROLE_FILE.read_text(encoding="utf-8")
    match = re.search(
        r"### Lessons Learned \(cumulative\)\n(.*?)\n---", content, re.DOTALL
    )
    return match.group(1).strip() if match else ""


def load_prompt_memory() -> str:
    """Everything that should actually reach a job's prompt: learned facts
    (roster/tickets/events), what's down right now, current security
    findings, and lessons already learned so they get applied instead of
    just journaled.

    DREAMS.md is deliberately excluded here — it's a nightly reflection
    journal, not a fact store, and injecting it raw would add ~10KB of
    narrative per job on top of what's already a bloat problem (the
    15-minute watcher's prompt was measured at ~66KB). Its value already
    reaches jobs through this function: DREAMS.md lessons get promoted into
    ROLE.md's "Lessons Learned" section by the weekly audit, and that
    section is what `_load_role_lessons()` returns.
    """
    parts = []
    facts = load_operational_memory()
    if facts:
        parts.append(facts)
    for path, header in [
        (OUTAGES_FILE, "## Active Outages (remembered, not live -- see note above)"),
        (SECURITY_FILE, "## Security Watch (remembered, not live -- see note above)"),
    ]:
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                filtered, kept, dropped = filter_stale_entries(raw)
                if dropped:
                    print(
                        f"[ops_memory] {path.name}: {kept} entry(ies) injected, "
                        f"{dropped} entry(ies) older than {DEFAULT_MAX_AGE_DAYS}d omitted from prompt",
                        file=sys.stderr,
                    )
                if filtered:
                    parts.append(f"{header}\n{filtered}")
    lessons = _load_role_lessons()
    if lessons:
        parts.append(f"## Lessons Learned (from ROLE.md)\n{lessons}")
    return "\n\n".join(parts) if parts else ""


def format_memory_for_prompt(memory: str) -> str:
    """Format memory block for injection into Penny's prompt."""
    if not memory:
        return ""
    return (
        "## What You Already Know (remembered context, NOT live state)\n"
        "The following is remembered context from past runs -- people, "
        "ticket notes, past outages, security findings, and lessons already "
        "learned. Every dated entry below shows its age; entries older than "
        f"{DEFAULT_MAX_AGE_DAYS} days have already been dropped, but a "
        "surviving entry can still be stale or resolved by now. This is "
        "background, not current fact: before stating a ticket's status, "
        "calling something active/open, or answering with any of this as "
        "current, query the ConnectWise tool for the live record. Use this "
        "block only to personalize tone, recognize recurring patterns, and "
        "apply corrections already learned -- never as a substitute for a "
        "live lookup.\n\n"
        f"{memory}\n\n"
        "---\n\n"
    )


_TICKET_MENTION_RE = re.compile(r"#(\d{4,6})\b")
_MAX_TICKET_MENTIONS_PER_RUN = 25  # guardrail against a runaway/garbled response


def _extract_ticket_mentions(agent_output: str) -> list[dict]:
    """Deterministically pull ticket IDs Penny's own output mentions, each
    with the literal line of context it appeared in. No inference, no
    invented fields — every id and context string came verbatim from the
    run's real output, so this cannot fabricate a ticket that wasn't there.
    """
    seen: dict[str, str] = {}
    for line in agent_output.splitlines():
        for m in _TICKET_MENTION_RE.finditer(line):
            ticket_id = m.group(1)
            if ticket_id in seen:
                continue
            context = line.strip()
            if len(context) > 200:
                context = context[:200] + "..."
            seen[ticket_id] = context
            if len(seen) >= _MAX_TICKET_MENTIONS_PER_RUN:
                return [{"id": tid, "context": ctx} for tid, ctx in seen.items()]
    return [{"id": tid, "context": ctx} for tid, ctx in seen.items()]


def extract_operational_memory(job_id: str, agent_output: str, job_name: str) -> None:
    """
    Post-job extraction: capture what a job's own output actually said,
    deterministically, and route it through the existing merge/apply
    machinery (_apply_ticket_mention, apply_stall_flags, _write_with_cap).

    This used to be a stub ("will be replaced by LLM call") that always
    returned empty updates — nothing has ever been learned from a run. The
    replacement is deliberately NOT an LLM call. These files describe real
    employees and real tickets and get read back into future prompts as
    established fact; a model summarizing free-text Teams output into
    roster/ticket/event records can paraphrase its way into a status, a
    name, or a history entry that was never actually said (this system has
    already shipped one incident — see cron/trend_detection.py's docstring
    — where a sweep posted numbers that didn't reconcile). Regex-extracted
    ticket mentions can't do that: every id and context line is copied
    verbatim from the run's own output, so there is nothing to hallucinate.

    Scope, deliberately narrow:
      - tickets.md: ticket IDs (#NNNNN) mentioned in the output get a dated
        "seen" note (existing entry) or a minimal stub (new entry). Never
        invents status, owner, or priority.
      - events.md / stall flags: handled by cron/trend_detection.py, which
        was already deterministic and already wired in — unchanged here.
      - roster.md: intentionally untouched by this path. Attributing a
        role or note to a name found in freeform prose is exactly the kind
        of invention this function exists to avoid; roster stays a
        human-reviewed (weekly audit) concern.
    """
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        mentions = _extract_ticket_mentions(agent_output) if agent_output else []
        for mention in mentions:
            try:
                _apply_ticket_mention(mention["id"], today, job_name, mention["context"])
            except Exception as e:
                print(
                    f"[ops_memory] ticket mention apply error for #{mention['id']}: {e}",
                    file=sys.stderr,
                )

        # Deterministic trend detection — counting/clustering/age-math, no
        # model call. Best-effort: uses the recorded CW callback log since
        # live ConnectWise ingress is down. Re-running against the same
        # historical data is safe: event updates key by date+name and
        # replace in place rather than duplicating (see _apply_event_update),
        # and stall flags are only appended once (see apply_stall_flags).
        event_count = 0
        stall_count = 0
        try:
            from cron.trend_detection import run_trend_detection

            trend = run_trend_detection()
            trend_events = trend.get("event_updates", [])
            for update in trend_events:
                _apply_event_update(update)
            event_count = len(trend_events)

            stall_findings = trend.get("stall_findings", [])
            apply_stall_flags(stall_findings)
            stall_count = len(stall_findings)
        except Exception as e:
            print(f"[ops_memory] trend detection error: {e}", file=sys.stderr)

        # Rotate archives if files exceed size cap
        _rotate_archives()

        # Observability: make zero-capture visible in logs instead of
        # silently "succeeding" the way the stub did for weeks.
        print(
            f"[ops_memory] captured {len(mentions)} ticket mention(s), "
            f"{event_count} event update(s), {stall_count} stall flag(s), "
            f"0 roster update(s) for job={job_name!r} ({job_id})",
            file=sys.stderr,
        )

    except Exception as e:
        # Log but don't fail the job
        print(f"[ops_memory] extraction error: {e}", file=sys.stderr)


def _apply_roster_update(update: dict) -> None:
    """Merge a roster entry into roster.md."""
    name = update.get("name", "").strip()
    entry = update.get("entry", "").strip()
    if not name or not entry:
        return

    # Read current content
    content = ROSTER_FILE.read_text(encoding="utf-8") if ROSTER_FILE.exists() else ""

    # Check if entry exists (by ## Name header)
    if f"## {name}" in content:
        # Replace existing section
        pattern = rf"(## {re.escape(name)}\n.*?)(?=\n## |\Z)"
        content = re.sub(pattern, entry, content, flags=re.DOTALL)
    else:
        # Append
        if content:
            content += "\n\n---\n\n"
        content += entry

    _write_with_cap(ROSTER_FILE, content)


def _apply_ticket_update(update: dict) -> None:
    """Merge a ticket entry into tickets.md."""
    ticket_id = update.get("id", "").strip()
    entry = update.get("entry", "").strip()
    if not ticket_id or not entry:
        return

    content = TICKETS_FILE.read_text(encoding="utf-8") if TICKETS_FILE.exists() else ""

    # Check if ticket exists (by ## #<id> header)
    if f"## #{ticket_id}" in content:
        # Replace existing
        pattern = rf"(## #{re.escape(ticket_id)} .*?)(?=\n## #|\n---\n|\Z)"
        content = re.sub(pattern, entry, content, flags=re.DOTALL)
    else:
        # Append
        if content:
            content += "\n\n---\n\n"
        content += entry

    _write_with_cap(TICKETS_FILE, content)


def _apply_ticket_mention(ticket_id: str, date: str, job_name: str, context_line: str) -> None:
    """Record that a ticket was mentioned in a job's output.

    Non-destructive by design: for a ticket already tracked, appends a
    dated "seen" note the same way apply_stall_flags appends stall notes,
    rather than replacing the section (which would blow away real status/
    owner/history fields with this function's much thinner view). For a
    ticket not yet tracked, creates a minimal stub — there's nothing
    richer to preserve, and the stub carries only what was literally
    observed (id, date, job, the line it appeared in).
    """
    ticket_id = str(ticket_id).strip()
    if not ticket_id:
        return

    content = TICKETS_FILE.read_text(encoding="utf-8") if TICKETS_FILE.exists() else ""
    header = f"## #{ticket_id}"
    note = f"- **Seen:** {date} in {job_name} — \"{context_line}\"\n"

    if header in content:
        if note.strip() in content:
            return  # already recorded, avoid duplicate append
        pattern = rf"({re.escape(header)} .*?)(?=\n## #|\n---\n|\Z)"
        new_content = re.sub(
            pattern, lambda m: m.group(1).rstrip("\n") + "\n" + note, content, count=1, flags=re.DOTALL
        )
        if new_content == content:
            # Header matched the substring check but not the section regex
            # (e.g. it's the last entry with no trailing section boundary
            # right after it) — fall back to a straight append of the note
            # rather than silently dropping the mention.
            new_content = content.rstrip("\n") + "\n" + note
        _write_with_cap(TICKETS_FILE, new_content)
    else:
        entry = f"## #{ticket_id} — (auto-captured)\n- **First seen:** {date} in {job_name}\n{note}"
        if content:
            content += "\n\n---\n\n"
        content += entry
        _write_with_cap(TICKETS_FILE, content)


def _apply_event_update(update: dict) -> None:
    """Merge an event entry into events.md."""
    date = update.get("date", "").strip()
    name = update.get("name", "").strip()
    entry = update.get("entry", "").strip()
    if not date or not name or not entry:
        return

    content = EVENTS_FILE.read_text(encoding="utf-8") if EVENTS_FILE.exists() else ""

    header = f"## {date} — {name}"
    if header in content:
        # Replace existing
        pattern = rf"({re.escape(header)}\n.*?)(?=\n## \d{{4}}-\d{{2}}-\d{{2}} — |\Z)"
        content = re.sub(pattern, entry, content, flags=re.DOTALL)
    else:
        # Prepend (newest events first)
        if content:
            entry += "\n\n" + content

    _write_with_cap(EVENTS_FILE, entry)


def apply_stall_flags(stall_findings: list) -> None:
    """
    Append a dated blocker note to an already-tracked ticket's tickets.md
    section when trend detection finds it stalled past its priority's
    threshold. Only touches tickets already present — never fabricates a
    ticket entry from detection output alone. Idempotent: skips if the
    exact note is already there, so a re-run on unchanged data is a no-op.
    """
    if not stall_findings:
        return
    content = TICKETS_FILE.read_text(encoding="utf-8") if TICKETS_FILE.exists() else ""
    if not content:
        return

    changed = False
    for finding in stall_findings:
        ticket_id = str(getattr(finding, "ticket_id", ""))
        header = f"## #{ticket_id}"
        if header not in content:
            continue  # don't invent tickets we haven't already tracked
        note = (
            f"- **Auto-flag:** stalled {finding.hours_stale}h "
            f"(threshold {finding.threshold_hours}h for {finding.priority}), "
            f"no activity since last update.\n"
        )
        if note.strip() in content:
            continue  # already flagged, avoid duplicate append
        pattern = rf"({re.escape(header)} .*?)(?=\n## #|\n---\n|\Z)"
        content = re.sub(pattern, lambda m: m.group(1).rstrip("\n") + "\n" + note, content, flags=re.DOTALL)
        changed = True

    if changed:
        _write_with_cap(TICKETS_FILE, content)


def _write_with_cap(file_path: Path, content: str) -> None:
    """Write file with size cap, archive if exceeded."""
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_FILE_SIZE:
        # Archive current content before truncation
        archive_name = f"{file_path.stem}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
        (ARCHIVE_DIR / archive_name).write_bytes(content_bytes)
        # Truncate to last 80% of content (keep most recent entries)
        keep_bytes = int(MAX_FILE_SIZE * 0.8)
        # Find a safe cut point (after a separator)
        truncated = content_bytes[-keep_bytes:]
        # Find first --- separator
        sep_idx = truncated.find(b"\n---\n")
        if sep_idx > 0:
            truncated = truncated[sep_idx + 5:]
        content = truncated.decode("utf-8", errors="ignore")

    file_path.write_text(content, encoding="utf-8")


def _rotate_archives() -> None:
    """Remove archive files older than 90 days."""
    if not ARCHIVE_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - (90 * 86400)
    for archive in ARCHIVE_DIR.glob("*.md"):
        try:
            if archive.stat().st_mtime < cutoff:
                archive.unlink()
        except Exception:
            pass
