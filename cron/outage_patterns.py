#!/usr/bin/env python3
"""
Recognition tables for cron/outage_signals.py.

Split out to keep both files under the house 300-line cap, and because these
tables are the part that needs editing when a vendor changes its wording.
Every pattern here was derived from live ticket summaries read on 2026-08-25,
not from what a monitoring tool's documentation claims it emits.

The hardest lesson encoded below: the infrastructure board is mostly NOT
outages. Over 30 days, 334 of 716 tickets on it matched neither of the two
structured streams, and the largest shapes among them were routine noise -
"NOC Checks (AM/PM)" (20), "Held Email Summary" (18), "Email Notification"
(17), "Automatic reply: ..." (10). A parser that treats unmatched text as an
outage manufactures events. Refusal is the common case here, and it must be
explicit rather than silent.
"""
from __future__ import annotations

import re

STREAM_M365 = "m365_service_health"
STREAM_STATUS = "status_service"
STREAM_INFRASTRUCTURE = "infrastructure"

# ---------------------------------------------------------------------------
# Stream A: Microsoft 365 service health
# ---------------------------------------------------------------------------
# "TM1423737: Microsoft Teams Service Health Incident (Service Degradation)"
# The incident id is the pairing key, and it is reliable: of 96 distinct ids
# over the validation corpus, 84 carried both an open and a close.
M365_SUMMARY = re.compile(
    r"^\s*(?P<incident>[A-Z]{2}\d{5,})\s*:\s*(?P<service>.+?)\s+Service Health Incident\s*\((?P<state>[^)]+)\)"
)

# Observed states and what each one MEANS for an outage record. The two
# non-obvious ones carry the most weight:
#   False Positive        - the incident was never real. Retract it, do not
#                           "clear" it, or the history keeps a phantom outage
#                           that once explained user tickets.
#   Investigation Suspended - Microsoft stopped looking. That is not a fix, so
#                           the outage stays open and falls to revalidation.
M365_STATES = {
    "service degradation": "open",
    "service interruption": "open",
    "investigating": "open",
    "extended recovery": "open",
    "service restored": "clear",
    "false positive": "retracted",
    "post-incident report published": "informational",
    "investigation suspended": "informational",
}

