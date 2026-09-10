#!/usr/bin/env python3
"""
Durable behavior store for Agent Penny -- lets IT teammates teach her
knobs and instructions without her prompt growing without bound.

Storage grows without limit (SQLite, one row per proposal/approval/
supersession, forever). What reaches a prompt does NOT: render_active_rules()
renders only active rules, hard-capped in characters, so a six-month history
of learning stays bounded at the point it is actually read back.

Commit model: propose() writes a PENDING row that never renders. Only
approve() by an authorized human makes a rule active. This is deliberate --
a pending proposal must never influence behavior, so ticket text or chat
prose cannot write its own instructions. reject() and retire() close out a
proposal or an active rule without ever letting it render.

Writing a knob rule for a key that already has an active rule retires the
old one in the same transaction (supersession) -- this is the mechanism
that keeps the active set, and therefore the rendered prompt, bounded even
as the audit trail keeps growing.

Schema/connection lifecycle lives in cron/behavior_db.py (kept separate to
stay under the house 300-line file cap). Knob schema/validation lives in
cron/behavior_knobs.py. See cron/ops_memory.py and cron/trend_state.py for
the house conventions this follows (memories/ops/ layout, atomic writes).
"""
from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cron.behavior_db import DB_PATH, authorized_approvers, connect
from cron.behavior_knobs import validate_knob

__all__ = [
    "DB_PATH", "propose", "approve", "reject", "retire",
    "render_active_rules", "count_active", "history", "validate_knob",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _audit(conn, rule_id: int, event: str, by_user: Optional[str], reason: Optional[str], now: str) -> None:
    conn.execute(
        "INSERT INTO audit (rule_id, event, by_user, reason, at) VALUES (?, ?, ?, ?, ?)",
        (rule_id, event, by_user, reason, now),
    )


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("value") is not None:
        try:
            d["value"] = json.loads(d["value"])
        except (TypeError, json.JSONDecodeError):
            pass
    return d


def propose(
    kind: str,
    text: str,
    *,
    scope: str,
    key: Optional[str] = None,
    value=None,
    requested_by: str,
    source_chat_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    now: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Store a PENDING proposal. Never renders until approve()."""
    if kind not in ("knob", "instruction"):
        raise ValueError(f"kind must be 'knob' or 'instruction', got {kind!r}")
    if not text or not text.strip():
        raise ValueError("text is required")
    now = now or _now_iso()

    if kind == "knob":
        if not key:
            raise ValueError("knob proposals require key")
        ok, msg = validate_knob(key, value)
        if not ok:
            raise ValueError(msg)
        value_json = json.dumps(value)
    else:
        value_json = None
        with contextlib.closing(connect(db_path)) as conn:
            existing = conn.execute(
                "SELECT id, text FROM rules WHERE kind='instruction' AND active=1"
            ).fetchall()
            normalized = _normalize_text(text)
            for row in existing:
                if _normalize_text(row["text"]) == normalized:
                    return _row_to_dict(
                        conn.execute("SELECT * FROM rules WHERE id=?", (row["id"],)).fetchone()
                    )

    with contextlib.closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                INSERT INTO rules (kind, scope, key, value, text, status, requested_by,
                                    source_chat_id, source_message_id, created_at, active)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, 0)
                """,
                (kind, scope, key, value_json, text.strip(), requested_by,
                 source_chat_id, source_message_id, now),
            )
            rule_id = cur.lastrowid
            _audit(conn, rule_id, "proposed", requested_by, None, now)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return _row_to_dict(conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone())


def approve(proposal_id: int, *, approved_by: str, now: Optional[str] = None, db_path: Optional[Path] = None) -> dict:
    """Activate a pending proposal. Only an authorized approver may do this.

    For a knob, retires any prior active rule with the same key in the same
    transaction -- exactly one active rule per key survives.
    """
    if approved_by not in authorized_approvers():
        raise PermissionError(f"{approved_by!r} is not authorized to approve behavior rules")
    now = now or _now_iso()

    with contextlib.closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM rules WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise ValueError(f"no rule with id {proposal_id}")
            if row["status"] != "pending":
                raise ValueError(f"rule {proposal_id} is {row['status']!r}, not pending")

            if row["kind"] == "knob" and row["key"]:
                prior = conn.execute(
                    "SELECT id FROM rules WHERE kind='knob' AND key=? AND active=1 AND id != ?",
                    (row["key"], proposal_id),
                ).fetchall()
                for p in prior:
                    conn.execute(
                        "UPDATE rules SET active=0, status='retired', superseded_by=?, retired_at=? WHERE id=?",
                        (proposal_id, now, p["id"]),
                    )
                    _audit(conn, p["id"], "superseded", approved_by, f"superseded by {proposal_id}", now)

            conn.execute(
                "UPDATE rules SET status='active', active=1, approved_by=? WHERE id=?",
                (approved_by, proposal_id),
            )
            _audit(conn, proposal_id, "approved", approved_by, None, now)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return _row_to_dict(conn.execute("SELECT * FROM rules WHERE id=?", (proposal_id,)).fetchone())


def reject(proposal_id: int, *, rejected_by: str, reason: str, now: Optional[str] = None, db_path: Optional[Path] = None) -> None:
    """Close out a pending proposal without ever letting it render."""
    now = now or _now_iso()
    with contextlib.closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT status FROM rules WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise ValueError(f"no rule with id {proposal_id}")
            if row["status"] != "pending":
                raise ValueError(f"rule {proposal_id} is {row['status']!r}, not pending")
            conn.execute("UPDATE rules SET status='rejected', active=0 WHERE id=?", (proposal_id,))
            _audit(conn, proposal_id, "rejected", rejected_by, reason, now)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def retire(rule_id: int, *, by: str, reason: str, now: Optional[str] = None, db_path: Optional[Path] = None) -> None:
    """Retire an active rule. Requires the same authorization as approve()."""
    if by not in authorized_approvers():
        raise PermissionError(f"{by!r} is not authorized to retire behavior rules")
    now = now or _now_iso()
    with contextlib.closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT status FROM rules WHERE id=?", (rule_id,)).fetchone()
            if row is None:
                raise ValueError(f"no rule with id {rule_id}")
            if row["status"] != "active":
                raise ValueError(f"rule {rule_id} is {row['status']!r}, not active")
            conn.execute(
                "UPDATE rules SET status='retired', active=0, retired_at=? WHERE id=?",
                (now, rule_id),
            )
            _audit(conn, rule_id, "retired", by, reason, now)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def count_active(scope: Optional[str] = None, db_path: Optional[Path] = None) -> int:
    with contextlib.closing(connect(db_path)) as conn:
        if scope:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM rules WHERE active=1 AND scope=?", (scope,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM rules WHERE active=1").fetchone()
        return row["n"]


