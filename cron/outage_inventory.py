#!/usr/bin/env python3
"""
The relevance gate for Penny's NOC outage tracking: which services is she
allowed to track at all.

Without this gate the outage streams are mostly noise. Over a live 45-day
window the third-party status feed produced 488 tickets, of which 367 were
about Anthropic, OpenAI, GitHub, Zapier and Sentry - services no Henssler end
user touches. Reporting those to the help desk is the exact noise this system
is supposed to remove, so relevance is decided BEFORE any scoring, from the
one source that knows what the firm actually runs: active CW configurations.

Two layers, because each catches something the other cannot.

  Layer one, the configuration gate. No active configuration means the service
  is not tracked and never reported. This is what kills the 367.

  Layer two, the retirement gate. An Active configuration is a claim, not a
  fact - Okta, Mimecast and ThreatLocker all hold Active configurations and all
  three are out of use. Retired services stay IN the registry, marked
  untracked, so noc_status can answer "retired, not tracked" instead of falling
  silent and looking like "nothing is wrong".

Pure transform. Raw configuration dicts in, registry out, no IO. Vocabulary
and naming rules live in cron/outage_vocabulary.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from cron.outage_vocabulary import (
    ALIAS_STOPWORDS,
    AMBIGUOUS_BRANDS,
    DERIVED_SERVICES,
    IMPLICIT_SERVICES,
    RETIRED_SERVICES,
    alias_hit,
    canonical_key,
    is_service_type,
    parse_aliases,
)

# Re-exported so callers import one module for the common case.
__all__ = [
    "ServiceEntry",
    "IMPLICIT_SERVICES",
    "RETIRED_SERVICES",
    "build_registry",
    "flag_stale",
    "parse_aliases",
    "resolve_service",
    "tracked_services",
]


@dataclass
class ServiceEntry:
    key: str
    name: str
    aliases: frozenset
    config_ids: tuple = ()
    types: frozenset = frozenset()
    sites: frozenset = frozenset()
    tracked: bool = True
    # "" when tracked; otherwise why not, so callers can say WHICH kind of
    # "no" they are giving ("retired" reads very differently from "unknown").
    suppressed_reason: str = ""
    # Set on derived entries (Teams -> Microsoft Office 365) so scope and
    # configuration attachment resolve through the parent.
    parent_key: str = ""
    stale: bool = field(default=False)


def build_registry(configurations, retired=RETIRED_SERVICES) -> dict:
    """Raw CW configuration rows -> {service_key: ServiceEntry}.

    Duplicates collapse: ShareFile holds three configurations, GoTo PBX three,
    Office 365 four. They must resolve to one entry or every count Penny
    reports is inflated.

    Malformed rows are skipped rather than raising. A single bad row in a
    543-row pull must not take the whole gate offline, and the gate failing
    open would be worse than the row being missing.
    """
    registry: dict = {}

    for row in configurations or []:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        type_field = row.get("type")
        type_name = type_field.get("name", "") if isinstance(type_field, dict) else ""
        if not is_service_type(type_name):
            continue

        display, aliases = parse_aliases(name)
        key = canonical_key(display)
        if not key:
            continue

        site_field = row.get("site")
        site = site_field.get("name", "") if isinstance(site_field, dict) else ""
        _merge_config(registry, key, display, aliases, row.get("id"), type_name, site)

    _add_leading_brand_aliases(registry)
    _add_implicit_services(registry)
    _apply_retirement_gate(registry, retired)
    _add_derived_services(registry, retired)
    return registry


def _merge_config(registry, key, display, aliases, config_id, type_name, site) -> None:
    entry = registry.get(key)
    if entry is None:
        registry[key] = ServiceEntry(
            key=key,
            name=display,
            aliases=aliases,
            config_ids=tuple(x for x in (config_id,) if x is not None),
            types=frozenset({type_name} if type_name else set()),
            sites=frozenset({site} if site else set()),
        )
        return

    # Merge, union the aliases: duplicate rows carry DIFFERENT alias lists
    # ("ShareFile - Citrix/SF" vs "ShareFile - SF"), and dropping either
    # loses a name a user might type.
    entry.aliases = entry.aliases | aliases
    if config_id is not None and config_id not in entry.config_ids:
        entry.config_ids = entry.config_ids + (config_id,)
    if type_name:
        entry.types = entry.types | {type_name}
    if site:
        entry.sites = entry.sites | {site}


def _add_leading_brand_aliases(registry: dict) -> None:
    """Let a UNIQUE, unambiguous leading brand word resolve on its own.

    Found against live data: the status feed reports "Apple", the
    configuration is named "Apple Business Account", and the outage was
    dropped as noise.

    Two guards keep this from over-matching. Uniqueness: a word leading more
    than one configuration stays unresolvable, which is what keeps Microsoft
    Azure out of Microsoft Office 365. AMBIGUOUS_BRANDS: a word that leads
    exactly one configuration but names a vendor with many unrelated products
    is still rejected, which is what stops the lone "Google My Maps"
    configuration from claiming every Google Workspace outage.
    """
    leaders: dict = {}
    for entry in registry.values():
        words = entry.name.split()
        if not words:
            continue
        first = words[0].lower().strip(".,-")
        if len(first) < 4 or first in ALIAS_STOPWORDS or first in AMBIGUOUS_BRANDS:
            continue
        leaders.setdefault(first, []).append(entry)

    for word, entries in leaders.items():
        if len(entries) == 1:
            entries[0].aliases = entries[0].aliases | {word}


def _add_implicit_services(registry: dict) -> None:
    for key, (name, aliases) in IMPLICIT_SERVICES.items():
        if key in registry:
            continue
        registry[key] = ServiceEntry(key=key, name=name, aliases=frozenset(aliases))


def _apply_retirement_gate(registry: dict, retired) -> None:
    retired_keys = {canonical_key(name) for name in (retired or ())}
    for key, entry in registry.items():
        if key in retired_keys:
            entry.tracked = False
            entry.suppressed_reason = "retired"


def _add_derived_services(registry: dict, retired) -> None:
    """Attach the finer-grained services a vendor reports on, but only while
    the parent configuration is present. This keeps the configuration gate
    intact: no Office 365 configuration, no Teams entry."""
    retired_keys = {canonical_key(name) for name in (retired or ())}

    for parent_key, children in DERIVED_SERVICES.items():
        parent = _find_parent(registry, parent_key)
        if parent is None:
            continue
        for child_key, (child_name, child_aliases) in children.items():
            if child_key in registry:
                # A real configuration of its own always wins over a derived
                # entry - Power BI has one, so it keeps its own config_ids.
                continue
            registry[child_key] = ServiceEntry(
                key=child_key,
                name=child_name,
                aliases=frozenset(child_aliases),
                config_ids=parent.config_ids,
                types=parent.types,
                sites=parent.sites,
                tracked=parent.tracked and child_key not in retired_keys,
                suppressed_reason=parent.suppressed_reason,
                parent_key=parent.key,
            )


def _find_parent(registry: dict, parent_key: str):
    """The Office 365 configuration name canonicalizes to "microsoft_office_365",
    not to the "microsoft_365" key used here, so match on token containment:
    every token of the parent key must appear in the candidate slug."""
    if parent_key in registry:
        return registry[parent_key]
    wanted = set(parent_key.split("_"))
    for key, entry in registry.items():
        if wanted <= set(key.split("_")):
            return entry
    return None


def resolve_service(text: str, registry: dict):
    """Find the service a piece of ticket text is about, or None.

    Longest alias wins, so "Virtual Office/Client Tax Portal" beats a bare
    "office" and a specific workload beats its parent suite. Returning None is
    a first-class answer: it is how the noise tickets get dropped.
    """
    haystack = (text or "").lower()
    if not haystack:
        return None

    best = None
    best_len = 0
    for entry in registry.values():
        for alias in entry.aliases:
            if len(alias) > best_len and alias_hit(alias, haystack):
                best, best_len = entry, len(alias)
    return best


def tracked_services(registry: dict) -> list:
    return [entry for entry in registry.values() if entry.tracked]


def flag_stale(registry: dict, last_activity, now: datetime, window_days: int = 365) -> set:
    """Mark services with no recent activity for human review.

    Advisory on purpose. Silence is not proof a service is gone - it may
    simply have had a good year - so this flags and never suppresses. Only the
    explicit RETIRED_SERVICES list stops tracking.
    """
    flagged = set()
    for key, entry in registry.items():
        if not entry.tracked:
            continue
        seen = (last_activity or {}).get(key)
        if seen is not None and (now - seen).days > window_days:
            entry.stale = True
            flagged.add(key)
    return flagged
