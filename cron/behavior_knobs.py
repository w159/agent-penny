#!/usr/bin/env python3
"""
Knob schema for the behavior store (cron/behavior_store.py).

A "knob" is a named, typed, range- or enum-bounded setting Penny can be
taught to change (e.g. "only alert on trends with 5+ tickets"). This module
is the single source of truth for which keys exist and what values are
legal for them -- an unknown key or an out-of-range value must be rejected
here, never silently accepted by the store.

Adding a knob is a one-line addition to KNOB_SCHEMA below.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class KnobSpec:
    type: str  # "int" | "float" | "bool" | "enum" | "str"
    description: str
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    enum_values: Optional[tuple] = None


# Add a knob here -- one line, following the pattern of an existing entry.
KNOB_SCHEMA: dict[str, KnobSpec] = {
    "trend.alert_min_tickets": KnobSpec(
        "int", "Minimum ticket count before a trend alert fires.",
        min_value=1, max_value=1000,
    ),
    "trend.alert_min_distinct_subjects": KnobSpec(
        "int", "Minimum distinct subjects before a trend alert fires.",
        min_value=1, max_value=1000,
    ),
    "quiet_hours.start": KnobSpec(
        "int", "Hour (0-23, 24h local) quiet hours begin.",
        min_value=0, max_value=23,
    ),
    "quiet_hours.end": KnobSpec(
        "int", "Hour (0-23, 24h local) quiet hours end.",
        min_value=0, max_value=23,
    ),
    "cw.watched_boards": KnobSpec(
        "str", "Comma-separated ConnectWise board names Penny watches.",
    ),
    "cards.post_on_closed_ticket": KnobSpec(
        "bool", "Whether closed-ticket cards post to the group chat at all.",
    ),
}


def _closest_keys(key: str, limit: int = 3) -> list[str]:
    """Cheap nearest-name suggestions for an unknown knob key error message."""
    key_lower = key.lower()
    scored = []
    for candidate in KNOB_SCHEMA:
        c_lower = candidate.lower()
        overlap = len(set(key_lower.split(".")) & set(c_lower.split(".")))
        if key_lower in c_lower or c_lower in key_lower or overlap:
            scored.append((overlap, candidate))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    if scored:
        return [name for _, name in scored[:limit]]
    return sorted(KNOB_SCHEMA)[:limit]


def validate_knob(key: str, value: Any) -> tuple[bool, str]:
    """Validate a proposed knob key/value against KNOB_SCHEMA.

    Returns (True, "") when valid, or (False, message) naming the problem
    and, for unknown keys, the closest valid keys. Never raises -- callers
    decide whether to reject or surface the message.
    """
    spec = KNOB_SCHEMA.get(key)
    if spec is None:
        suggestions = ", ".join(_closest_keys(key))
        return False, (
            f"unknown knob key {key!r}. Closest valid keys: {suggestions}. "
            f"Valid keys: {', '.join(sorted(KNOB_SCHEMA))}"
        )

    if spec.type == "bool":
        if not isinstance(value, bool):
            return False, f"{key!r} must be a bool, got {type(value).__name__}"
        return True, ""

    if spec.type in ("int", "float"):
        expected = int if spec.type == "int" else (int, float)
        if isinstance(value, bool) or not isinstance(value, expected):
            return False, f"{key!r} must be a {spec.type}, got {type(value).__name__}"
        if spec.min_value is not None and value < spec.min_value:
            return False, f"{key!r} must be >= {spec.min_value}, got {value}"
        if spec.max_value is not None and value > spec.max_value:
            return False, f"{key!r} must be <= {spec.max_value}, got {value}"
        return True, ""

    if spec.type == "enum":
        if value not in (spec.enum_values or ()):
            return False, f"{key!r} must be one of {spec.enum_values}, got {value!r}"
        return True, ""

    if spec.type == "str":
        if not isinstance(value, str):
            return False, f"{key!r} must be a str, got {type(value).__name__}"
        return True, ""

    return False, f"{key!r} has an unrecognized spec type {spec.type!r}"
