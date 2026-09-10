"""Per-user home skeleton for Agent Penny.

Subcommands:
    init <username>   — create /home/<user>/.agent-penny/ tree (idempotent)
    list              — list Unix users that have a .agent-penny/ directory
    info <username>   — show home, mtime, state.db size, memory/skill counts, last session, last audit

This is the substrate for per-user isolation. All directories under
/home/<user>/.agent-penny/ are mode 700. env.d/ is mode 700. audit.log is
mode 600. The init command NEVER overwrites existing files.

Layout created under each per-user home:
    config.yaml
    state.db
    memories/
    skills/
    cron/
    sessions/
    env.d/
    logs/
    pairing/
    channel_directory.json
    audit.log
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# Username rule: starts with letter or underscore, then [a-z0-9_-]+
# Lowercase only. This is intentionally stricter than POSIX (which allows
# uppercase and dots) to keep filesystem operations predictable and to
# match what most distros do for real interactive users.
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Subdirectories created under each per-user home. The `files` map are
# files (relative path -> content). Directories are created first with
# mode 0o700. Files are written with their explicit modes below.
_USER_DIRS: List[str] = [
    "memories",
    "skills",
    "cron",
    "sessions",
    "env.d",
    "logs",
    "pairing",
]

_USER_FILES: List[str] = [
    "channel_directory.json",
]

# Default config template seeded on init. The agent name and toolset are
# Agent Penny-specific; everything else is a sane default.
_DEFAULT_CONFIG_TEMPLATE = """\
# Agent Penny per-user configuration.
# This file lives at /home/<user>/.agent-penny/config.yaml.
# It overrides any global defaults from the install root.
agent:
  name: Agent Penny
  personality: helpful
  default_toolset: agent-penny-cli

vault:
  backend: mock      # one of: mock, onepassword
  onepassword:
    vault: Personal

user_routing:
  default_user: {username}
  routes: []
  unmatched: warn
