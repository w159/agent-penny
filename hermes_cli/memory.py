"""Per-user memory namespace for Agent Penny.

Subcommands:
    write --user <u> <topic> <body>   # writes /home/<u>/.agent-penny/memories/<topic>.md
    read  --user <u> <topic>          # reads it
    list  --user <u>                  # lists topics
    delete --user <u> <topic> [--yes] # removes it
    search --user <u> <query>         # substring search across the user's memories

Security rules (non-negotiable):
    - A user cannot read/list/search another user's memory. The --user value
      must match the current Unix user. Cross-user attempts are refused with
      a clear error and audited in BOTH the current user's audit.log and
      the target user's audit.log.
    - Memory file mode is 0o644 — readable by other users on the host for
      agent-penny tooling, but not writable.
    - Writes are logged with: timestamp, topic, sha256(body), byte length.
      The body itself is NEVER written to the audit log.
    - Re-writing the same topic APPENDS a timestamped section. The previous
      content is preserved. This is intentional: durable memory grows
      over time, it is not overwritten.

Audit placement:
    - The user is the actor (the running process) AND the target
      (the namespace being acted on). For the legitimate self-access case,
      one audit entry is written to /home/<user>/.agent-penny/audit.log.
    - For cross-user denial, two audit entries are written: one to the
      actor's audit.log, one to the target user's audit.log (if it exists).
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import pwd
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# Same rule as hermes_cli.user — keep namespace definitions consistent.
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Per-memory-file mode. 0o644 = owner-writable, world-readable. The
# memory files do not contain secrets (only facts the user wants to
# remember), so this matches the spirit of /etc/hosts.
_MEMORY_FILE_MODE = 0o644

# Per-memory-directory mode. 0o755 so the user can list topics; the
# global install dir is also 0o700 so only the owner can write.
_MEMORY_DIR_MODE = 0o755


def _eprint(*args) -> None:
    print(*args, file=sys.stderr)


def _current_unix_user() -> str:
    """Return the current Unix username (raises SystemExit on lookup failure)."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        # Fall back to LOGNAME / USER env vars, then to the uid-as-string.
        name = os.environ.get("USER") or os.environ.get("LOGNAME")
        if name:
            return name
        raise SystemExit("agent-penny memory: cannot resolve current Unix user")


def _validate_username(username: str) -> None:
    """Validate a Unix username against the agent-penny rule set.

    Stricter than POSIX: lowercase, [a-z0-9_-], starts with letter or
    underscore, 1-32 chars. Real Unix usernames can contain uppercase and
    dots; we reject those here so filesystem and CLI semantics stay clean.
    """
    if not _USERNAME_RE.match(username):
        raise SystemExit(
            f"agent-penny memory: invalid username '{username}'. "
            "Must be lowercase, start with a letter or underscore, "
            "and contain only [a-z0-9_-] (max 32 chars)."
        )


def _resolve_user_home(username: str) -> Path:
    """Resolve a Unix username to its /home/<user> path.

    Raises SystemExit if the user is not in /etc/passwd.
    """
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise SystemExit(
            f"agent-penny memory: user '{username}' not found in /etc/passwd"
        )
    return Path(pw.pw_dir)


def _user_agent_home(username: str) -> Path:
    """Return /home/<user>/.agent-penny — the per-user home."""
    return _resolve_user_home(username) / ".agent-penny"


def _user_memories_dir(username: str) -> Path:
    """Return /home/<user>/.agent-penny/memories — the per-user memories dir."""
    return _user_agent_home(username) / "memories"


def _user_audit_log(username: str) -> Path:
    """Return /home/<user>/.agent-penny/audit.log — the per-user audit log."""
    return _user_agent_home(username) / "audit.log"