# ---------------------------------------------------------------------------
# Stream B: third-party status service (contact "notifications")
# ---------------------------------------------------------------------------
# "\U0001F534 OpenAI is having a MAJOR outage"
# "\U0001F7E2 Sentry recovered from an outage"
# "\U0001F535 NinjaOne is undergoing MAINTENANCE"   <- fourth state, found live
STATUS_OPEN = re.compile(
    r"^[^\w]*(?P<service>.+?)\s+is having a\s+(?P<severity>MAJOR|MINOR)\s+outage"
    r"(?P<suffix>\s*-\s*New Outage)?\s*$",
    re.IGNORECASE,
)
STATUS_CLEAR = re.compile(
    r"^[^\w]*(?P<service>.+?)\s+(?:has\s+)?recovered from an outage\s*$", re.IGNORECASE
)
STATUS_MAINTENANCE = re.compile(
    r"^[^\w]*(?P<service>.+?)\s+is undergoing\s+MAINTENANCE\s*$", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Stream C: infrastructure and RMM
# ---------------------------------------------------------------------------
# "pa-820-01 on Henssler Financial Headquarters (hensslerhq): This network
#  element has gone offline"
INFRA_DEVICE_ON_SITE = re.compile(
    r"^(?P<device>[^:]+?)\s+on\s+(?P<site>.+?)\s*:\s*(?P<detail>.+)$"
)

# "Henssler Financial Headquarters (hensslerhq): 6 new alerts"
INFRA_ROLLUP = re.compile(r"^(?P<site>.+?)\s*:\s*(?P<count>\d+)\s+new alerts\s*$", re.IGNORECASE)

# "Incident INC-4821: Offline on GWH-CONF2 (Conference Room 2) - Needs action"
INFRA_ROOM_INCIDENT = re.compile(
    r"^Incident\s+(?P<incident>[^:]+):\s*(?P<component>.+?)\s+on\s+(?P<device>\S+)\s*"
    r"\((?P<room>[^)]+)\)\s*-\s*Needs action\s*$",
    re.IGNORECASE,
)

# Wording that means a thing stopped working, inside an INFRA_DEVICE_ON_SITE
# detail clause. Checked as substrings on lowercase.
INFRA_DOWN_MARKERS = (
    "gone offline",
    "went offline",
    "is offline",
    "has stopped responding",
    "not responding",
    "is down",
    "unreachable",
    "link down",
    "interface down",
)
INFRA_UP_MARKERS = (
    "back online",
    "came online",
    "is online",
    "has recovered",
    "link up",
    "interface up",
    "is reachable again",
)

# Summaries that are never an outage, whatever else they look like. Substring
# match on lowercase. Each entry is here because it was observed on the NOC
# board in volume, not because it seemed plausible.
NEVER_AN_OUTAGE = (
    ("[phish alert]", "phish report, belongs to the SOC lane"),
    ("phish alert", "phish report, belongs to the SOC lane"),
    ("noc checks", "routine operator checklist, not an event"),
    ("held email summary", "mail quarantine digest, not an event"),
    ("email notification", "generic mail relay notice, carries no fault"),
    ("automatic reply:", "out-of-office autoresponse"),
    ("threatlocker application request", "software approval workflow"),
    ("threatlocker elevation request", "software approval workflow"),
    ("new shared credentials found", "security finding, belongs to the SOC lane"),
    ("threat analytics report", "security digest, belongs to the SOC lane"),
    ("defender for cloud apps", "security policy match, belongs to the SOC lane"),
    ("reported by user as malware or phish", "security finding, belongs to the SOC lane"),
    ("phishing document on", "security detection, belongs to the SOC lane"),
    ("user provisioning", "identity sync job, not a service outage"),
    ("cost anomaly detected", "billing alert, not a service outage"),
    # The buckets below were found by inspecting what landed in "unclassified"
    # against 45 days of real traffic. Naming them matters: unclassified is
    # the human review queue, and at 1303 entries of audit noise nobody reads
    # it, so a genuinely unknown outage would hide there unseen.
    ("user account added", "endpoint account audit event, never a service outage"),
    ("user account removed", "endpoint account audit event, never a service outage"),
    ("defender has detected", "security detection, belongs to the SOC lane"),
    ("defender has merged", "security detection, belongs to the SOC lane"),
    ("new vulnerabilities notification", "vulnerability digest, belongs to the SOC lane"),
    ("alert has been added to the microsoft", "security incident update, belongs to the SOC lane"),
    ("severity alert:", "security detection, belongs to the SOC lane"),
    ("patch management scan failed", "endpoint patch failure, device health not a service outage"),
    ("patch management update failed", "endpoint patch failure, device health not a service outage"),
    ("failed to restart windows update services", "endpoint patch failure, device health not a service outage"),
    ("dsm update is ready", "appliance update notice, no fault reported"),
    ("automatic dsm update cancelled", "appliance update notice, no fault reported"),
    ("threatlocker storage request", "software approval workflow"),
    ("migrating to microsoft authenticator", "project email thread, not monitoring output"),
)

# Vendor service faults that arrive with their own wording rather than in the
# device-on-site shape. The relevance gate still decides whether the service is
# tracked, so adding a pattern here does not bypass the configuration check.
VENDOR_SERVICE_FAULT = (
    (re.compile(r"^(?P<service>Spanning Backup for Office 365)\s*-\s*Error", re.IGNORECASE), "open"),
)

# Shapes that clearly come from monitoring but say nothing about what failed.
# "NAP-CONFROOM - Windows Workstation" is 104 tickets in 30 days: the fault is
# only in the note body, so the summary alone must yield "unclassified" rather
# than a guessed outage. Resolving these needs the note text and belongs to a
# later phase, not to a guess here.
AMBIGUOUS_DEVICE_SHAPE = re.compile(
    r"^\s*\S+\s*-\s*(Windows Workstation|Windows Server|Meeting Rooms)\s*$", re.IGNORECASE
)

# Contacts that mark a machine-generated ticket. Deliberately NOT the sole
# classifier: structure decides, and this only breaks ties and flags
# candidates.
#
# The second group was read off the real contact distribution on the NOC and
# SOC boards over 90 days, because guessing at generic words was not enough -
# "Microsoft 365 Defender" (638 tickets) matches none of the obvious markers.
# Extend from observed traffic, not from imagination.
MACHINE_CONTACT_MARKERS = (
    "notifications", "alert", "monitor", "bot", "noreply", "no-reply",
    "system", "automate", "ninja", "auvik", "cloud app", "lansweeper", "reports",
    # observed on the NOC/SOC boards, 2026-08-25
    "defender", "microsoft security", "managed rooms", "help desk",
    "service alert", "docusign account", "office365",
)
