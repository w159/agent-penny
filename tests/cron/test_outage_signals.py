#!/usr/bin/env python3
"""Tests for cron/outage_signals.py - turning raw CW tickets into outage
signals, or into an explicit refusal with a reason.

Every summary string in this file was copied from a live ticket on
2026-08-25. The point of that is to stop the fixtures being easier than
reality: the infrastructure board carries far more non-outages than outages,
and a parser that only ever sees clean examples will happily classify
"Held Email Summary" as a service being down.

Zero silent drops is the contract. Anything not recognized comes back as
state "unclassified" WITH a reason, never as None.
"""
from __future__ import annotations

from datetime import datetime, timezone

from cron.outage_inventory import build_registry
from cron.outage_signals import (
    STREAM_INFRASTRUCTURE,
    STREAM_M365,
    STREAM_STATUS,
    classify_ticket,
    parse_infrastructure,
    parse_m365_health,
    parse_status_service,
)

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _config(cfg_id, name, type_name="Software"):
    return {"id": cfg_id, "name": name, "type": {"name": type_name},
            "site": {"name": "Henssler Financial Headquarters"}, "status": {"name": "Active"}}


REGISTRY = build_registry([
    _config(1, "Microsoft Office 365 - Azure/Exchange/Defender/Entra ID/Active Directory/Sync/SSO/IDP", "Vendor-Office365"),
    _config(2, "NinjaOne"),
    _config(3, "Okta "),
    _config(4, "Apple Business Account"),
])


def _ticket(ticket_id, summary, contact=None, board=" Network Operations Center",
            entered="2026-08-25T09:00:00Z"):
    """Mirrors a live row, including dateEntered living under _info."""
    return {
        "id": ticket_id,
        "summary": summary,
        "contact": ({"name": contact} if contact else None),
        "board": {"name": board},
        "_info": {"dateEntered": entered},
    }


# --------------------------------------------------------------------------
# stream A: Microsoft 365 service health
# --------------------------------------------------------------------------

def test_m365_degradation_opens_and_carries_the_incident_id():
    sig = parse_m365_health("TM1423737: Microsoft Teams Service Health Incident (Service Degradation)")
    assert sig.stream == STREAM_M365
    assert sig.pairing_key == "TM1423737"
    assert sig.service_name == "Microsoft Teams"
    assert sig.state == "open"


def test_m365_restored_clears_on_the_same_pairing_key():
    opened = parse_m365_health("TM1423737: Microsoft Teams Service Health Incident (Service Degradation)")
    cleared = parse_m365_health("TM1423737: Microsoft Teams Service Health Incident (Service Restored)")
    assert cleared.state == "clear"
    assert cleared.pairing_key == opened.pairing_key


def test_m365_investigating_is_an_open_not_a_clear():
    sig = parse_m365_health("EX1442215: Exchange Online Service Health Incident (Investigating)")
    assert sig.state == "open"


def test_m365_false_positive_retracts_rather_than_clears():
    """A false positive means the incident was never real. Recording it as a
    clear would leave a phantom outage in the history that once explained
    user tickets."""
    sig = parse_m365_health("MO1421789: Microsoft 365 suite Service Health Incident (False Positive)")
    assert sig.state == "retracted"


def test_m365_post_incident_report_is_informational():
    """Arrives days after the restore. It must not reopen anything."""
    sig = parse_m365_health("SP1425697: SharePoint Online Service Health Incident (Post-Incident Report Published)")
    assert sig.state == "informational"


def test_m365_investigation_suspended_leaves_it_open_for_revalidation():
    sig = parse_m365_health("EX1427120: Exchange Online Service Health Incident (Investigation Suspended)")
    assert sig.state == "informational"


def test_m365_parser_refuses_a_non_m365_summary():
    assert parse_m365_health("Held Email Summary") is None


# --------------------------------------------------------------------------
# stream B: third-party status service
# --------------------------------------------------------------------------

