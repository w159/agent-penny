#!/usr/bin/env python3
"""
Vendor status feeds, used ONLY to let Penny check herself.

These are not a primary source. Nearly all of Penny's outage awareness arrives
as CW tickets, including recoveries. Feeds exist for one job: when an outage
has gone quiet and no recovery ticket ever came, ask the vendor directly
instead of guessing (see cron/outage_revalidate.py).

Coverage is partial and always will be. Registered below are the vendors that
are BOTH tracked in CW configurations and expose a readable feed, verified by
parsing the payload on 2026-08-25.

A trap worth remembering: HTTP 200 is not evidence of a feed. An earlier probe
recorded Adobe and Darktrace as working because both returned 200 - Adobe
serves a JavaScript app shell at that path and Darktrace sits behind a
Cloudflare Access sign-in page. Neither is machine readable, so neither is
registered. Verify by parsing, never by status code.

No free feed exists for Microsoft, Thomson Reuters, Tamarac, eMoney,
PrinterLogic, Mailchimp or ConnectWise. Microsoft matters most and is the
least of a problem, because M365 service health already arrives as tickets.

The rule that governs every function here: a feed that cannot be read returns
"unknown". Never "up". A timeout, a 404, a changed payload shape and a service
with no feed are all failures to observe, not observations of health, and
treating them as health would silently close real outages.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

FEED_TIMEOUT_SECONDS = 12

# Statuspage indicators that mean something is wrong. "none" is healthy;
# "maintenance" is planned work and is deliberately NOT an outage.
STATUSPAGE_BAD_INDICATORS = ("minor", "major", "critical")

ADAPTER_STATUSPAGE = "statuspage_v2"

# Keys on the LEFT are canonical service keys produced by
# cron/outage_inventory.build_registry from live CW configuration names. They
# are not free-form: a typo means the feed silently never fires, which looks
# exactly like a healthy service. test_outage_revalidate.py asserts these
# against the real slugs.
FEED_REGISTRY = {
    "ninjaone": (ADAPTER_STATUSPAGE, "https://status.ninjaone.com/api/v2/summary.json"),
    "sharefile": (ADAPTER_STATUSPAGE, "https://status.sharefile.com/api/v2/summary.json"),
    "jive_goto_pbx": (ADAPTER_STATUSPAGE, "https://status.goto.com/api/v2/summary.json"),
    "cloudflare": (ADAPTER_STATUSPAGE, "https://www.cloudflarestatus.com/api/v2/summary.json"),
    "knowbe4": (ADAPTER_STATUSPAGE, "https://status.knowbe4.com/api/v2/summary.json"),
    "vanta": (ADAPTER_STATUSPAGE, "https://status.vanta.com/api/v2/summary.json"),
    "docusign": (ADAPTER_STATUSPAGE, "https://status.docusign.com/api/v2/summary.json"),
    "asana": (ADAPTER_STATUSPAGE, "https://status.asana.com/api/v2/summary.json"),
    "godaddy": (ADAPTER_STATUSPAGE, "https://status.godaddy.com/api/v2/summary.json"),
    "right_networks": (ADAPTER_STATUSPAGE, "https://status.rightnetworks.com/api/v2/summary.json"),
    "quickbooks_online_intuit": (ADAPTER_STATUSPAGE, "https://status.quickbooks.intuit.com/api/v2/summary.json"),
    "unifi_site_manager": (ADAPTER_STATUSPAGE, "https://status.ui.com/api/v2/summary.json"),
    "yardi": (ADAPTER_STATUSPAGE, "https://status.yardi.com/api/v2/summary.json"),
}


@dataclass
class FeedResult:
    # "up", "down" or "unknown". There is no fourth answer, and "unknown" is
    # the default for every failure mode.
    state: str
    detail: str = ""
    source_url: str = ""


def check_service(service_key: str, fetch=None, now: Optional[datetime] = None) -> FeedResult:
    """Ask the vendor whether a service is up. Never raises."""
    entry = FEED_REGISTRY.get(service_key or "")
    if entry is None:
        return FeedResult("unknown", "no status feed is available for this service")

    adapter, url = entry
    fetcher = fetch or _http_get
    try:
        body = fetcher(url)
    except Exception as exc:  # noqa: BLE001 - any failure is a non-observation
        return FeedResult("unknown", f"feed unreachable: {exc}", url)

    if adapter != ADAPTER_STATUSPAGE:
        return FeedResult("unknown", f"no reader for adapter {adapter}", url)
    result = parse_statuspage_summary(body)
    result.source_url = url
    return result


def parse_statuspage_summary(body) -> FeedResult:
    """Atlassian Statuspage v2 summary.json.

    A payload that does not parse, or that lacks the status block entirely,
    is "unknown". Vendors change these without notice, and a missing block
    must not read as "all systems operational".
    """
    try:
        payload = json.loads(body or "")
    except (TypeError, ValueError):
        return FeedResult("unknown", "status feed did not return JSON")

    if not isinstance(payload, dict):
        return FeedResult("unknown", "status feed returned an unexpected shape")

    status = payload.get("status")
    if not isinstance(status, dict) or "indicator" not in status:
        return FeedResult("unknown", "status feed carried no indicator")

    indicator = str(status.get("indicator", "")).lower()
    description = str(status.get("description", "")).strip()

    if indicator in STATUSPAGE_BAD_INDICATORS:
        return FeedResult("down", description or f"indicator: {indicator}")
    if indicator == "none":
        return FeedResult("up", description or "all systems operational")
    # "maintenance" and anything new land here: not an outage, but not a
    # statement of health either.
    return FeedResult("unknown", description or f"indicator: {indicator}")


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-outage-revalidate/1.0"})
    with urllib.request.urlopen(request, timeout=FEED_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")