def render_active_rules(*, max_chars: int = 2000, scope: Optional[str] = None, db_path: Optional[Path] = None) -> str:
    """Render only active rules, newest first, hard-capped at max_chars.

    Knobs render as `key = value`; instructions render as sentences. When
    the cap truncates, the last line always names how many rules were
    omitted and how to see them -- truncation is never silent.
    """
    with contextlib.closing(connect(db_path)) as conn:
        if scope:
            rows = conn.execute(
                "SELECT * FROM rules WHERE active=1 AND scope=? ORDER BY id DESC", (scope,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM rules WHERE active=1 ORDER BY id DESC").fetchall()

    total = len(rows)
    rendered_lines: list[str] = []
    for row in rows:
        if row["kind"] == "knob":
            try:
                value = json.loads(row["value"]) if row["value"] is not None else None
            except json.JSONDecodeError:
                value = row["value"]
            line = f"- {row['key']} = {value}"
        else:
            line = f"- {row['text']}"
        rendered_lines.append(line)

    if len("\n".join(rendered_lines)) <= max_chars:
        return "\n".join(rendered_lines)

    # Drop lines from the tail until the kept lines plus the omission
    # notice both fit inside max_chars. The notice is recomputed each time
    # since the omitted count (and therefore its own length) changes.
    kept = rendered_lines
    while kept:
        omitted = total - len(kept)
        notice = f"... {omitted} more rule(s) omitted; call history() or widen max_chars to see them"
        candidate = "\n".join(kept + [notice])
        if len(candidate) <= max_chars:
            return candidate
        kept = kept[:-1]

    # max_chars too small even for one omission notice naming everything.
    return f"... {total} more rule(s) omitted; call history() or widen max_chars to see them"[:max_chars]


def history(key: Optional[str] = None, limit: int = 50, db_path: Optional[Path] = None) -> list[dict]:
    """Return rules (any status) newest-first, so a retired/superseded rule
    is still visible for audit purposes after supersession."""
    with contextlib.closing(connect(db_path)) as conn:
        if key:
            rows = conn.execute(
                "SELECT * FROM rules WHERE key=? ORDER BY id DESC LIMIT ?", (key, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM rules ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]
