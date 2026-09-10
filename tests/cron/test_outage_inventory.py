#!/usr/bin/env python3
"""Tests for cron/outage_inventory.py - the two-layer relevance gate that
decides which services Penny is allowed to track at all.

The failable checks named in docs/plans/penny-noc-outage-tracking.md phase 1
are encoded here:

  - Microsoft 365, ShareFile, GoTo PBX and Thomson Reuters Virtual Office
    each resolve to ONE deduped entry despite duplicate configurations.
  - The retired three (Okta, Mimecast, ThreatLocker) are excluded from the
    tracked set even though all three still hold Active configurations.
  - The status-stream noise vendors (Anthropic, OpenAI, GitHub, Zapier,
    Sentry) resolve to nothing, because no configuration exists for them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cron.outage_inventory import (
    IMPLICIT_SERVICES,
    ServiceEntry,
    build_registry,
    flag_stale,
    parse_aliases,
    resolve_service,
    tracked_services,
)

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _config(cfg_id, name, type_name="Software", site="Henssler Financial Headquarters"):
    """Shape mirrors a real /company/configurations row: `vendor` deliberately
    absent, because it is empty on 515 of 543 live rows."""
    return {
        "id": cfg_id,
        "name": name,
        "type": {"name": type_name},
        "site": {"name": site},
        "status": {"name": "Active"},
    }


# Drawn verbatim from the live tenant on 2026-08-25 so the fixture cannot
# drift into being easier than reality.
LIVE_SAMPLE = [
    _config(1, "Microsoft Office 365 - Azure/Exchange/Defender/Entra ID/Active Directory/Sync/SSO/IDP", "Vendor-Office365"),
    _config(2, "Microsoft Office 365 - Azure/Exchange/Defender/Entra ID/Active Directory/Sync/SSO/IDP", "Vendor-Office365"),
    _config(3, "ShareFile - Citrix/SF"),
    _config(4, "ShareFile - SF"),
    _config(5, "Jive/GoTo PBX", "Vendor Phone/Voice/VoIP/PBX"),
    _config(6, "Jive/GoTo PBX", "Vendor Phone/Voice/VoIP/PBX"),
    _config(7, "Virtual Office/Client Tax Portal (VO/V.O./UltraTax/FileCabinet)"),
    _config(8, "Virtual Office/Client Tax Portal (VO/V.O.)"),
    _config(9, "Okta "),
    _config(10, "Okta "),
    _config(11, "Mimecast", "Vendor-Email Security"),
    _config(12, "Threatlocker"),
    _config(13, "NinjaOne"),
    _config(14, "Comcast - HQ ", "Internet Service Provider - ISP"),
    _config(15, "Adobe - SIGN/PDF/CREATIVE/CLOUD/CC/ACROBAT"),
    _config(16, "Power BI"),
    _config(20, "GWH-PF5BEKKB", "Managed Workstation"),
]


# --------------------------------------------------------------------------
# alias parsing
# --------------------------------------------------------------------------

def test_parse_aliases_splits_the_slash_list():
    display, aliases = parse_aliases("Adobe - SIGN/PDF/CREATIVE/CLOUD/CC/ACROBAT")
    assert display == "Adobe"
    assert "acrobat" in aliases
    assert "creative cloud" in aliases or "cc" in aliases


def test_parse_aliases_keeps_parenthetical_abbreviations():
    display, aliases = parse_aliases("Virtual Office/Client Tax Portal (VO/V.O./UltraTax/FileCabinet)")
    assert "ultratax" in aliases
    assert "vo" in aliases
    assert "filecabinet" in aliases


def test_parse_aliases_on_a_plain_name():
    display, aliases = parse_aliases("NinjaOne")
    assert display == "NinjaOne"
    assert "ninjaone" in aliases


def test_parse_aliases_strips_trailing_whitespace_in_live_names():
    # "Okta " and "Comcast - HQ " both carry trailing spaces in live data.
    display, _ = parse_aliases("Okta ")
    assert display == "Okta"


# --------------------------------------------------------------------------
# layer one: dedupe and the configuration gate
# --------------------------------------------------------------------------

def test_duplicate_configurations_collapse_to_one_entry():
    registry = build_registry(LIVE_SAMPLE)
    m365 = resolve_service("Microsoft Office 365", registry)
    assert isinstance(m365, ServiceEntry)
    assert sorted(m365.config_ids) == [1, 2]

    sharefile = resolve_service("ShareFile", registry)
    assert sorted(sharefile.config_ids) == [3, 4]

    pbx = resolve_service("GoTo PBX", registry)
    assert sorted(pbx.config_ids) == [5, 6]

    vo = resolve_service("UltraTax", registry)
    assert sorted(vo.config_ids) == [7, 8]


def test_noise_vendors_resolve_to_nothing():
    """The 367-of-488 status-stream tickets about services Henssler does not
    run must die at layer one, not at a scoring threshold."""
    registry = build_registry(LIVE_SAMPLE)
    for summary in (
        "OpenAI is having a MAJOR outage",
        "Anthropic is having a MINOR outage",
        "GitHub recovered from an outage",
        "Zapier is having a MINOR outage",
        "Sentry is having a MAJOR outage",
    ):
        assert resolve_service(summary, registry) is None, summary


def test_distinctive_first_word_becomes_an_alias():
    """Live gap: the status feed says "Apple", the configuration is named
    "Apple Business Account", and the outage was dropped as noise. A unique
    leading brand word must resolve."""
    rows = LIVE_SAMPLE + [_config(30, "Apple Business Account")]
    registry = build_registry(rows)
    entry = resolve_service("Apple is having a MINOR outage", registry)
    assert entry is not None
    assert 30 in entry.config_ids


def test_ambiguous_first_word_does_not_become_an_alias():
    """"Microsoft" leads several configurations, so it must NOT collapse
    Microsoft Azure into Microsoft Office 365."""
    rows = LIVE_SAMPLE + [_config(31, "Microsoft Azure Subscription")]
    registry = build_registry(rows)
    entry = resolve_service("Microsoft is having an outage", registry)
    assert entry is None


def test_implicit_services_are_tracked_without_a_configuration():
    """ConnectWise runs the help desk but has no configuration row, so the
    configuration gate dropped its outages. Implicit services close that
    hole explicitly rather than by loosening the gate."""
    registry = build_registry(LIVE_SAMPLE)
    entry = resolve_service("ConnectWise is having a MAJOR outage", registry)
    assert entry is not None
    assert entry.tracked
    assert entry.config_ids == ()


def test_ambiguous_brand_does_not_claim_unrelated_products():
    """Live regression: one "Google My Maps" configuration made every Google
    Workspace and Google Cloud outage resolve to a maps entry."""
    rows = LIVE_SAMPLE + [_config(32, "Google My Maps")]
    registry = build_registry(rows)
    assert resolve_service("Google Workspace is having an outage", registry) is None
    assert resolve_service("Google Cloud recovered from an outage", registry) is None
    # The specific product still resolves on its full name.
    assert resolve_service("Google My Maps is broken", registry) is not None


def test_a_workstation_is_not_a_service():
    registry = build_registry(LIVE_SAMPLE)
    assert resolve_service("GWH-PF5BEKKB", registry) is None


def test_isp_configurations_are_tracked_as_services():
    registry = build_registry(LIVE_SAMPLE)
    comcast = resolve_service("Comcast", registry)
    assert comcast is not None
    assert comcast.tracked


# --------------------------------------------------------------------------
# layer two: the retirement gate
# --------------------------------------------------------------------------

def test_retired_services_are_present_but_not_tracked():
    """Suppressed, not deleted. noc_status must be able to answer
    'retired, not tracked' rather than falling silent."""
    registry = build_registry(LIVE_SAMPLE)
    okta = resolve_service("Okta", registry)
    assert okta is not None
    assert not okta.tracked
    assert okta.suppressed_reason == "retired"

    for name in ("Mimecast", "Threatlocker"):
        entry = resolve_service(name, registry)
        assert entry is not None and not entry.tracked, name


def test_tracked_services_excludes_the_retired_three():
    registry = build_registry(LIVE_SAMPLE)
    keys = {e.key for e in tracked_services(registry)}
    assert "okta" not in keys
    assert "mimecast" not in keys
    assert "threatlocker" not in keys
    assert "ninjaone" in keys


def test_retired_list_is_overridable():
    registry = build_registry(LIVE_SAMPLE, retired=frozenset())
    assert resolve_service("Okta", registry).tracked


# --------------------------------------------------------------------------
# staleness flagging: a claim, not a verdict
# --------------------------------------------------------------------------

def test_stale_service_is_flagged_not_suppressed():
    registry = build_registry(LIVE_SAMPLE)
    last_seen = {"ninjaone": NOW - timedelta(days=400)}
    flagged = flag_stale(registry, last_seen, now=NOW, window_days=365)
    assert "ninjaone" in flagged
    # Flagging is advisory. It must NOT silently stop tracking.
    assert registry["ninjaone"].tracked


def test_recently_active_service_is_not_flagged():
    registry = build_registry(LIVE_SAMPLE)
    last_seen = {"ninjaone": NOW - timedelta(days=10)}
    assert flag_stale(registry, last_seen, now=NOW, window_days=365) == set()


# --------------------------------------------------------------------------
# derived M365 sub-services (stream A granularity)
# --------------------------------------------------------------------------

def test_m365_subservices_exist_when_the_parent_config_does():
    """Stream A reports at Teams/Exchange/SharePoint granularity, but the
    configuration name lists neither Teams nor SharePoint. The derived
    entries close that gap without loosening the gate."""
    registry = build_registry(LIVE_SAMPLE)
    teams = resolve_service("Microsoft Teams Service Health Incident", registry)
    assert teams is not None
    assert teams.key == "microsoft_teams"
    # Assert the relationship, not a hardcoded slug: the parent's key comes
    # from the live configuration name ("microsoft_office_365"), not from the
    # shorter DERIVED_SERVICES map key.
    assert teams.parent_key == resolve_service("Microsoft Office 365", registry).key
    assert teams.tracked

    exchange = resolve_service("Exchange Online Service Health Incident", registry)
    assert exchange is not None and exchange.key == "exchange_online"


def test_m365_subservices_vanish_without_the_parent_config():
    """Gate preserved: no Office 365 configuration means no Teams tracking."""
    without_m365 = [c for c in LIVE_SAMPLE if c["id"] not in (1, 2)]
    registry = build_registry(without_m365)
    assert resolve_service("Microsoft Teams Service Health Incident", registry) is None


def test_power_bi_prefers_its_own_configuration():
    registry = build_registry(LIVE_SAMPLE)
    entry = resolve_service("Power BI Service Health Incident", registry)
    assert entry is not None
    assert 16 in entry.config_ids


# --------------------------------------------------------------------------
# resolution behavior
# --------------------------------------------------------------------------

def test_longest_alias_wins():
    registry = build_registry(LIVE_SAMPLE)
    entry = resolve_service("user cannot reach Virtual Office/Client Tax Portal today", registry)
    assert entry is not None
    assert 7 in entry.config_ids


def test_resolution_is_case_insensitive():
    registry = build_registry(LIVE_SAMPLE)
    assert resolve_service("NINJAONE recovered from an outage", registry) is not None


def test_empty_configuration_set_yields_only_implicit_services():
    """Implicit services do not depend on configurations, by definition, so
    an empty pull leaves exactly them and nothing else."""
    registry = build_registry([])
    assert set(registry) == set(IMPLICIT_SERVICES)


def test_malformed_configurations_are_skipped_not_fatal():
    rows = LIVE_SAMPLE + [{"id": 99}, {"name": ""}, None, {"id": 98, "name": None}]
    registry = build_registry(rows)
    assert resolve_service("NinjaOne", registry) is not None
