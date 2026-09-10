#!/usr/bin/env python3
"""
Vocabulary and naming rules for cron/outage_inventory.py.

Split out of outage_inventory.py to keep both files under the house 300-line
cap, and because these tables are the part a human will actually maintain.
Everything here answers one of two questions: what counts as a service, and
what words refer to it.

The alias tables exist because live CW configuration names are the closest
thing Henssler has to a maintained synonym list. Names like
"Adobe - SIGN/PDF/CREATIVE/CLOUD/CC/ACROBAT" and
"Virtual Office/Client Tax Portal (VO/V.O./UltraTax/FileCabinet)" were written
by techs recording every name a user might say. Parsing them is free
vocabulary; the maps below only patch the gaps that parsing cannot reach.
"""
from __future__ import annotations

import re

# Configuration types that describe a SERVICE (something that can be "down"
# for many users at once). Managed Workstation, printers and phones are
# devices: they matter as outage scope in the infrastructure stream, not as
# services in their own right. Substring match on lowercase.
SERVICE_TYPE_MARKERS = (
    "software",
    "vendor",
    "internet service provider",
    "isp",
)

# Confirmed out of use or being phased out (Jerry, 2026-08-25). Suppressed,
# not deleted. This list is the intended maintenance path - add a name here
# the day a service is decommissioned, rather than waiting for CW
# configuration hygiene to catch up.
RETIRED_SERVICES = frozenset({"okta", "mimecast", "threatlocker"})

# Words that carry no identifying weight, so they never become an alias on
# their own. "Microsoft Office 365 - Azure/..." must not answer to "office".
ALIAS_STOPWORDS = frozenset({
    "the", "and", "inc", "llc", "corp", "co", "online", "cloud", "portal",
    "client", "service", "services", "software", "vendor", "account",
    "accounts", "app", "apps", "suite", "pro", "plus", "office", "sign",
    "hq", "new", "old",
})

# Aliases too short to match safely on their own. "cc" and "sf" appear inside
# ordinary words and would fire constantly.
MIN_ALIAS_LEN = 3

# Aliases worth keeping despite being under MIN_ALIAS_LEN, because they are
# how people actually refer to the thing in tickets.
SHORT_ALIAS_ALLOWLIST = frozenset({"vo", "365", "cc"})

# Brands that span many unrelated products, so their bare name identifies
# nothing. Without this, the single "Google My Maps" configuration would make
# every Google Workspace and Google Cloud outage resolve to a maps entry -
# observed on the live 45-day stream. "Microsoft" is excluded by the
# uniqueness rule anyway (several configurations lead with it); Google and
# Amazon need saying out loud because only one configuration leads with each.
AMBIGUOUS_BRANDS = frozenset({"google", "amazon", "microsoft"})

# Microsoft 365 reports service health at a granularity its CW configuration
# name does not list: the configuration says "Azure/Exchange/Defender/Entra
# ID/...", but the service-health stream files incidents as Teams, SharePoint,
# Intune and Purview. These derived entries close that gap WITHOUT loosening
# the gate - each exists only while its parent configuration does, so removing
# the Office 365 configuration removes Teams tracking with it.
#
# Extend this map when Microsoft adds a workload, or when another vendor turns
# out to report at a finer grain than its configuration name admits.
DERIVED_SERVICES = {
    "microsoft_365": {
        "microsoft_teams": ("Microsoft Teams", ("microsoft teams", "teams")),
        "exchange_online": ("Exchange Online", ("exchange online", "exchange", "outlook")),
        "sharepoint_online": ("SharePoint Online", ("sharepoint online", "sharepoint")),
        "onedrive": ("OneDrive", ("onedrive", "one drive")),
        "microsoft_intune": ("Microsoft Intune", ("microsoft intune", "intune")),
        "microsoft_purview": ("Microsoft Purview", ("microsoft purview", "purview")),
        "microsoft_entra": ("Microsoft Entra ID", ("entra id", "entra", "azure ad")),
        "microsoft_365_apps": ("Microsoft 365 apps", ("microsoft 365 apps", "m365 apps")),
        "microsoft_365_suite": ("Microsoft 365 suite", ("microsoft 365 suite", "m365 suite")),
    },
}

# Services genuinely in use that have NO configuration row, so the
# configuration gate drops them. Found by running the gate against the live
# 45-day stream: ConnectWise runs the help desk itself and its outages were
# being discarded as noise. Keep this list short and justify each entry - it
# is a hole in layer one, deliberately cut, and every addition widens it.
IMPLICIT_SERVICES = {
    "connectwise": ("ConnectWise", ("connectwise", "connectwise manage", "cw manage")),
}


def canonical_key(name: str) -> str:
    """Lowercase slug used as the registry key and as the join key against
    outage records. Stable across the trailing-whitespace and punctuation
    noise present in live configuration names ("Okta ", "Comcast - HQ ")."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower())
    return slug.strip("_")


def is_service_type(type_name: str) -> bool:
    lowered = (type_name or "").lower()
    return any(marker in lowered for marker in SERVICE_TYPE_MARKERS)


def parse_aliases(config_name: str) -> tuple:
    """Split a live configuration name into (display name, alias set).

    The display name is everything before the first " - " separator or open
    paren; everything else becomes an alias, split on slashes and commas.
    """
    raw = (config_name or "").strip()
    if not raw:
        return "", frozenset()

    # Parenthetical groups are pure alias lists; pull them out first so the
    # display name is not polluted by them.
    parenthetical = re.findall(r"\(([^)]*)\)", raw)
    without_parens = re.sub(r"\([^)]*\)", " ", raw).strip()

    head, _, tail = without_parens.partition(" - ")
    display = head.strip().rstrip("/").strip()

    aliases = set()
    for source in [display, tail, without_parens] + parenthetical:
        for chunk in re.split(r"[/,]", source or ""):
            token = re.sub(r"\s+", " ", chunk.strip().strip(".").lower())
            if not token or token in ALIAS_STOPWORDS:
                continue
            if len(token) < MIN_ALIAS_LEN and token not in SHORT_ALIAS_ALLOWLIST:
                continue
            aliases.add(token)
            # "V.O." and "VO" are the same alias to a human writing a ticket.
            undotted = token.replace(".", "")
            if undotted and undotted != token and (
                len(undotted) >= MIN_ALIAS_LEN or undotted in SHORT_ALIAS_ALLOWLIST
            ):
                aliases.add(undotted)

    return display, frozenset(aliases)


def alias_hit(alias: str, haystack: str) -> bool:
    """Word-boundary match, so "cc" does not fire inside "account" and
    "teams" does not fire inside "teamsters"."""
    return re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", haystack) is not None
