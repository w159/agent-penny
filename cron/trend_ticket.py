#!/usr/bin/env python3
"""
Level-3 ConnectWise ticket creation for cron/trend_escalation.py, split
into its own module purely to keep trend_escalation.py under the house
300-line file cap -- this is a self-contained CW payload-building concern
with no reason to live inline with the ladder's state arithmetic.

Creating this ticket IS the owner's requested final rung: "even
potentially escalating to creating a ticket for it if nobody acknowledges
your recommendations." cron/trend_escalation.py owns deciding WHEN this
fires (once, on the trend that reached level 3); this module only owns
HOW the ticket is built once that decision has been made.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home
from cron.trend_entities import extract_entities

logger = logging.getLogger(__name__)

# Company 250 = "Henssler Financial" (verified live). Board name matches
# the Triage board this deployment's CW client is scoped to.
ESCALATION_BOARD_NAME = "Triage"
ESCALATION_COMPANY_ID = 250

# Ledger lives next to trend_alert_state.json (see cron/trend_state.py's
# OPS_DIR) so both files share the same crash-safe atomic-write pattern
# and the same directory an operator would look in for ops state.
LEDGER_FILE = get_hermes_home() / "memories" / "ops" / "trend_ticket_ledger.json"

# v1 fingerprinted trend["summary"] tokens (LLM prose, reworded run to run
# -> false new fingerprints). v2 switched to the founding CW ticket id: only
# stable within a single rolling window. cron.trend.window_days (see
# cron/trend_pass.py) is 21 days, so a trend outliving that window has its
# founding ticket age out the back, the cluster re-forms around a new
# earliest id, and v2 mints a fresh fingerprint -- and a duplicate CW
# ticket -- for a trend that never stopped. v3 fingerprints the trend's
# entity/symptom signature instead (see _entity_signature()), which
# survives both LLM rewording and a window slide. Bumped so load_ledger()
# can flag pre-v3 entries instead of silently treating them as equivalent
# to entries keyed the new way.
FINGERPRINT_SCHEME_VERSION = 3

# Most common normalized entity/symptom tokens kept in the signature. See
# _entity_signature()'s docstring for the tradeoff this size encodes.
_SIGNATURE_ENTITY_COUNT = 5


def _entity_signature(trend: dict) -> list[str]:
    """The trend's stable identifying core: its top-N most common normalized
    entity/symptom tokens (cron.trend_entities.extract_entities), not its
    founding ticket ids and not trend["title"]/["why_related"] (Stage B
    LLM prose, trend_cluster_semantic.py -- reworded run to run for the
    same ongoing trend).

    Extracted from each member ticket's own "summary"/"evidence" text --
    finalize_trend()'s real "tickets": [{"summary":..., "evidence":...}]
    shape (cron/trend_cluster_output.py:97-98). That text is the raw CW
    ticket summary and the technician's own note/resolution line, never
    LLM-authored, so it can't drift the way the trend's title does.

    Full-set equality would still break under ordinary churn: as tickets
    age out of the rolling 21-day window (cron.trend.window_days) and new
    ones join, a rare one-off entity can appear or vanish without the
    trend's actual identity changing. Top-N (ranked by frequency, ties
    broken alphabetically for determinism) tolerates that: the dominant
    symptom(s) have to persist, but a minor entity at the edges coming or
    going doesn't flip the fingerprint. N=5 gives a little more headroom
    than the N=3 already used for trend titling
    (trend_cluster_semantic._fallback_title), since a fingerprint
    collision here is cheap to fix (a human merges two CW tickets) while a
    false split silently reopens a duplicate ticket for a trend that never
    stopped. N=5 will NOT absorb a trend whose dominant symptom itself
    changes -- see test_different_trend_produces_different_fingerprint.
    """
    counts: dict[str, int] = {}
    for t in trend.get("tickets", []):
        if not isinstance(t, dict):
            continue
        for text in (t.get("summary"), t.get("evidence")):
            if not text:
                continue
            for entity in extract_entities(text):
                counts[entity] = counts.get(entity, 0) + 1

    ranked = sorted(counts, key=lambda e: (-counts[e], e))
    return ranked[:_SIGNATURE_ENTITY_COUNT]


def trend_fingerprint(trend: dict) -> str:
    """Deterministic key identifying a trend across daily runs.

    Built from the trend's entity/symptom signature (see
    _entity_signature()), NOT from trend["summary"]/["title"] (LLM-authored
    prose that gets reworded run to run) and NOT from founding ticket ids
    (only stable within one rolling 21-day window -- see
    FINGERPRINT_SCHEME_VERSION's comment for why that broke v2).
    ticket_count and device_count are excluded for the same reason they
    always were: they grow as more tickets join an ongoing trend, and
    hashing a growing count would mint a new fingerprint every day right
    along with it. The entity/symptom core doesn't have that problem -- an
    ongoing trend's dominant symptom is what makes it that trend.

    Tradeoff: top-N entity matching is tighter than fuzzy summary matching
    but looser than an exact ticket id. Two distinct real trends sharing
    the same top-5 normalized entities (e.g. two separate "printer"
    trends on different floors) would collide and land on one CW ticket --
    a nuisance a human merges. The alternative failure mode -- keying on
    something that drifts under ordinary window churn -- silently opens a
    duplicate ticket for a trend that's still open, which is the exact
    defect this function exists to prevent. See ensure_trend_ticket()'s
    docstring for the same duplicate-over-silent-loss tradeoff made for
    the ledger write ordering.
    """
    signature = _entity_signature(trend)
    payload = json.dumps({"entity_signature": signature}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_ledger(path: Optional[Path] = None) -> dict:
    """Read trend_ticket_ledger.json. Missing or corrupt file -> empty ledger.

    Entries written under an older fingerprint scheme (see
    FINGERPRINT_SCHEME_VERSION) -- v1 keyed on hashed summary tokens, v2 on
    the founding CW ticket id -- can never be reproduced by the current
    scheme. Rather than silently treat those keys as dead weight, flag
    them loudly: each one will look "new" to ensure_trend_ticket() on its
    next run and mint one (bounded, one-time) duplicate ticket for a trend
    that already has one. That's the same acceptable-nuisance tradeoff
    documented in trend_fingerprint() -- surfaced here so an operator can
    go merge those duplicates instead of discovering them cold.
    """
    p = path or LEDGER_FILE
    if not p.exists():
        return {}
    try:
        ledger = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("trend_ticket: ledger unreadable (%s), starting fresh", e)
        return {}

    legacy = [
        fp for fp, entry in ledger.items()
        if isinstance(entry, dict) and entry.get("fingerprint_scheme_version") != FINGERPRINT_SCHEME_VERSION
    ]
    if legacy:
        logger.warning(
            "trend_ticket: %d ledger entr%s predate fingerprint scheme v%d and will not "
            "match the current entity-signature-based fingerprint; expect one duplicate "
            "CW ticket per entry on next re-detection: %s",
            len(legacy), "y" if len(legacy) == 1 else "ies", FINGERPRINT_SCHEME_VERSION, legacy,
        )
    return ledger


def save_ledger(ledger: dict, path: Optional[Path] = None) -> None:
    """Write the ledger atomically (temp file + rename), matching
    cron/trend_state.py's save_state pattern -- a crash mid-write must
    never leave a truncated, unparseable ledger behind."""
    p = path or LEDGER_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def ensure_trend_ticket(
    trend: dict,
    cw_client,
    *,
    dry_run: bool = False,
    ledger_path: Optional[Path] = None,
) -> dict:
    """Create the ConnectWise ticket for a trend at most once, ever.

    Crash-ordering tradeoff: the ledger is written AFTER create_ticket()
    returns, never before. A crash between the API call succeeding and
    the ledger write landing risks one duplicate ticket on the next run
    (the fingerprint isn't recorded yet, so the trend looks new again).
    The alternative -- writing the ledger first -- risks the opposite: a
    crash after the ledger write but before/during the API call would
    permanently mark the trend as ticketed with NO ticket ever created,
    silently swallowing a real alert forever. An occasional duplicate
    ticket is a nuisance a human can merge; a silently dropped alert is
    the exact failure this whole feature exists to prevent. So "write
    ledger after a confirmed success" is the smaller risk.
    """
    fingerprint = trend_fingerprint(trend)
    ledger = load_ledger(ledger_path)
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = ledger.get(fingerprint)
    if existing is not None:
        if not dry_run:
            existing["last_seen_iso"] = now_iso
            save_ledger(ledger, ledger_path)
        return {
            "created": False,
            "already_existed": True,
            "dry_run": dry_run,
            "ticket_id": existing.get("connectwise_ticket_id"),
            "fingerprint": fingerprint,
        }

    if dry_run:
        return {
            "created": False,
            "already_existed": False,
            "dry_run": True,
            "ticket_id": None,
            "fingerprint": fingerprint,
        }

    ticket_ids = trend.get("ticket_ids", [])
    description_lines = [
        trend.get(
            "summary",
            "Automated trend alert: no human acknowledgment after the escalation ladder was exhausted.",
        ),
        f"Ticket count: {trend.get('ticket_count', 0)}",
        f"Device count: {trend.get('device_count', 0)}",
    ]
    if ticket_ids:
        description_lines.append("Related tickets: " + ", ".join(f"#{t}" for t in ticket_ids))

    # No try/except around create_ticket: an API failure must propagate
    # loudly (fail fast) rather than be absorbed here, and skipping the
    # ledger write on the exception path is what keeps the ledger from
    # being polluted with a fingerprint for a ticket that was never made.
    result = cw_client.create_ticket(
        summary=f"Unacknowledged trend alert: {trend.get('trend_id')}",
        initial_description="\n".join(description_lines),
        board_name=ESCALATION_BOARD_NAME,
        company_id=ESCALATION_COMPANY_ID,
        priority_name="Priority 2 - High",
    )
    ticket_id = result.get("id")

    ledger[fingerprint] = {
        "connectwise_ticket_id": ticket_id,
        "created_at_iso": now_iso,
        "last_seen_iso": now_iso,
        "fingerprint_scheme_version": FINGERPRINT_SCHEME_VERSION,
    }
    save_ledger(ledger, ledger_path)

    return {
        "created": True,
        "already_existed": False,
        "dry_run": False,
        "ticket_id": ticket_id,
        "fingerprint": fingerprint,
    }


def escalate_to_ticket(trend: dict, *, client, dry_run: bool = True) -> dict:
    """Create the level-3 ConnectWise ticket for an unacknowledged trend.

    dry_run defaults True so nothing calls out to live CW by accident;
    callers running this for real must opt in explicitly.
    """
    if dry_run:
        return {"created": False, "dry_run": True, "ticket_id": None}

    ticket_ids = trend.get("ticket_ids", [])
    description_lines = [
        trend.get(
            "summary",
            "Automated trend alert: no human acknowledgment after the escalation ladder was exhausted.",
        ),
        f"Ticket count: {trend.get('ticket_count', 0)}",
        f"Device count: {trend.get('device_count', 0)}",
    ]
    if ticket_ids:
        description_lines.append("Related tickets: " + ", ".join(f"#{t}" for t in ticket_ids))

    result = client.create_ticket(
        summary=f"Unacknowledged trend alert: {trend.get('trend_id')}",
        initial_description="\n".join(description_lines),
        board_name=ESCALATION_BOARD_NAME,
        company_id=ESCALATION_COMPANY_ID,
        priority_name="Priority 2 - High",
    )
    return {"created": True, "dry_run": False, "ticket_id": result.get("id")}
