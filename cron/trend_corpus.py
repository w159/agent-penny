#!/usr/bin/env python3
"""
Deterministic ticket digest layer for Agent Penny's trend detection.

trend_detection.py already clusters on ticket SUMMARY tokens. Real-data
analysis of the CW corpus found that summary text is the wrong signal:
end users describe the same underlying fault in completely unrelated
words ("bitlocker", "blue screen asking for a recovery key", "stuck in
automatic repair", "computer keeps powering off"). The linking evidence
is almost never in the summary - it is in the TECHNICIAN'S TIME ENTRY
NOTE, e.g. "Tried running a startup repair and uninstalling the latest
quality update" (ticket 94792). This module turns raw CW tickets, notes,
and time entries into one clean TicketDigest per ticket, with time entry
notes and resolution notes promoted to first-class inputs, so a later
clustering stage has real signal to group on instead of guessing from
summaries alone.

Kept 100% deterministic and stdlib-only on purpose: no model call, no
embeddings. A later stage may narrate what this module finds, but every
digest field here is reproducible from the same CW data every time.

build_digests() is pure and takes plain dicts (dependency injection) so
it is testable without network. load_corpus() is the only function that
talks to ConnectWise, via the existing CWClient in cron/cw_client.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from cron.cw_configurations import normalize_configurations
from cron.trend_entities import extract_entities

# Board names that are 100% machine-generated, confirmed against the real
# 14-day corpus (Security Operations Center 535 tickets, Network
# Operations Center 356). Board alone is not sufficient - Triage and
# Incident Reporting are human boards but can still carry an automated
# alert forwarded by a monitoring tool - so this is combined with a
# content-marker check in _is_automated() below.
_AUTOMATED_BOARDS = {"security operations center", "network operations center"}

# Phrases that show up in machine-generated alert bodies but essentially
# never in a human's own words, used as the content half of the
# is_automated check.
_AUTOMATED_MARKERS = (
    "failed to complete", "patch management", "monitoring alert",
    "auto-generated", "automatically generated", "threat detected",
    "policy violation detected", "backup job", "sensor offline",
)

IMG = re.compile(r'!\[[^\]]*\]\([^)]*\)|!\[\\\[[^\]]*\\\]\]\([^)]*\)')
LINK = re.compile(r'\[([^\]]*)\]\([^)]*\)')
SIG_MARKERS = re.compile(
    r'(?im)^\s*(?:--+\s*$|from:\s|sent:\s|to:\s|subject:\s|cc:\s'
    r'|henssler financial\s*$|3735 cherokee|committed to:|this (?:e-?mail|message) '
    r'|confidentiality notice|direct:\s*\[?\d|main:\s*\d{3}|fax:\s*\d{3}'
    r'|web:\s*\[?www\.|sent from my |get outlook for )')

# GWH-xxxx, HPM-xxxx, NAP-xxxx, THFG-xxxx, HF-xxxx hostnames. Confirmed to
# find 74 distinct devices in the real 14-day window.
DEVICE_RE = re.compile(r'\b(?:GWH|HPM|NAP|THFG|HF)-[A-Z0-9]{3,14}\b', re.IGNORECASE)

_EMPHASIS_RE = re.compile(r'[*_`#]+')
_WHITESPACE_RE = re.compile(r'\s+')


@dataclass
class TicketDigest:
    id: int
    date: str
    board: str
    summary: str
    contact: str
    status: str
    priority: str
    issue: str
    resolution: str
    tech_notes: list = field(default_factory=list)
    techs: list = field(default_factory=list)
    devices: list = field(default_factory=list)
    entities: set = field(default_factory=set)
    is_automated: bool = False
    # Aliases of issue/resolution/tech_notes under the names a later
    # clustering stage expects, plus configurations - a CORROBORATING
    # signal only. All existing fields above are untouched so current
    # consumers (trend_cluster_embed.py) keep working unmodified.
    issue_text: str = ""
    resolution_text: str = ""
    time_entry_text: str = ""
    configurations: list = field(default_factory=list)


def clean_note(text: str, limit: int = 900) -> str:
    """
    Strip email/markdown noise from a raw CW note body.

    Order matters: images go first (they carry no label worth keeping),
    then links unwrap to their label text, then a signature/quoted-reply
    marker cuts everything after it - UNLESS that marker sits at or
    before character 60, which means the whole note is a forward and
    should be kept rather than truncated to nothing. Must never raise on
    malformed input (None, non-string, empty) and must be idempotent -
    re-running it on already-cleaned text is a no-op.
    """
    if not text or not isinstance(text, str):
        return ""

    cleaned = IMG.sub("", text)
    cleaned = LINK.sub(r"\1", cleaned)

    match = SIG_MARKERS.search(cleaned)
    if match and match.start() > 60:
        cleaned = cleaned[: match.start()]

    cleaned = _EMPHASIS_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:limit]


def extract_devices(*texts: str) -> list:
    """Pull distinct hostnames (GWH-/HPM-/NAP-/THFG-/HF- prefixed) out of any number of text fields."""
    found = set()
    for text in texts:
        if not text or not isinstance(text, str):
            continue
        found.update(m.upper() for m in DEVICE_RE.findall(text))
    return sorted(found)


def _is_automated(board: str, *texts: str) -> bool:
    """
    True for RMM/SOC/NOC machine-generated tickets.

    Board name alone over-fires (a human can file into an automated
    board via forward) and under-fires (a monitoring tool can post into
    a human board), so this checks board membership OR a content marker,
    not board membership alone.
    """
    if board.strip().lower() in _AUTOMATED_BOARDS:
        return True
    combined = " ".join(t for t in texts if t).lower()
    return any(marker in combined for marker in _AUTOMATED_MARKERS)


def _issue_and_resolution(notes: list) -> tuple[str, str]:
    """
    Pick the issue note (detailDescriptionFlag) and resolution note
    (resolutionFlag, else the last note that isn't the issue note itself)
    out of a ticket's raw note list. Missing/malformed notes are skipped,
    never raise.
    """
    issue = ""
    resolution = ""
    last_non_issue = ""
    for note in notes or []:
        if not isinstance(note, dict):
            continue
        text = clean_note(note.get("text"))
        if not text:
            continue
        if note.get("detailDescriptionFlag") and not issue:
            issue = text
            continue
        if note.get("resolutionFlag"):
            resolution = text
        last_non_issue = text
    return issue, resolution or last_non_issue


def _tech_notes_and_techs(entries: list) -> tuple[list, list]:
    """
    Cleaned time entry notes (newest first) and the distinct member names
    behind them, for one ticket's time entries. Entries are assumed
    already newest-first-sorted by the caller (CW returns them by id
    order); this just cleans and filters blanks.
    """
    notes = []
    techs = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        note = clean_note(entry.get("notes"))
        if note:
            notes.append(note)
        member = (entry.get("member") or {}).get("name")
        if member and member not in techs:
            techs.append(member)
    return notes, techs


def _dedup_join(texts: list) -> str:
    """Join cleaned texts with a space, dropping exact duplicates but keeping order."""
    seen: set = set()
    parts = []
    for text in texts:
        if text and text not in seen:
            seen.add(text)
            parts.append(text)
    return " ".join(parts)


def build_digests(
    tickets: list,
    notes_by_id: dict,
    time_entries: list,
    configurations_by_id: dict | None = None,
) -> list:
    """
    Pure transform: raw CW ticket/note/time-entry/configuration dicts ->
    TicketDigest list. No I/O. Time entries are matched to tickets via
    chargeToId, ignoring anything whose chargeToType isn't ServiceTicket
    (time can be charged to a project or an activity, not just a
    ticket). configurations_by_id defaults to empty per ticket - a
    ticket with no attached configurations is the normal
    device-diverse-trend case, not a malformed digest.
    """
    configurations_by_id = configurations_by_id or {}
    entries_by_ticket: dict = {}
    for entry in time_entries or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("chargeToType") != "ServiceTicket":
            continue
        ticket_id = entry.get("chargeToId")
        if ticket_id is None:
            continue
        entries_by_ticket.setdefault(ticket_id, []).append(entry)

    digests = []
    for ticket in tickets or []:
        if not isinstance(ticket, dict):
            continue
        tid = ticket.get("id")
        if tid is None:
            continue

        info = ticket.get("_info") or {}
        summary = clean_note(ticket.get("summary") or "", limit=300)
        notes = notes_by_id.get(tid, []) if notes_by_id else []
        issue, resolution = _issue_and_resolution(notes)
        tech_notes, techs = _tech_notes_and_techs(entries_by_ticket.get(tid, []))
        tech_notes.reverse()  # entries arrive oldest-first from CW; digest wants newest-first

        devices = extract_devices(summary, issue, resolution, *tech_notes)
        board = (ticket.get("board") or {}).get("name") or ""
        entities = extract_entities(summary, issue, resolution, *tech_notes, devices=devices)
        configurations = normalize_configurations(configurations_by_id.get(tid, []))

        digests.append(
            TicketDigest(
                id=tid,
                date=info.get("dateEntered") or "",
                board=board,
                summary=summary,
                contact=ticket.get("contactName") or "",
                status=(ticket.get("status") or {}).get("name") or "",
                priority=(ticket.get("priority") or {}).get("name") or "",
                issue=issue,
                resolution=resolution,
                tech_notes=tech_notes,
                techs=techs,
                devices=devices,
                entities=entities,
                is_automated=_is_automated(board, summary, issue, resolution, *tech_notes),
                issue_text=issue,
                resolution_text=resolution,
                time_entry_text=_dedup_join(tech_notes),
                configurations=configurations,
            )
        )
    return digests


# load_corpus() lives in trend_corpus_loader.py (the only I/O in this
# feature) to keep this file under the house 300-line cap - re-exported
# here so `from cron.trend_corpus import load_corpus` (trend_pass.py,
# tests) keeps working unmodified.
from cron.trend_corpus_loader import load_corpus  # noqa: E402,F401
