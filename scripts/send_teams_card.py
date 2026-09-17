"""Post one Python-built Teams ticket card for a ticket dict - operator smoke tool.

The webhook lane builds every card in code (plugins.platforms.teams.ticket_card)
and ships it through TeamsAdapter's card send path. This script exercises that
same pipeline by hand: feed it a ticket dict as JSON on stdin (or --file), and
it renders the card. Without --post it prints the fenced card it would send,
which doubles as an import-time regression check for the card builder. With
--post CHAT_ID it connects a TeamsAdapter (credentials from the environment:
TEAMS_CLIENT_ID / TEAMS_CLIENT_SECRET / TEAMS_TENANT_ID) and posts the card.

Usage:
    echo '{"ticket_id": 94822, "summary": "...", ...}' | uv run scripts/send_teams_card.py
    uv run scripts/send_teams_card.py --file ticket.json --post 19:abc@thread.v2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _validate_ticket(ticket) -> dict:
    if not isinstance(ticket, dict):
        raise SystemExit("ticket JSON must be an object, got %s" % type(ticket).__name__)
    return ticket


def _load_ticket(path: str | None) -> dict:
    raw = sys.stdin.read() if path is None else open(path, encoding="utf-8").read()
    return _validate_ticket(json.loads(raw))


def _build(ticket: dict) -> str:
    from plugins.platforms.teams.ticket_card import build_ticket_card, render_card_fence

    return render_card_fence(build_ticket_card(ticket))


async def _post(card_markdown: str, chat_id: str) -> None:
    """Send the pre-rendered fence through TeamsAdapter's own card path."""
    from gateway.config import PlatformConfig
    from plugins.platforms.teams.adapter import TeamsAdapter

    adapter = TeamsAdapter(PlatformConfig(enabled=True, extra={}))
    if not await adapter.connect():
        raise SystemExit("Teams adapter failed to connect (credentials? SDK?)")
    try:
        result = await adapter.send(chat_id, card_markdown)
    finally:
        await adapter.disconnect()
    if not result.success:
        raise SystemExit("Teams send failed: %s" % result.error)
    print("card posted to %s" % chat_id, file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", help="ticket JSON file (default: stdin)")
    parser.add_argument("--post", metavar="CHAT_ID",
                        help="post the card to this Teams conversation instead of printing it")
    args = parser.parse_args()

    card_markdown = _build(_load_ticket(args.file))
    if args.post:
        asyncio.run(_post(card_markdown, args.post))
    else:
        print(card_markdown)


if __name__ == "__main__":
    main()
