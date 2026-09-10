#!/usr/bin/env python3
"""
Prompt construction and response parsing for cron/trend_cluster_semantic.py's
Stage B narration call.

Split out to keep trend_cluster_semantic.py under the house 300-line cap and
because these are pure functions (digests in, text or parsed dicts out) with
no model-calling or fallback-orchestration concerns of their own - those stay
in trend_cluster_semantic.py.

The merge decision itself (formerly "Stage A" here, an LLM prompt shown
every candidate at once) now lives in cron/trend_cluster_embed.py as
deterministic embedding + cosine similarity - it needs no prompt at all, so
this module is Stage B only: narrating one already-merged group per call.
"""
from __future__ import annotations

import json
import re

from cron.trend_cluster_output import best_evidence as _best_evidence

# ---------------------------------------------------------------------------
# Stage B: narration. One prompt per merged group.
# ---------------------------------------------------------------------------

STAGE_B_MAX_PROMPT_CHARS = 12000
STAGE_B_EVIDENCE_CHARS_PER_TICKET = 200
STAGE_B_SUMMARY_CHARS_PER_TICKET = 120
STAGE_B_MAX_TICKETS_SHOWN = 12

STAGE_B_SYSTEM_PROMPT = (
    "You are narrating one MERGED ticket group for a managed service "
    "provider. The grouping decision is already made and is not yours to "
    "change. Write a short title, a one or two sentence why_related "
    "explaining the shared root cause, and a one sentence "
    "recommended_action. Do not invent or report any ticket count, device "
    "count, or user count - those are computed separately and any number "
    "you write is ignored. Respond with strict JSON only, no markdown "
    "fence, no prose before or after, matching exactly: "
    '{"title": "...", "why_related": "...", "recommended_action": "..."}'
)

STRICT_JSON_SUFFIX = (
    "\n\nYour previous response could not be parsed as JSON. Respond with "
    "ONLY the JSON object this time - no markdown fence, no explanation "
    "before or after it."
)


def _diagnostic_score(note: str) -> int:
    """Ranks a ticket's own evidence lines so the most useful one is
    picked first - see trend_cluster_output.py's copy for the reasoning."""
    return sum(c.isdigit() for c in note) + note.count(":") * 2


def _clean_str(value) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _parse_json_object(raw):
    """Tolerates a markdown fence around the JSON and trailing/leading
    prose. Returns None (never raises) on anything unparseable. Shared by
    both stages - the model's fencing habits don't vary by stage."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def build_stage_b_prompt(digests: list, label: str) -> str:
    """One merged group's fuller-detail prompt. Budget is per-group here,
    not per-corpus, so truncation pressure is far lower than Stage A's."""
    rank = lambda d: _diagnostic_score(_best_evidence(d))  # noqa: E731
    shown = sorted(digests, key=rank, reverse=True)[:STAGE_B_MAX_TICKETS_SHOWN]
    omitted = len(digests) - len(shown)
    lines = [f'Merged group "{label}" ({len(digests)} tickets total):']
    for d in sorted(shown, key=lambda x: x.id):
        summary = (d.summary or "")[:STAGE_B_SUMMARY_CHARS_PER_TICKET]
        evidence = _best_evidence(d)[:STAGE_B_EVIDENCE_CHARS_PER_TICKET]
        lines.append(f"  #{d.id} {d.date} summary={summary!r} evidence={evidence!r}")
    if omitted > 0:
        lines.append(f"  ...({omitted} more ticket(s) in this group, omitted for brevity)")
    return "\n".join(lines)[:STAGE_B_MAX_PROMPT_CHARS]


def parse_stage_b_narration(raw):
    """Returns {"title", "why_related", "recommended_action"} or None if
    unusable. A blank/missing title makes the whole narration unusable -
    the caller falls back to a deterministic title for this group rather
    than shipping an empty one."""
    parsed = _parse_json_object(raw)
    if not isinstance(parsed, dict):
        return None
    title = _clean_str(parsed.get("title"))
    if not title:
        return None
    return {
        "title": title,
        "why_related": _clean_str(parsed.get("why_related")),
        "recommended_action": _clean_str(parsed.get("recommended_action")),
    }
