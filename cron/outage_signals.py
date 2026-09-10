#!/usr/bin/env python3
"""
Turn a raw CW ticket into an outage signal, or into an explicit refusal.

Three machine streams carry the outage picture, all arriving as ordinary
service tickets: Microsoft 365 service health keyed by incident id, a
third-party status service keyed by service name and emoji state, and
infrastructure/RMM alerts keyed by device and site. See cron/outage_patterns.py
for the exact wording of each.

Two rules shape this module.

Classification never reads the board. Measured on the validation corpus, the
M365 stream landed 192 tickets on the NOC board, 9 on SOC and 2 on Triage, so a
board-based classifier silently loses 11 of 203. Structure decides; the board
move is something Penny does later, not evidence she reads.

Nothing is ever silently dropped. Every ticket comes back as an OutageSignal,
including the ones that are not outages, and every refusal carries a reason in
plain words. Refusal is the common case here: most infrastructure traffic is
audit and security noise, and a parser that treats unmatched text as an outage
manufactures events, which is worse than missing one.

Pure transform. Tickets in, signals out, no IO.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from cron.outage_inventory import resolve_service
from cron.outage_patterns import (
    AMBIGUOUS_DEVICE_SHAPE,
    INFRA_DEVICE_ON_SITE,
    INFRA_DOWN_MARKERS,
    INFRA_ROLLUP,
    INFRA_ROOM_INCIDENT,
    INFRA_UP_MARKERS,
    M365_STATES,
    M365_SUMMARY,
    NEVER_AN_OUTAGE,
    STATUS_CLEAR,
    STATUS_MAINTENANCE,
    STATUS_OPEN,
    STREAM_INFRASTRUCTURE,
    STREAM_M365,
    STREAM_STATUS,
    VENDOR_SERVICE_FAULT,
)

__all__ = [
    "OutageSignal", "STREAM_INFRASTRUCTURE", "STREAM_M365", "STREAM_STATUS",
    "classify_ticket", "parse_infrastructure", "parse_m365_health", "parse_status_service",
]


@dataclass
class OutageSignal:
    stream: str
    state: str
    # Joins an open to its close. Incident id for stream A, canonical service
    # name for B, device plus site for C.
    pairing_key: str = ""
    service_name: str = ""
    service_key: str = ""
    severity: str = ""
    device: str = ""
    site: str = ""
    observed_at: Optional[datetime] = None
    ticket_id: Optional[int] = None
    # Why this is not a usable signal. Always populated when state is
    # "excluded" or "unclassified", so a human can see what Penny refused and
    # act on it, rather than the ticket vanishing.
    reason: str = ""
    # A "- New Outage" suffix opens a fresh run rather than extending the
    # prior one, which really happens while an earlier run is still open.
    is_new_run: bool = False
    is_rollup: bool = False
    evidence: str = ""


def parse_m365_health(summary: str):
    """Stream A. Returns None when the summary is not a service health
    incident, so the dispatcher can try the next parser."""
    match = M365_SUMMARY.match(summary or "")
    if not match:
        return None

    raw_state = match.group("state").strip().lower()
    state = M365_STATES.get(raw_state)
    if state is None:
        # A state Microsoft has not used before. Surface it rather than
        # guessing which side of open/clear it falls on.
        return OutageSignal(
            stream=STREAM_M365,
            state="unclassified",
            pairing_key=match.group("incident"),
            service_name=match.group("service").strip(),
            reason=f"unrecognized service health state: {match.group('state').strip()}",
            evidence=summary,
        )

    return OutageSignal(
        stream=STREAM_M365,
        state=state,
        pairing_key=match.group("incident"),
        service_name=match.group("service").strip(),
        evidence=summary,
    )


def parse_status_service(summary: str):
    """Stream B. Maintenance is a distinct state on purpose: treating it as an
    outage generates false correlations, and treating it as a clear closes a
    real outage that is still running."""
    text = (summary or "").strip()

    match = STATUS_CLEAR.match(text)
    if match:
        name = match.group("service").strip()
        return OutageSignal(
            stream=STREAM_STATUS, state="clear", service_name=name,
            pairing_key=_status_key(name), evidence=text,
        )

    match = STATUS_MAINTENANCE.match(text)
    if match:
        name = match.group("service").strip()
        return OutageSignal(
            stream=STREAM_STATUS, state="maintenance", service_name=name,
            pairing_key=_status_key(name), evidence=text,
        )

    match = STATUS_OPEN.match(text)
    if match:
        name = match.group("service").strip()
        return OutageSignal(
            stream=STREAM_STATUS, state="open", service_name=name,
            pairing_key=_status_key(name),
            severity=match.group("severity").lower(),
            is_new_run=bool(match.group("suffix")),
            evidence=text,
        )

    return None


def _status_key(service_name: str) -> str:
    return " ".join((service_name or "").lower().split())

def parse_infrastructure(summary: str):
    """Stream C. Refusal is the common case, and it is always explicit."""
    text = (summary or "").strip()
    if not text:
        return OutageSignal(stream=STREAM_INFRASTRUCTURE, state="unclassified",
                            reason="empty summary", evidence=text)

    lowered = text.lower()
    for marker, reason in NEVER_AN_OUTAGE:
        if marker in lowered:
            return OutageSignal(stream=STREAM_INFRASTRUCTURE, state="excluded",
                                reason=reason, evidence=text)

    for pattern, state in VENDOR_SERVICE_FAULT:
        match = pattern.match(text)
        if match:
            name = match.group("service").strip()
            return OutageSignal(
                stream=STREAM_INFRASTRUCTURE, state=state, service_name=name,
                pairing_key=name.lower(), evidence=text,
            )

    match = INFRA_ROLLUP.match(text)
    if match:
        # A count of alerts is pressure on a site, not a distinct outage.
        # Opening one outage per rollup would double-count every real fault
        # the rollup is summarizing.
        return OutageSignal(
            stream=STREAM_INFRASTRUCTURE, state="informational", is_rollup=True,
            site=match.group("site").strip(), reason="alert rollup, counted as site pressure",
            evidence=text,
        )

    match = INFRA_ROOM_INCIDENT.match(text)
    if match:
        device = match.group("device").strip()
        room = match.group("room").strip()
        return OutageSignal(
            stream=STREAM_INFRASTRUCTURE, state="open", device=device, site=room,
            pairing_key=f"{device}|{room}".lower(),
            service_name=match.group("component").strip(), evidence=text,
        )

    match = INFRA_DEVICE_ON_SITE.match(text)
    if match:
        return _infrastructure_detail(match, text)

    if AMBIGUOUS_DEVICE_SHAPE.match(text):
        return OutageSignal(
            stream=STREAM_INFRASTRUCTURE, state="unclassified", evidence=text,
            reason="RMM device alert with no fault in the summary; needs the note body",
        )

    return OutageSignal(stream=STREAM_INFRASTRUCTURE, state="unclassified",
                        reason="no known infrastructure pattern matched", evidence=text)


def _infrastructure_detail(match, text: str) -> OutageSignal:
    device = match.group("device").strip()
    site = match.group("site").strip()
    detail = match.group("detail").strip().lower()
    pairing_key = f"{device}|{site}".lower()

    # Recovery is checked first: "back online" contains "online", and several
    # down-markers are substrings of up-markers.
    for marker in INFRA_UP_MARKERS:
        if marker in detail:
            return OutageSignal(
                stream=STREAM_INFRASTRUCTURE, state="clear", device=device, site=site,
                pairing_key=pairing_key, evidence=text,
            )
    for marker in INFRA_DOWN_MARKERS:
        if marker in detail:
            return OutageSignal(
                stream=STREAM_INFRASTRUCTURE, state="open", device=device, site=site,
                pairing_key=pairing_key, evidence=text,
            )

    return OutageSignal(
        stream=STREAM_INFRASTRUCTURE, state="unclassified", device=device, site=site,
        reason="device alert with no recognized up or down wording", evidence=text,
    )


def classify_ticket(ticket: dict, registry: dict) -> OutageSignal:
    """Full pipeline for one ticket: parse, then apply the relevance gate.

    Never returns None. A ticket Penny cannot use still comes back, with the
    reason attached, because a silently dropped ticket is indistinguishable
    from a ticket that never existed.
    """
    ticket = ticket if isinstance(ticket, dict) else {}
    summary = ticket.get("summary") or ""

    signal = parse_m365_health(summary) or parse_status_service(summary) or parse_infrastructure(summary)
    signal.ticket_id = ticket.get("id")
    signal.observed_at = _entered_at(ticket)

    if signal.observed_at is None and signal.state not in ("excluded",):
        signal.state = "unclassified"
        signal.reason = "ticket has no dateEntered under _info; refusing to guess a date"
        return signal

    if signal.state in ("excluded", "unclassified"):
        return signal

    return _apply_relevance_gate(signal, registry)


def _apply_relevance_gate(signal: OutageSignal, registry: dict) -> OutageSignal:
    """Layer one and two of cron/outage_inventory, applied to a parsed signal.

    Infrastructure signals are exempt: a device on a Henssler site is by
    definition Henssler's, and it has no vendor name to resolve.
    """
    if signal.stream == STREAM_INFRASTRUCTURE:
        return signal

    entry = resolve_service(signal.service_name, registry or {})
    if entry is None:
        signal.state = "excluded"
        signal.reason = f"'{signal.service_name}' is not tracked: no active configuration"
        return signal

    if not entry.tracked:
        signal.state = "excluded"
        signal.reason = f"'{entry.name}' is {entry.suppressed_reason or 'not tracked'}"
        return signal

    signal.service_key = entry.key
    return signal


def _entered_at(ticket: dict):
    """CW puts dateEntered under _info on a single-ticket GET. Reading it from
    the top level yields empty, and a default of epoch would make every ticket
    look ancient - which has already happened once. Return None and let the
    caller refuse."""
    info = ticket.get("_info")
    raw = info.get("dateEntered") if isinstance(info, dict) else None
    raw = raw or ticket.get("dateEntered")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
