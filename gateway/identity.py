"""Per-user identity resolution for the agent-penny gateway.

Maps a ``(platform, sender_id)`` pair to a Unix username on the host, by
reading the ``user_routing`` block from ``/home/yoda/.hermes/config.yaml``
(or the per-user config under ``HERMES_HOME``). The result is then used
to scope ``HERMES_HOME`` for the duration of message processing so the
state.db, sessions, pairing, channel directory, and audit log all land
under ``/home/<user>/.agent-penny/``.

Schema (under the top-level ``user_routing`` key in config.yaml)::

    user_routing:
      default_user: yoda   # fallback if no specific match
      routes:
        - platform: telegram
          sender_id: '123456789'
          user: yoda
        - platform: webhook
          sender_id: alice-sender
          user: alice
      unmatched: warn   # 'warn' | 'reject' | 'allow_as_default'

The resolve function is intentionally lightweight — it's a synchronous,
in-process dict lookup with a 5-minute LRU cache so the gateway doesn't
take a config-file hit on every message.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Lazy import to avoid module-level import cycles with hermes_constants.
# The SessionSource type lives in gateway.session and is passed in by
# callers, so we don't need to import it here — duck typing is enough.

_CACHE_TTL_SECONDS = 300  # 5 minutes per the plan
_CACHE_MAX_ENTRIES = 1000

_config_lock = threading.Lock()
_config_cache: Dict[str, Any] = {
    "mtime": 0.0,
    "data": None,
    "path": None,
}


def _candidate_config_paths() -> list[Path]:
    """Return the list of config.yaml paths to search, in priority order.

    The active ``HERMES_HOME`` (if set) wins, then ``/home/yoda/.hermes``,
    then ``~/.hermes`` as a last resort. Returns whichever exist.
    """
    candidates: list[Path] = []
    try:
        # Lazy import — keep this module import-safe at top level.
        from hermes_constants import get_hermes_home
        candidates.append(get_hermes_home() / "config.yaml")
    except Exception:
        pass
    candidates.append(Path("/home/yoda/.hermes/config.yaml"))
    candidates.append(Path.home() / ".hermes" / "config.yaml")
    return candidates


def _load_user_routing_config() -> Dict[str, Any]:
    """Load and cache the ``user_routing`` block from config.yaml.

    Returns a dict with keys ``default_user``, ``routes`` (list of dicts),
    and ``unmatched`` (one of ``warn``/``reject``/``allow_as_default``).
    If no config exists, returns a conservative empty config (unmatched=warn,
    no routes, default_user=None).

    The cache is keyed on the file mtime — config edits are picked up on
    the next resolve call without restart.
    """
    with _config_lock:
        for path in _candidate_config_paths():
            if not path.exists():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            cached = _config_cache
            if (
                cached["path"] == path
                and cached["data"] is not None
                and cached["mtime"] == mtime
            ):
                return cached["data"]  # type: ignore[return-value]
            # (Re)load.
            try:
                import yaml  # type: ignore
                with open(path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            except Exception:
                raw = {}
            block = raw.get("user_routing") or {}
            data: Dict[str, Any] = {
                "default_user": block.get("default_user"),
                "routes": list(block.get("routes") or []),
                "unmatched": block.get("unmatched", "warn"),
            }
            _config_cache.update({
                "mtime": mtime,
                "data": data,
                "path": path,
            })
            return data
        # No config found — return conservative empty.
        empty: Dict[str, Any] = {
            "default_user": None,
            "routes": [],
            "unmatched": "warn",
        }
        return empty


def _config_user_match(config: Dict[str, Any], platform: str, sender_id: str) -> Optional[str]:
    """Linear scan the route list for a matching (platform, sender_id).

    Matching is exact-string on both fields. Platform comparison is
    case-insensitive because the enum value (``Platform.WEBHOOK.value``
    = ``"webhook"``) is what callers pass in.
    """
    plat = (platform or "").lower()
    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue
        rp = str(route.get("platform", "")).lower()
        rid = str(route.get("sender_id", ""))
        user = route.get("user")
        if rp == plat and rid == sender_id and user:
            return str(user)
    return None


# Per-(platform, sender_id) result cache with TTL. Sized LRU.
_resolve_cache: "OrderedDict[Tuple[str, str], Tuple[float, Optional[str]]]" = OrderedDict()
_resolve_cache_lock = threading.Lock()


def _cache_get(key: Tuple[str, str]) -> Optional[Optional[str]]:
    """Return the cached result if present and fresh, else None (a miss)."""
    now = time.time()
    with _resolve_cache_lock:
        entry = _resolve_cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if (now - ts) > _CACHE_TTL_SECONDS:
            _resolve_cache.pop(key, None)
            return None
        _resolve_cache.move_to_end(key)
        return value


def _cache_put(key: Tuple[str, str], value: Optional[str]) -> None:
    with _resolve_cache_lock:
        _resolve_cache[key] = (time.time(), value)
        _resolve_cache.move_to_end(key)
        while len(_resolve_cache) > _CACHE_MAX_ENTRIES:
            _resolve_cache.popitem(last=False)


def _platform_name(platform: Any) -> str:
    """Extract the platform name from a Platform enum or a raw string."""
    if platform is None:
        return ""
    val = getattr(platform, "value", None)
    if val is not None:
        return str(val)
    return str(platform)


def _sender_id(source: Any) -> str:
    """Extract the sender_id from a SessionSource duck-typed object.

    For the generic webhook adapter, ``user_id`` is set to
    ``"webhook:<route_name>"`` — not the per-message ``sender_id`` from
    the body. To get per-user routing on webhooks we need the body-level
    ``sender_id`` (or ``user_name``) which the webhook adapter stuffs
    onto ``raw_message``. Falls back to ``user_id`` if not present.
    """
    if source is None:
        return ""
    raw = getattr(source, "raw_message", None)
    if isinstance(raw, dict):
        for key in ("sender_id", "user_id", "from_id", "from"):
            val = raw.get(key)
            if val:
                return str(val)
    rid = getattr(source, "user_id", None)
    if rid:
        return str(rid)
    return ""


def resolve_user(source: Any) -> Optional[str]:
    """Return the Unix username that should own this message, or None.

    Resolution order:
      1. Explicit ``user_routing.routes`` match on (platform, sender_id)
      2. ``user_routing.default_user`` (if set) — for ``unmatched`` policy
         ``warn`` or ``allow_as_default``
      3. None — caller decides what to do (reject/warn/default)

    The result is cached for 5 minutes per (platform, sender_id) so the
    gateway doesn't re-read config.yaml on every message.
    """
    platform = _platform_name(getattr(source, "platform", None))
    sender_id = _sender_id(source)
    if not platform or not sender_id:
        # Without a (platform, sender_id) pair we can't route per-user.
        return None

    key = (platform, sender_id)
    cached = _cache_get(key)
    if cached is not None or key in _resolve_cache:
        # ``_cache_get`` returns None for both "miss" and "cached None",
        # so we have to disambiguate by checking membership. The simpler
        # way: just use the cached value when present, and re-resolve
        # only on true miss.
        if cached is not None or _cache_get(key) is not None or key in _resolve_cache:
            # (The above guards against a race: re-read for truthiness.)
            with _resolve_cache_lock:
                entry = _resolve_cache.get(key)
            if entry is not None:
                return entry[1]

    config = _load_user_routing_config()
    matched = _config_user_match(config, platform, sender_id)
    if matched is not None:
        _cache_put(key, matched)
        return matched

    default_user = config.get("default_user")
    if default_user:
        _cache_put(key, str(default_user))
        return str(default_user)

    _cache_put(key, None)
    return None


def get_unmatched_policy() -> str:
    """Return the configured unmatched-message policy.

    One of ``"warn"`` (default), ``"reject"``, ``"allow_as_default"``.
    """
    config = _load_user_routing_config()
    return str(config.get("unmatched", "warn"))


def resolve_per_user_home(username: str) -> Optional[Path]:
    """Return the per-user home for ``username`` if it exists, else None.

    The path is ``/home/<username>/.agent-penny/``. If the directory
    doesn't exist, the caller should fall back to the default home.
    """
    if not username:
        return None
    # Sanitize: the username has to be Unix-safe.
    import re
    if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", username):
        return None
    home = Path("/home") / username / ".agent-penny"
    return home if home.is_dir() else None
