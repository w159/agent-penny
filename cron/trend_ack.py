#!/usr/bin/env python3
"""
Human-acknowledgment lookup for cron/trend_escalation.py, split into its
own module purely to keep trend_escalation.py under the house 300-line
file cap -- this is one cohesive concern (query state.db for a real Teams
reply) with no reason to live inline with the ladder arithmetic.

Read-only against /home/yoda/.hermes/state.db. The load-bearing detail:
`messages.role='user'` covers BOTH real Teams replies and synthetic
cron/webhook prompts fed to the model, so every query here joins to
`sessions.source = 'teams'` -- see cron/trend_escalation.py's module
docstring for the full story on why that join must never be dropped.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_STATE_DB = get_hermes_home() / "state.db"

# A human reply naming the trend_id counts as an ack no matter how late it
# arrives. A reply that does NOT name it only counts if it lands within
# this window of the raise -- otherwise an unrelated message hours later
# would be misread as an answer to a trend nobody mentioned.
ACKNOWLEDGMENT_WINDOW_MINUTES = 90


def detect_acknowledgment(
    trend_id: str, since_ts: float, chat_id: str, *, db_path: Optional[Path] = None
) -> tuple:
    """Has a human answered this trend in `chat_id` since `since_ts`?

    Any DB error (missing file, locked file, unexpected schema) degrades
    to "no ack found" rather than raising, since a probe failure here
    must never be mistaken for silence from the trend's owner.
    """
    path = db_path or _STATE_DB
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.warning("trend_ack: could not open state.db read-only (%s)", e)
        return None, None

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.content, m.timestamp, s.user_id
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE s.source = 'teams'
              AND m.role = 'user'
              AND s.chat_id = ?
              AND m.timestamp > ?
            ORDER BY m.timestamp ASC
            """,
            (chat_id, since_ts),
        )
        rows = cur.fetchall()
    except sqlite3.Error as e:
        logger.warning("trend_ack: ack query failed (%s)", e)
        return None, None
    finally:
        conn.close()

    window_seconds = ACKNOWLEDGMENT_WINDOW_MINUTES * 60
    for content, ts, user_id in rows:
        content = content or ""
        if trend_id in content or (ts - since_ts) <= window_seconds:
            when = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            return when, user_id
    return None, None