"""


def _eprint(*args) -> None:
    print(*args, file=sys.stderr)


def _resolve_user_home(username: str) -> Path:
    """Resolve a Unix username to its home directory.

    Looks the user up in /etc/passwd. Raises if not found.
    Returns the home as a Path (does not check existence).
    """
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise SystemExit(
            f"agent-penny user: user '{username}' not found in /etc/passwd"
        )
    return Path(pw.pw_dir)


def _validate_username(username: str) -> None:
    """Validate a Unix username against the agent-penny rule set.

    Stricter than POSIX: lowercase, [a-z0-9_-], starts with letter or
    underscore, 1-32 chars. Real Unix usernames can contain uppercase and
    dots; we reject those here so filesystem and CLI semantics stay clean.
    """
    if not _USERNAME_RE.match(username):
        raise SystemExit(
            f"agent-penny user: invalid username '{username}'. "
            "Must be lowercase, start with a letter or underscore, "
            "and contain only [a-z0-9_-] (max 32 chars)."
        )


def _set_mode(path: Path, mode: int) -> None:
    """Set the mode bits on path, masking out the umask."""
    os.chmod(path, mode)


def _safe_mkdir(path: Path, mode: int = 0o700) -> None:
    """Create a directory if it doesn't exist; never clobber.

    Idempotent. The mode is applied unconditionally after creation so that
    a half-created directory from a previous failed run is repaired.
    """
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def _safe_write(path: Path, content: str, mode: int) -> bool:
    """Write content to path only if it does not exist.

    Returns True if the file was written, False if it was already present
    (idempotent no-op). The mode is applied on both new and existing
    files so that a wrong-mode file gets repaired.
    """
    if path.exists():
        os.chmod(path, mode)
        return False
    # Write atomically: tmp file in same dir, then rename.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)
    return True


def _audit_log(home: Path, line: str) -> None:
    """Append a timestamped line to the user's audit.log.

    The audit log is mode 600 and never contains secret material — only
    names, hashes, byte counts, and decisions.
    """
    audit = home / "audit.log"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(audit, "a", encoding="utf-8") as f:
        f.write(f"{ts} {line}\n")


def _create_state_db(home: Path) -> None:
    """Create the per-user state.db with a schema_version table.

    The per-user DB is intentionally minimal: schema_version only. It is
    NOT a copy of the global /home/yoda/.hermes/state.db. Other tables
    (sessions, messages, …) are added by later tracks as needed.
    """
    db_path = home / "state.db"
    if db_path.exists():
        # Don't touch an existing DB — it might already have data.
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def init_user(username: str) -> int:
    """Idempotently create /home/<user>/.agent-penny/.

    Returns 0 always (errors raise SystemExit with a non-zero code).
    Prints a one-line summary on success; on re-run, prints 'already
    initialized' and returns 0.
    """
    _validate_username(username)
    home_dir = _resolve_user_home(username)
    if not home_dir.exists():
        raise SystemExit(
            f"agent-penny user: home directory {home_dir} does not exist "
            f"for user '{username}'"
        )
    agent_home = home_dir / ".agent-penny"
    existed_before = agent_home.exists()

    if existed_before:
        # Idempotent re-run: ensure tree is complete and modes are right,
        # but never overwrite user data.
        _safe_mkdir(agent_home, 0o700)
        for d in _USER_DIRS:
            _safe_mkdir(agent_home / d, 0o700)
        # Ensure files exist (do not overwrite).
        for f in _USER_FILES:
            target = agent_home / f
            if not target.exists():
                if f == "channel_directory.json":
                    _safe_write(target, "{}\n", 0o600)
        # Ensure audit.log exists (mode 600).
        audit = agent_home / "audit.log"
        if not audit.exists():
            audit.touch(mode=0o600)
        else:
            os.chmod(audit, 0o600)
        # Ensure config.yaml exists.
        config = agent_home / "config.yaml"
        if not config.exists():
            content = _DEFAULT_CONFIG_TEMPLATE.format(username=username)
            _safe_write(config, content, 0o600)
        else:
            os.chmod(config, 0o600)
        # Ensure state.db exists.
        _create_state_db(agent_home)
        _audit_log(
            agent_home,
            f"event=init-existing user={username} action=no-op",
        )
        print(f"already initialized: {agent_home}")
        return 0

    # Fresh init.
    _safe_mkdir(agent_home, 0o700)
    for d in _USER_DIRS:
        _safe_mkdir(agent_home / d, 0o700)

    # config.yaml — seeded with defaults; mode 600.
    config_content = _DEFAULT_CONFIG_TEMPLATE.format(username=username)
    _safe_write(agent_home / "config.yaml", config_content, 0o600)

    # state.db — empty SQLite with schema_version=1.
    _create_state_db(agent_home)

    # channel_directory.json — empty JSON object.
    _safe_write(agent_home / "channel_directory.json", "{}\n", 0o600)

    # audit.log — empty file, mode 600.
    audit = agent_home / "audit.log"
    audit.touch(mode=0o600)

    _audit_log(
        agent_home,
        f"event=home-initialized user={username} dirs={len(_USER_DIRS)}",
    )
    print(f"initialized: {agent_home}")
    return 0


def list_users() -> int:
    """List Unix users that have a .agent-penny/ directory.

    We scan /etc/passwd and check for /home/<user>/.agent-penny/.
    This is O(users) but the user list is bounded on a real system.
    """
    rows: List[Tuple[str, str]] = []
    for entry in pwd.getpwall():
        pw_dir = entry.pw_dir
        if not pw_dir or not pw_dir.startswith("/home/"):
            continue
        # Skip system accounts with non-agent-penny homes.
        agent_dir = Path(pw_dir) / ".agent-penny"
        if not agent_dir.is_dir():
            continue
        try:
            mtime = datetime.fromtimestamp(
                agent_dir.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            mtime = "?"
        rows.append((entry.pw_name, mtime))

    if not rows:
        print("no initialized users")
        return 0
    rows.sort(key=lambda r: r[0])
    name_w = max(len("USER"), max(len(r[0]) for r in rows))
    print(f"{'USER'.ljust(name_w)}  HOME_MTIME")
    for name, mtime in rows:
        print(f"{name.ljust(name_w)}  {mtime}")
    return 0


def info_user(username: str) -> int:
    """Show per-user home details for a user."""
    _validate_username(username)
    home_dir = _resolve_user_home(username)
    agent_home = home_dir / ".agent-penny"

    if not agent_home.is_dir():
        print(f"not initialized: {agent_home} (run 'agent-penny user init {username}')")
        return 1

    # Basic paths.
    print(f"user:        {username}")
    print(f"home:        {agent_home}")

    # Home mtime.
    try:
        mtime = datetime.fromtimestamp(
            agent_home.stat().st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError as e:
        mtime = f"? ({e})"
    print(f"home_mtime:  {mtime}")

    # state.db size.
    state_db = agent_home / "state.db"
    if state_db.is_file():
        try:
            size = state_db.stat().st_size
        except OSError as e:
            size = f"? ({e})"
        print(f"state_db:    {state_db} ({size} bytes)")
    else:
        print(f"state_db:    (missing)")

    # Memory count.
    memories_dir = agent_home / "memories"
    mem_count = (
        sum(1 for _ in memories_dir.glob("*.md"))
        if memories_dir.is_dir()
        else 0
    )
    print(f"memories:    {mem_count} topic(s)")

    # Skills count.
    skills_dir = agent_home / "skills"
    skill_count = 0
    if skills_dir.is_dir():
        for p in skills_dir.iterdir():
            if p.is_dir():
                skill_count += 1
            elif p.is_file() and p.suffix in (".md", ".py"):
                skill_count += 1
    print(f"skills:      {skill_count} item(s)")

    # Last session — best-effort read of sessions/ mtime.
    sessions_dir = agent_home / "sessions"
    last_session = "none"
    if sessions_dir.is_dir():
        latest_mtime = 0.0
        latest_name = ""
        for p in sessions_dir.iterdir():
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt > latest_mtime:
                latest_mtime = mt
                latest_name = p.name
        if latest_name:
            last_session = (
                f"{latest_name} "
                f"({datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat()})"
            )
    print(f"last_session:{last_session}")

    # Last audit entry.
    audit = agent_home / "audit.log"
    if audit.is_file():
        try:
            with open(audit, "r", encoding="utf-8") as f:
                lines = f.readlines()
            last_audit = lines[-1].rstrip() if lines else "(empty)"
        except OSError as e:
            last_audit = f"? ({e})"
    else:
        last_audit = "(no audit.log)"
    print(f"last_audit:  {last_audit}")

    return 0


# ---------------------------------------------------------------------------
# Argparse wiring — called from hermes_cli.main
# ---------------------------------------------------------------------------


def audit_user(username: str) -> int:
    """Show recent audit.log entries for a per-user home.

    Used by the per-user gateway routing track to verify that the
    routing decisions are recorded per-user.  The audit log is owned
    by the gateway code (gateway/run.py:_handle_message) and contains
    one line per routed message.
    """
    _validate_username(username)
    home_dir = _resolve_user_home(username)
    agent_home = home_dir / ".agent-penny"
    audit = agent_home / "audit.log"
    if not audit.is_file():
        print(f"no audit log: {audit}")
        return 1
    lines = audit.read_text(errors="replace").splitlines()
    # Show the last 50 entries; "routing" entries are the per-message
    # records the gateway writes.  If the log is shorter, show it all.
    tail = lines[-50:] if len(lines) > 50 else lines
    routing_hits = sum(1 for ln in tail if "routing" in ln)
    session_hits = sum(1 for ln in tail if "session" in ln)
    print(f"user:        {username}")
    print(f"audit_log:   {audit}")
    print(f"total_lines: {len(lines)} (showing last {len(tail)})")
    print(f"routing_entries_in_tail: {routing_hits}")
    print(f"session_entries_in_tail: {session_hits}")
    print("--- last 50 lines ---")
    for ln in tail:
        print(ln)
    return 0


def register_cli(parent_parser: argparse.ArgumentParser) -> None:
    """Attach the ``user`` subcommand tree to a parent parser.

    Called from ``hermes_cli.main`` as part of building the top-level
    ``agent-penny user`` parser.
    """
    sub = parent_parser.add_subparsers(dest="user_command")

    init_p = sub.add_parser(
        "init",
        help="Create /home/<user>/.agent-penny/ (idempotent)",
    )
    init_p.add_argument(
        "username",
        help="Unix username whose home will be initialized",
    )

    sub.add_parser(
        "list",
        aliases=["ls"],
        help="List Unix users with an initialized .agent-penny/ home",
    )

    info_p = sub.add_parser(
        "info",
        help="Show per-user home details (mtime, sizes, last session, last audit)",
    )
    info_p.add_argument(
        "username",
        help="Unix username to inspect",
    )

    audit_p = sub.add_parser(
        "audit",
        help="Show the last 50 audit.log entries for a per-user home (routing decisions, sessions)",
    )
    audit_p.add_argument(
        "username",
        help="Unix username whose audit log to inspect",
    )


def cmd_user(args: argparse.Namespace) -> int:
    """Dispatch entry point called by hermes_cli.main."""
    sub = getattr(args, "user_command", None)
    if sub in ("init",):
        return init_user(args.username)
    if sub in ("list", "ls"):
        return list_users()
    if sub == "info":
        return info_user(args.username)
    if sub == "audit":
        return audit_user(args.username)
    # No subcommand: print help.
    return 2