def _validate_topic(topic: str) -> None:
    """Validate a memory topic name.

    Topics become filenames: <topic>.md. Restrict to a filesystem-safe set
    and keep them reasonable to type on a CLI.
    """
    if not topic:
        raise SystemExit("agent-penny memory: topic must not be empty")
    if len(topic) > 96:
        raise SystemExit(
            f"agent-penny memory: topic '{topic[:32]}...' too long "
            "(max 96 chars)"
        )
    # Allow letters, digits, dash, underscore, dot. Reject path separators
    # and shell metacharacters. Lowercase and case-insensitive lookups
    # are the caller's problem; we store the topic verbatim.
    if not re.match(r"^[A-Za-z0-9._-]+$", topic):
        raise SystemExit(
            f"agent-penny memory: invalid topic '{topic}'. "
            "Use only [A-Za-z0-9._-] (no spaces, no slashes)."
        )
    if topic in {".", ".."}:
        raise SystemExit("agent-penny memory: topic must not be '.' or '..'")


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _audit(home: Path, line: str) -> None:
    """Append a timestamped line to the per-user audit.log.

    Tolerant: if the audit log does not exist (e.g. the user's home was
    not initialized), this is a no-op. We never auto-create the audit
    log here — only 'agent-penny user init' does that. The exception is
    cross-user denial: in that case, even if the target user has no
    audit.log, we silently skip; the actor's audit.log still records
    the attempt.
    """
    audit = home / "audit.log"
    if not audit.exists():
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(audit, "a", encoding="utf-8") as f:
            f.write(f"{ts} {line}\n")
    except OSError as e:
        _eprint(f"agent-penny memory: warning — could not write audit.log: {e}")


def _require_initialized(username: str, agent_home: Path) -> None:
    """Refuse to operate on a per-user home that has not been initialized."""
    if not agent_home.is_dir():
        raise SystemExit(
            f"agent-penny memory: user '{username}' is not initialized "
            f"(no {agent_home}). Run 'agent-penny user init {username}' first."
        )


def _authorize_actor(actor: str, target: str, action: str) -> None:
    """Enforce the same-Unix-user rule.

    The actor (current Unix user) must equal the target user. If not,
    log a denied attempt in BOTH audit logs (where they exist) and raise
    SystemExit. This is non-negotiable: no escalation, no admin bypass.

    The audit log is computed from /home/<u>/.agent-penny/audit.log
    directly (NOT through pwd.getpwnam) so the denial can be audited
    even when the target user does not exist in /etc/passwd.
    """
    if actor == target:
        return
    # Build audit paths without invoking pwd — we want to log a denial
    # even when the target username is not a valid Unix account.
    actor_audit = Path("/home") / actor / ".agent-penny" / "audit.log"
    target_audit = Path("/home") / target / ".agent-penny" / "audit.log"
    msg_deny = (
        f"event=memory-cross-user-denied actor={actor} target={target} "
        f"action={action}"
    )
    # Audit on the actor side — actor is always a real user (current uid).
    if actor_audit.exists():
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with open(actor_audit, "a", encoding="utf-8") as f:
                f.write(f"{ts} {msg_deny}\n")
        except OSError as e:
            _eprint(f"agent-penny memory: warning — could not write actor audit.log: {e}")
    # Best-effort on target — only if the target user's audit log exists.
    if target_audit.exists():
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with open(target_audit, "a", encoding="utf-8") as f:
                f.write(f"{ts} {msg_deny}\n")
        except OSError as e:
            _eprint(f"agent-penny memory: warning — could not write target audit.log: {e}")
    raise SystemExit(
        f"agent-penny memory: cross-user memory access denied "
        f"(actor='{actor}' cannot access '{target}'s memory)"
    )


def _memory_path(username: str, topic: str) -> Path:
    """Resolve /home/<user>/.agent-penny/memories/<topic>.md."""
    return _user_memories_dir(username) / f"{topic}.md"