def test_status_major_outage_opens():
    sig = parse_status_service("\U0001F534 OpenAI is having a MAJOR outage")
    assert sig.stream == STREAM_STATUS
    assert sig.service_name == "OpenAI"
    assert sig.state == "open"
    assert sig.severity == "major"


def test_status_minor_outage_opens_with_lower_severity():
    sig = parse_status_service("\U0001F7E0 Zapier is having a MINOR outage")
    assert sig.state == "open"
    assert sig.severity == "minor"


def test_status_recovery_clears():
    sig = parse_status_service("\U0001F7E2 Sentry recovered from an outage")
    assert sig.state == "clear"
    assert sig.pairing_key == "sentry"


def test_status_maintenance_is_neither_open_nor_clear():
    """Live fourth state, blue circle. Treating maintenance as an outage
    generates false correlations; treating it as a clear closes a real one."""
    sig = parse_status_service("\U0001F535 NinjaOne is undergoing MAINTENANCE")
    assert sig.state == "maintenance"


def test_status_new_outage_suffix_opens_a_fresh_run():
    sig = parse_status_service("\U0001F534 GitHub is having a MAJOR outage - New Outage")
    assert sig.state == "open"
    assert sig.is_new_run


def test_status_pairing_key_matches_across_open_and_clear():
    opened = parse_status_service("\U0001F534 Apple is having a MAJOR outage")
    closed = parse_status_service("\U0001F7E2 Apple recovered from an outage")
    assert opened.pairing_key == closed.pairing_key


def test_status_parser_refuses_an_unrelated_summary():
    assert parse_status_service("NOC Checks (AM/PM)") is None


# --------------------------------------------------------------------------
# stream C: infrastructure. Mostly NOT outages.
# --------------------------------------------------------------------------

def test_infrastructure_device_offline_opens_with_device_and_site():
    sig = parse_infrastructure(
        "pa-820-01 on Henssler Financial Headquarters (hensslerhq): This network element has gone offline")
    assert sig.stream == STREAM_INFRASTRUCTURE
    assert sig.state == "open"
    assert sig.device == "pa-820-01"
    assert "hensslerhq" in (sig.site or "").lower()


def test_infrastructure_room_incident_captures_the_room_as_scope():
    sig = parse_infrastructure(
        "Incident INC-4821: Offline on GWH-CONF2 (Conference Room 2) - Needs action")
    assert sig.state == "open"
    assert sig.device == "GWH-CONF2"
    assert "Conference Room 2" in (sig.site or "")


def test_infrastructure_rollup_is_site_pressure_not_an_outage():
    sig = parse_infrastructure("Henssler Financial Headquarters (hensslerhq): 6 new alerts")
    assert sig.state == "informational"
    assert sig.is_rollup


def test_phish_alert_is_excluded_outright():
    sig = parse_infrastructure("[Phish Alert] RE: Checking back in")
    assert sig.state == "excluded"
    assert "phish" in sig.reason.lower()


def test_routine_noise_is_excluded_with_a_reason():
    """These four shapes alone are 165 tickets in 30 days on the NOC board."""
    for summary in (
        "NOC Checks (AM/PM)",
        "Held Email Summary",
        "Email Notification",
        "Automatic reply: Migrating to Microsoft Authenticator - Update",
    ):
        sig = parse_infrastructure(summary)
        assert sig.state == "excluded", summary
        assert sig.reason, summary


def test_audit_and_security_noise_is_named_not_left_unclassified():
    """Unclassified is the human review queue. Left uncategorized, these
    buckets were 1303 entries over 45 days and nobody would read it, so a
    genuine unknown outage would hide there."""
    for summary, expect in (
        ("GWH-PF5BEKKB - User account added : 'GWH\\svc', CreatedTime: 2026-08-01", "audit"),
        ("GWH-PF5BEKKB - User account removed : 'GWH\\svc'", "audit"),
        ("Microsoft 365 Defender has detected a security threat", "soc"),
        ("New vulnerabilities notification from Microsoft Defender for Endpoint", "soc"),
        ("GWH-PF5BEKKB - Patch management scan FAILED to complete. Error: [-2145]", "patch"),
        ("[synologynas.henssler.com] DSM update is ready to be installed on NAS01", "appliance"),
        ("RE: Migrating to Microsoft Authenticator - Update", "project"),
    ):
        sig = parse_infrastructure(summary)
        assert sig.state == "excluded", (summary, sig.state)
        assert sig.reason, summary


