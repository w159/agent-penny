#!/usr/bin/env python3
"""Tests for cron/outage_revalidate.py and cron/outage_feeds.py - Penny
checking herself when no recovery ticket ever arrives.

The governing rule, and the one most likely to be broken by a well-meaning
change: a failure to reach an answer produces "unknown", never "cleared". A
dead feed, a timeout, a service with no feed at all - none of those are
evidence that anything recovered. Declaring an outage fixed because we could
not check is worse than saying we do not know, because an outage marked
cleared silently stops explaining the user tickets it actually caused.

No network. Every fetch is injected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cron.outage_feeds import (
    FEED_REGISTRY,
    FeedResult,
    check_service,
    parse_statuspage_summary,
)
from cron.outage_revalidate import revalidate_all, revalidate_one
from cron.outage_signals import OutageSignal, STREAM_INFRASTRUCTURE, STREAM_M365, STREAM_STATUS
from cron.outage_store import active_outages, apply_signal, outages_for_service
from cron.outage_windows import OUTAGE_QUIET_DAYS

T0 = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
QUIET = T0 + timedelta(days=OUTAGE_QUIET_DAYS + 1)


@pytest.fixture()
def db(tmp_path):
    from cron.outage_db import connect
    conn = connect(tmp_path / "outage.db")
    yield conn
    conn.close()


def _open_run(db, service_key="ninjaone", stream=STREAM_STATUS, pairing_key="ninjaone",
              name="NinjaOne", **kw):
    apply_signal(db, OutageSignal(stream=stream, state="open", pairing_key=pairing_key,
                                  service_key=service_key, service_name=name,
                                  ticket_id=1, evidence="fixture", **kw), now=T0)


OPERATIONAL = '{"status": {"indicator": "none", "description": "All Systems Operational"}, "components": []}'
DEGRADED = ('{"status": {"indicator": "major", "description": "Partial System Outage"},'
            ' "components": [{"name": "API", "status": "major_outage"}]}')


# --------------------------------------------------------------------------
# feed parsing
# --------------------------------------------------------------------------

def test_statuspage_operational_reads_as_up():
    result = parse_statuspage_summary(OPERATIONAL)
    assert result.state == "up"


def test_statuspage_incident_reads_as_down():
    result = parse_statuspage_summary(DEGRADED)
    assert result.state == "down"
    assert "Partial System Outage" in result.detail


def test_statuspage_garbage_is_unknown_not_up():
    """A feed that changed shape must not read as 'all clear'."""
    for body in ("", "<html>404</html>", "{}", "null"):
        assert parse_statuspage_summary(body).state == "unknown", body


def test_a_200_response_that_is_not_a_feed_is_unknown():
    """Adobe and Darktrace both return HTTP 200: one serves a JavaScript app
    shell, the other a Cloudflare Access sign-in page. Status code is not
    evidence of a feed."""
    for body in ("<!doctype html><html><head><title>Adobe Status</title>",
                 "<!DOCTYPE html><html><head><title>Sign in - Cloudflare Access</title>"):
        assert parse_statuspage_summary(body).state == "unknown"


def test_check_service_without_a_feed_is_unknown():
    assert check_service("microsoft_teams", fetch=lambda url: OPERATIONAL).state == "unknown"


def test_check_service_network_failure_is_unknown_not_up():
    def boom(url):
        raise OSError("connection reset")
    result = check_service("ninjaone", fetch=boom)
    assert result.state == "unknown"
    assert "connection reset" in result.detail


def test_feed_registry_keys_match_real_registry_slugs():
    """These slugs come from live CW configuration names. A typo here means a
    feed silently never fires, which looks identical to a healthy service."""
    for key in ("ninjaone", "sharefile", "jive_goto_pbx", "cloudflare", "knowbe4",
                "vanta", "docusign", "asana", "godaddy", "right_networks",
                "quickbooks_online_intuit", "unifi_site_manager", "yardi"):
        assert key in FEED_REGISTRY, key


def test_unreadable_vendors_are_not_registered():
    """Registering a feed that cannot be parsed is worse than none: it burns a
    request and still answers unknown, while looking like coverage."""
    for key in ("adobe", "darktrace", "mailchimp", "connectwise", "printerlogic"):
        assert key not in FEED_REGISTRY, key


# --------------------------------------------------------------------------
# revalidation outcomes
# --------------------------------------------------------------------------

def test_feed_says_recovered_so_the_run_clears(db):
    _open_run(db)
    outcome = revalidate_one(db, active_outages(db)[0], now=QUIET,
                             fetch=lambda url: OPERATIONAL)
    assert outcome == "recovered"
    stored = outages_for_service(db, "ninjaone")[0]
    assert stored["status"] == "cleared"
    assert "revalidat" in stored["clear_reason"]


def test_feed_says_still_down_so_the_run_is_extended(db):
    _open_run(db)
    outcome = revalidate_one(db, active_outages(db)[0], now=QUIET,
                             fetch=lambda url: DEGRADED)
    assert outcome == "still_down"
    run = active_outages(db)[0]
    assert run["status"] == "open"
    assert run["last_signal_at"] > run["opened_at"]


def test_unreachable_feed_marks_unknown_never_cleared(db):
    def boom(url):
        raise TimeoutError("timed out")
    _open_run(db)
    outcome = revalidate_one(db, active_outages(db)[0], now=QUIET, fetch=boom)
    assert outcome == "unresolvable"
    stored = outages_for_service(db, "ninjaone")[0]
    assert stored["status"] == "unknown"
    assert stored["cleared_at"] is None


def test_service_with_no_feed_falls_through_to_unknown(db):
    """Microsoft has no free feed. An Exchange run that never got a restore
    ticket must end up unknown, not quietly cleared."""
    _open_run(db, service_key="exchange_online", stream=STREAM_M365,
              pairing_key="EX1442215", name="Exchange Online")
    outcome = revalidate_one(db, active_outages(db)[0], now=QUIET,
                             fetch=lambda url: OPERATIONAL)
    assert outcome == "unresolvable"
    assert outages_for_service(db, "exchange_online")[0]["status"] == "unknown"


def test_infrastructure_run_with_fresh_device_activity_stays_open(db):
    """Signal two: no feed exists for a device, but the device is still
    alerting, which is direct evidence it has not recovered."""
    _open_run(db, service_key="", stream=STREAM_INFRASTRUCTURE,
              pairing_key="pa-820-01|hq", name="pa-820-01", device="pa-820-01")
    outcome = revalidate_one(db, active_outages(db)[0], now=QUIET,
                             fetch=lambda url: OPERATIONAL,
                             recent_activity_keys={"pa-820-01|hq"})
    assert outcome == "still_down"
    assert active_outages(db)[0]["status"] == "open"


def test_a_run_that_is_not_quiet_is_left_alone(db):
    _open_run(db)
    changed = revalidate_all(db, now=T0 + timedelta(hours=1), fetch=lambda url: OPERATIONAL)
    assert changed == {}
    assert active_outages(db)[0]["status"] == "open"


def test_revalidate_all_processes_every_quiet_run(db):
    _open_run(db)
    _open_run(db, service_key="cloudflare", pairing_key="cloudflare", name="Cloudflare")
    changed = revalidate_all(db, now=QUIET, fetch=lambda url: OPERATIONAL)
    assert set(changed.values()) == {"recovered"}
    assert active_outages(db) == []


def test_revalidation_records_its_evidence(db):
    _open_run(db)
    revalidate_one(db, active_outages(db)[0], now=QUIET, fetch=lambda url: DEGRADED)
    run = active_outages(db)[0]
    assert "Partial System Outage" in run["evidence"]


def test_one_bad_feed_does_not_stop_the_sweep(db):
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("first one dies")
        return OPERATIONAL

    _open_run(db)
    _open_run(db, service_key="cloudflare", pairing_key="cloudflare", name="Cloudflare")
    changed = revalidate_all(db, now=QUIET, fetch=flaky)
    assert len(changed) == 2
    assert set(changed.values()) == {"unresolvable", "recovered"}
