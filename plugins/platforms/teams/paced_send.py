"""Pacing helper for sending several autonomous messages in a row.

The 2026-07-27 outage was 32 card deliveries inside 650ms, which tripped
Teams' 429 rate limit and (before adapter.py's retry fix) silently degraded
every one of them to a text code block. Splitting one autonomous message
into N (see ``ticket_card.split_tickets_to_messages``) makes that worse
unless the sends themselves are paced.

Full cron-loop wiring (finding the exact call site that iterates ticket
sends for a sweep) is a follow-up; this module provides the primitive and is
covered by tests directly.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, List

# Empirically well under Teams' per-chat rate limit for a handful of cards;
# small enough that a 5-ticket sweep still lands in a few seconds.
DEFAULT_AUTONOMOUS_SEND_DELAY_S = 0.75


async def send_paced(
    send_fn: Callable[[str], Awaitable[Any]],
    messages: List[str],
    *,
    delay_s: float = DEFAULT_AUTONOMOUS_SEND_DELAY_S,
) -> List[Any]:
    """Call ``send_fn`` once per message, sleeping ``delay_s`` between sends.

    No delay before the first send. Returns the list of ``send_fn`` results
    in order, so callers can inspect per-message success/failure.
    """
    results: List[Any] = []
    for index, message in enumerate(messages):
        if index > 0:
            await asyncio.sleep(delay_s)
        results.append(await send_fn(message))
    return results