def _atomic_write_with_mode(path: Path, content: str, mode: int) -> None:
    """Write content to path atomically with the given mode.

    The directory must already exist. The temp file is created in the
    same directory so the rename is atomic on POSIX.
    """
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def _ensure_memories_dir(username: str) -> Path:
    """Ensure /home/<user>/.agent-penny/memories/ exists; return its Path."""
    d = _user_memories_dir(username)
    d.mkdir(parents=True, exist_ok=True)
    # Repair mode if a previous run left it wrong.
    os.chmod(d, _MEMORY_DIR_MODE)
    return d


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def write_memory(
    username: str, topic: str, body: str, actor: Optional[str] = None
) -> int:
    """Write a memory for `username`. Appends if the topic already exists.

    `actor` is the current Unix user (the running process). If omitted, it
    is resolved via pwd.getpwuid(os.getuid()).
    """
    _validate_username(username)
    _validate_topic(topic)
    if actor is None:
        actor = _current_unix_user()
    _authorize_actor(actor, username, action="write")
    home_dir = _resolve_user_home(username)
    if not home_dir.exists():
        raise SystemExit(
            f"agent-penny memory: home directory {home_dir} does not exist "
            f"for user '{username}'"
        )
    agent_home = home_dir / ".agent-penny"
    _require_initialized(username, agent_home)
    mem_dir = _ensure_memories_dir(username)
    path = _memory_path(username, topic)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body_bytes = len(body.encode("utf-8"))
    body_sha = _sha256_hex(body)
    new_section = (
        f"\n## {ts}\n\n"
        f"{body.rstrip()}\n"
    )

    if path.exists():
        # Append a timestamped section, preserving the prior body.
        with open(path, "a", encoding="utf-8") as f:
            f.write(new_section)
        # Repair mode in case the file was created outside agent-penny.
        os.chmod(path, _MEMORY_FILE_MODE)
        action = "append"
    else:
        # New memory — write a small header so a read shows the topic.
        header = f"# {topic}\n"
        full = f"{header}{new_section}"
        _atomic_write_with_mode(path, full, _MEMORY_FILE_MODE)
        action = "create"

    _audit(
        agent_home,
        f"event=memory-write user={username} actor={actor} topic={topic} "
        f"action={action} sha256={body_sha} bytes={body_bytes}",
    )
    print(f"memory {action}: {path} ({body_bytes} bytes)")
    return 0


def read_memory(
    username: str, topic: str, actor: Optional[str] = None
) -> int:
    """Read a memory body. Returns 0 on success, raises SystemExit otherwise."""
    _validate_username(username)
    _validate_topic(topic)
    if actor is None:
        actor = _current_unix_user()
    _authorize_actor(actor, username, action="read")
    _resolve_user_home(username)  # raises if not in passwd
    agent_home = _user_agent_home(username)
    _require_initialized(username, agent_home)
    path = _memory_path(username, topic)
    if not path.exists():
        raise SystemExit(
            f"agent-penny memory: no memory at {path} "
            f"(use 'memory list --user {username}' to see topics)"
        )
    body = path.read_text(encoding="utf-8")
    # Print to stdout — caller can pipe.
    sys.stdout.write(body)
    if not body.endswith("\n"):
        sys.stdout.write("\n")
    _audit(
        agent_home,
        f"event=memory-read user={username} actor={actor} topic={topic} "
        f"bytes={len(body.encode('utf-8'))}",
    )
    return 0


