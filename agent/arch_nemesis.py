"""Loader for Agent Penny's configurable "arch nemesis" running-gag target.

The name itself lives in ``memories/ops/arch_nemesis.md`` under HERMES_HOME -
this module is the one place that knows how to read it, so callers never
parse the markdown themselves. See SOUL.md's "ARCH NEMESIS" section for the
persona rules that consume the name this returns.
"""

from __future__ import annotations

from typing import Optional

from hermes_constants import get_hermes_home

_CONFIG_RELATIVE_PATH = "memories/ops/arch_nemesis.md"
_NAME_PREFIX = "name:"


def get_arch_nemesis_config_path():
    """Return the Path to the arch-nemesis config file (may not exist)."""
    return get_hermes_home() / _CONFIG_RELATIVE_PATH


def load_arch_nemesis_name() -> Optional[str]:
    """Return the configured arch-nemesis name, or None if there isn't one.

    None covers every "no bit today" case on purpose: missing file, empty
    file, a file with no ``name:`` line, or a ``name:`` line left blank.
    Callers must treat None as "omit the running gag entirely" rather than
    inventing a target or raising.
    """
    path = get_arch_nemesis_config_path()

    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith(_NAME_PREFIX):
            continue
        name = stripped[len(_NAME_PREFIX):].strip()
        return name or None

    return None
