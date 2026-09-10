#!/usr/bin/env python3
"""
Entity normalization for cron/trend_corpus.py.

Split out of trend_corpus.py to keep both files under the house 300-line
cap. This module's WHOLE JOB is defeating the wording problem the owner
called out: end users describe the same fault in completely unrelated
words ("bitlocker", "blue screen asking for a recovery key", "stuck in
automatic repair"). SYNONYM_MAP below is where that gets fixed - every
entry maps a real phrase seen in the corpus onto one canonical token so
two differently-worded tickets about the same fault land on the same
entity and a later clustering stage can find them.
"""
from __future__ import annotations

import re

# Products/systems the corpus references. Match is substring-on-lowercase,
# so keep entries lowercase and prefer the shortest unambiguous form.
# Extend this list as new recurring nouns show up in real tickets - that
# is the intended maintenance path, not a code change to the extractor.
PRODUCT_TERMS = {
    "outlook", "onedrive", "sharepoint", "teams", "vpn", "bitlocker",
    "windows update", "defender", "yardi", "tamarac", "zocks", "power bi",
    "ultratax", "paylocity", "checkscanner", "printer", "docking station",
    "excel", "word", "adobe", "chrome", "edge", "azure", "office 365",
    "active directory", "duo", "citrix", "ninjaone", "threatlocker",
}

# Phrase -> canonical entity. Order matters: longer/more specific phrases
# are checked before shorter ones so "automatic repair" doesn't get
# swallowed by a looser later match. Both sides of a real-world pair MUST
# resolve to the same right-hand value or the whole feature is pointless -
# see the bitlocker_recovery and startup_repair pairs, which are the
# headline test cases (tickets 94722/95140 and 94792/95169/95232).
SYNONYM_MAP = {
    "automatic repair": "startup_repair",
    "startup repair": "startup_repair",
    "stuck in automatic repair": "startup_repair",
    "recovery key": "bitlocker_recovery",
    "bitlocker": "bitlocker_recovery",
    "blue screen": "bitlocker_recovery",
    "bsod": "bitlocker_recovery",
    "quality update": "windows_update",
    "windows update": "windows_update",
    "reset the device": "device_reset",
    "reimage": "device_reset",
    "freezing": "freeze_crash",
    "crashing": "freeze_crash",
    "crashes": "freeze_crash",
    "black screen": "boot_failure",
    "blank screen": "boot_failure",
    "tiny white square": "boot_failure",
    "hangs on the logo": "boot_failure",
    "hung on the logo": "boot_failure",
    "unresponsive to normal input": "boot_failure",
    "became unresponsive": "boot_failure",
    "shuts off by itself": "boot_failure",
    "shuts off on its own": "boot_failure",
    "powers off by itself": "boot_failure",
    "powers off on its own": "boot_failure",
    "cycles through updates": "update_failure",
    "completed updates while shutting down": "update_failure",
    "stuck installing updates": "update_failure",
    "stuck on updates": "update_failure",
    "pending reboot for updates": "update_failure",
}

# Longest phrase first so multi-word phrases match before a shorter
# substring of themselves steals the hit.
_SYNONYM_PHRASES = sorted(SYNONYM_MAP, key=len, reverse=True)

# boot_failure and update_failure are deliberately regex-based rather than
# plain substrings: real users phrase "the machine won't boot" as "won't
# power on", "keeps powering off", "won't start up", "get it to power on",
# and a dozen other negation shapes. A flat substring list would either
# miss most of them or (if loosened to bare "power on"/"start up") light
# up on unrelated tickets like a conference-room speaker that "says
# Powering Off" or a driver "crashing as soon as it starts up". Each
# pattern below requires the negation/failure word next to the verb so it
# only fires on an actual reported fault, not an incidental mention.
# Job of these two families: catch the SAME underlying fault (a device
# that will not complete boot, or a Windows update that will not
# complete) when it is described in wording unrelated to the phrases
# above - not to guess at root cause. They intentionally do NOT map into
# startup_repair or bitlocker_recovery; a later clustering stage decides
# whether this month's boot_failure/update_failure tickets share a cause.
_BOOT_FAILURE_PATTERNS = [
    re.compile(r"\b(?:won'?t|will\s+not|wont|can'?t|cannot|unable\s+to)\s+"
               r"(?:boot(?:\s+up)?|start\s*up|turn\s*on|power\s*on|power\s*up)\b"),
    re.compile(r"\bkeeps?\s+powering\s+off\b"),
    re.compile(r"\bkeeps?\s+shutting\s+off\b"),
    re.compile(r"\bget\s+\S+\s+(?:computer|laptop|pc|device|machine)\s+to\s+power\s+on\b"),
    re.compile(r"\bpower\s+on\s+(?:my|the|his|her|their)\s+(?:computer|laptop|pc|device|machine)\b"),
]

_UPDATE_FAILURE_PATTERNS = [
    re.compile(r"\bupdates?\s+(?:are|is|was|were)\s+never\s+successful\b"),
    re.compile(r"\bupdates?\s+never\s+succeeds?\b"),
    re.compile(r"\bupdates?\s+keeps?\s+fail(?:ing|s)?\b"),
    re.compile(r"\bupdates?\s+fail(?:s|ed)?\s+to\s+install\b"),
    re.compile(r"\btries?\s+to\s+update\b"),
    re.compile(r"\bupdate\s+(?:loop|error)\b"),
    re.compile(r"\bfail(?:s|ed|ing)?\s+to\s+update\b"),
]

_ERROR_CODE = re.compile(
    r"(?:0x[0-9a-fA-F]{4,8})"          # 0x80070005
    r"|(?:error\s*-?\d{2,5})"           # Error 13, Error -3005
    r"|(?:-\d{3,5}\b)"                  # -3005 standalone
)


def _phrase_entities(text: str) -> set[str]:
    """Find every SYNONYM_MAP phrase and PRODUCT_TERMS term present in text."""
    lowered = text.lower()
    found: set[str] = set()
    for phrase in _SYNONYM_PHRASES:
        if phrase in lowered:
            found.add(SYNONYM_MAP[phrase])
    for term in PRODUCT_TERMS:
        if term in lowered:
            found.add(term.replace(" ", "_"))
    if any(p.search(lowered) for p in _BOOT_FAILURE_PATTERNS):
        found.add("boot_failure")
    if any(p.search(lowered) for p in _UPDATE_FAILURE_PATTERNS):
        found.add("update_failure")
    return found


def _error_code_entities(text: str) -> set[str]:
    """Pull out error codes like -3005, 0x80070005, Error 13."""
    codes = set()
    for match in _ERROR_CODE.finditer(text):
        code = match.group(0).lower().replace(" ", "")
        codes.add(f"err_{code}")
    return codes


def extract_entities(*texts: str, devices: list[str] | None = None) -> set[str]:
    """
    Build the normalized entity set a clustering stage groups tickets on.

    Combines device hostnames, error codes, known product/system terms,
    and synonym-normalized symptom phrases pulled from every text field
    passed in (summary, issue, resolution, tech notes). Deliberately does
    NOT fall back to raw keyword tokens for symptom phrases - a phrase
    only becomes an entity if it is in SYNONYM_MAP or PRODUCT_TERMS, so
    unrelated tickets don't get falsely linked on generic words like
    "computer" or "laptop".
    """
    entities: set[str] = set(devices or [])
    for text in texts:
        if not text:
            continue
        entities |= _phrase_entities(text)
        entities |= _error_code_entities(text)
    return entities