def test_vendor_service_fault_is_recognized_and_still_gated():
    """Spanning backup errors are a real service fault, 43 in 45 days. They
    parse as an outage, but the configuration gate still decides whether
    Henssler tracks that service."""
    sig = parse_infrastructure("Spanning Backup for Office 365 - Error")
    assert sig.state == "open"
    assert "Spanning" in sig.service_name


def test_ambiguous_workstation_alert_is_unclassified_not_guessed():
    """104 tickets in 30 days read exactly like this. The summary alone does
    not say what is wrong, so guessing "outage" would manufacture events."""
    sig = parse_infrastructure("NAP-CONFROOM - Windows Workstation")
    assert sig.state == "unclassified"
    assert sig.reason


# --------------------------------------------------------------------------
# dispatcher: classification never reads the board, and applies the gate
# --------------------------------------------------------------------------

def test_classify_does_not_depend_on_the_board():
    """Live distribution: the M365 stream landed on NOC, SOC and Triage. A
    board-based classifier misses 11 of 203."""
    on_triage = classify_ticket(
        _ticket(96159, "OP1460815: Microsoft 365 apps Service Health Incident (Service Restored)",
                board="Triage"), REGISTRY)
    on_noc = classify_ticket(
        _ticket(96160, "OP1460815: Microsoft 365 apps Service Health Incident (Service Restored)",
                board=" Network Operations Center"), REGISTRY)
    assert on_triage.state == on_noc.state == "clear"
    assert on_triage.stream == on_noc.stream == STREAM_M365


def test_classify_applies_the_relevance_gate():
    sig = classify_ticket(_ticket(93260, "\U0001F534 OpenAI is having a MAJOR outage",
                                  contact="notifications"), REGISTRY)
    assert sig.state == "excluded"
    assert "not tracked" in sig.reason.lower()


def test_classify_keeps_a_tracked_service():
    sig = classify_ticket(_ticket(93326, "\U0001F7E0 NinjaOne is having a MINOR outage",
                                  contact="notifications"), REGISTRY)
    assert sig.state == "open"
    assert sig.service_key == "ninjaone"


def test_classify_marks_a_retired_service_distinctly():
    """"Retired" must not read the same as "we do not know what this is"."""
    sig = classify_ticket(_ticket(93381, "\U0001F534 Okta is having a MAJOR outage",
                                  contact="notifications"), REGISTRY)
    assert sig.state == "excluded"
    assert "retired" in sig.reason.lower()


def test_classify_reads_date_entered_from_info():
    sig = classify_ticket(_ticket(93326, "\U0001F7E0 NinjaOne is having a MINOR outage",
                                  contact="notifications", entered="2026-08-24T13:05:00Z"), REGISTRY)
    assert sig.observed_at == datetime(2026, 8, 24, 13, 5, tzinfo=timezone.utc)


def test_classify_fails_loud_on_a_missing_date():
    """A silent epoch fallback made every ticket look ancient once already."""
    ticket = _ticket(1, "\U0001F7E0 NinjaOne is having a MINOR outage", contact="notifications")
    ticket["_info"] = {}
    sig = classify_ticket(ticket, REGISTRY)
    assert sig.observed_at is None
    assert sig.state == "unclassified"
    assert "date" in sig.reason.lower()


def test_classify_never_returns_none():
    for summary in ("", "something nobody has ever seen before", "Fw: lunch"):
        sig = classify_ticket(_ticket(9, summary), REGISTRY)
        assert sig is not None, summary
        assert sig.state in ("unclassified", "excluded"), summary
        assert sig.reason, summary