def list_memories(username: str, actor: Optional[str] = None) -> int:
    """List the topics stored for `username`.

    Prints one topic per line, sorted alphabetically. No header so
    scripts can pipe it. The actor authorization is the same as for
    reads: a cross-user list is denied.
    """
    _validate_username(username)
    if actor is None:
        actor = _current_unix_user()
    _authorize_actor(actor, username, action="list")
    _resolve_user_home(username)  # raises if not in passwd
    agent_home = _user_agent_home(username)
    _require_initialized(username, agent_home)
    mem_dir = _user_memories_dir(username)
    if not mem_dir.is_dir():
        # Init creates the dir, but if someone deleted it we report empty.
        _audit(agent_home, f"event=memory-list user={username} actor={actor} count=0")
        return 0
    topics: List[str] = []
    for entry in sorted(mem_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix != ".md":
            continue
        topics.append(entry.stem)
    _audit(
        agent_home,
        f"event=memory-list user={username} actor={actor} count={len(topics)}",
    )
    for t in topics:
        print(t)
    return 0


def delete_memory(
    username: str, topic: str, yes: bool, actor: Optional[str] = None
) -> int:
    """Delete a memory file. Requires --yes unless the file is empty.

    Refuses if the topic does not exist.
    """
    _validate_username(username)
    _validate_topic(topic)
    if actor is None:
        actor = _current_unix_user()
    _authorize_actor(actor, username, action="delete")
    _resolve_user_home(username)  # raises if not in passwd
    agent_home = _user_agent_home(username)
    _require_initialized(username, agent_home)
    path = _memory_path(username, topic)
    if not path.exists():
        raise SystemExit(
            f"agent-penny memory: no memory at {path}"
        )
    if not yes:
        size = path.stat().st_size
        raise SystemExit(
            f"agent-penny memory: refusing to delete {path} ({size} bytes) "
            f"without --yes"
        )
    path.unlink()
    _audit(
        agent_home,
        f"event=memory-delete user={username} actor={actor} topic={topic}",
    )
    print(f"memory deleted: {path}")
    return 0


def search_memory(
    username: str, query: str, actor: Optional[str] = None
) -> int:
    """Substring search across the user's memories.

    Returns 0 always (search is informational). Prints matching lines
    prefixed with the topic, like:
        henssler-glossary: Henssler Financial is a fee-only ...
    """
    _validate_username(username)
    if actor is None:
        actor = _current_unix_user()
    _authorize_actor(actor, username, action="search")
    _resolve_user_home(username)  # raises if not in passwd
    agent_home = _user_agent_home(username)
    _require_initialized(username, agent_home)
    mem_dir = _user_memories_dir(username)
    if not mem_dir.is_dir():
        _audit(
            agent_home,
            f"event=memory-search user={username} actor={actor} "
            f"query={query!r} matches=0",
        )
        return 0
    q = query.lower()
    matches: List[Tuple[str, str]] = []
    for entry in sorted(mem_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".md":
            continue
        topic = entry.stem
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError as e:
            _eprint(f"warning: cannot read {entry}: {e}")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if q in line.lower():
                matches.append((topic, line.rstrip()))
    _audit(
        agent_home,
        f"event=memory-search user={username} actor={actor} "
        f"query={query!r} matches={len(matches)}",
    )
    for topic, line in matches:
        print(f"{topic}:{line}")
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def register_cli(parent_parser: argparse.ArgumentParser) -> None:
    """Attach the per-user memory subcommands to a parent parser.

    The parent parser is the existing top-level ``memory`` parser
    (see hermes_cli.main), which already has a subparsers group with
    ``dest="memory_command"`` for the external-provider subcommands
    (setup/status/off/reset). To coexist with that group, this function
    ADDS the new per-user subcommands to the same group rather than
    creating a new one — argparse forbids multiple subparser groups
    under the same dest.

    Caller is expected to invoke ``cmd_memory(args)`` for the
    per-user subcommands; the existing main.cmd_memory already routes
    ``write|read|list|delete|search`` to this module.

    All subcommands take a required ``--user`` flag. The same-user rule
    is enforced uniformly: the --user value must match the current Unix
    user, otherwise the call is denied and audited.
    """
    # The parent already has a subparsers group attached under the
    # name ``memory_command``. Reuse it via the action's add_parser().
    sub = None
    for action in parent_parser._actions:  # type: ignore[attr-defined]
        if isinstance(action, argparse._SubParsersAction):
            sub = action
            break
    if sub is None:
        # Standalone parent (e.g. unit tests) — create a fresh group.
        sub = parent_parser.add_subparsers(dest="memory_command")

    # write
    write_p = sub.add_parser(
        "write",
        help="Write (or append to) a memory file for --user",
    )
    write_p.add_argument(
        "--user", required=True,
        help="Target Unix user whose memory namespace to write to",
    )
    write_p.add_argument(
        "topic",
        help="Memory topic (becomes <topic>.md in the memories/ dir)",
    )
    write_p.add_argument(
        "body",
        help="Memory body text. Re-writing appends a timestamped section.",
    )

    # read
    read_p = sub.add_parser(
        "read",
        help="Print a memory file's contents",
    )
    read_p.add_argument("--user", required=True, help="Target Unix user")
    read_p.add_argument("topic", help="Memory topic to read")

    # list
    list_p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="List memory topics for --user",
    )
    list_p.add_argument("--user", required=True, help="Target Unix user")

    # delete
    delete_p = sub.add_parser(
        "delete",
        aliases=["rm"],
        help="Delete a memory file (requires --yes unless empty)",
    )
    delete_p.add_argument("--user", required=True, help="Target Unix user")
    delete_p.add_argument("topic", help="Memory topic to delete")
    delete_p.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt",
    )

    # search
    search_p = sub.add_parser(
        "search",
        help="Substring search across --user's memories",
    )
    search_p.add_argument("--user", required=True, help="Target Unix user")
    search_p.add_argument("query", help="Substring query (case-insensitive)")


def cmd_memory(args: argparse.Namespace) -> int:
    """Dispatcher called by hermes_cli.main."""
    sub = getattr(args, "memory_command", None)
    if sub == "write":
        return write_memory(args.user, args.topic, args.body)
    if sub == "read":
        return read_memory(args.user, args.topic)
    if sub in ("list", "ls"):
        return list_memories(args.user)
    if sub in ("delete", "rm"):
        return delete_memory(args.user, args.topic, args.yes)
    if sub == "search":
        return search_memory(args.user, args.query)
    # No subcommand: print help.
    return 2
