"""Vault connector for Agent Penny.

Subcommands:
    backend list                       - show 'onepassword' and 'mock' and which is active
    backend set <name>                 - set the active backend (writes per-user config.yaml)
    get <name>                         - resolve a single secret (NEVER prints the value)
    list <prefix>                      - list keys under a prefix
    put <name> <value>                 - ONLY for mock backend; refused for onepassword
    resolve --user <user> --session <sid> --out <path>
                                        - materialize all keys for a user into a
                                          per-session env file (chmod 600)
    doctor                             - run health checks on the active backend

Backend interface (Python ABC):
    class VaultBackend:
        def get(self, name: str) -> str
        def list(self, prefix: str) -> list[str]
        def put(self, name: str, value: str) -> None     # may raise on read-only
        def doctor(self) -> list[(str, str, str)]        # (check, status, detail)

Backends:
    MockBackend          - per-user file: /home/<user>/.agent-penny/vault.mock.json
                           (chmod 600). JSON: {"secrets": {name: value, ...}}.
                           Seeds: mailbox.primary, mailbox.password,
                                  calendar.token, knowledge.api_key
                           on first creation.
    OnePasswordBackend   - shells out to the `op` CLI. Read-only. doctor() prints
                           install/sign-in instructions when `op` is missing.

Security:
    - 'vault get' NEVER prints the secret value. It prints 'OK' + last-4 chars
      (or first-4 if length<8) + sha256 fingerprint.
    - Per-session env files (vault resolve --out) are chmod 600, never
      group/world readable.
    - vault.mock.json is chmod 600, owner-only.
    - All 'get' calls are logged to the user's audit.log with: timestamp, name,
      sha256 of value, calling command (argv[0]).
    - No secret value is ever persisted in state.db, sessions.json, or memory.

Per-user scoping:
    - Standalone CLI invocations default to the current Unix user
      (pwd.getpwuid(os.getuid()).pw_name).
    - Agent invocations may override via --user.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Username rule mirrors hermes_cli.user (lowercase, [a-z0-9_-], starts
# with letter or underscore, max 32 chars). Re-declared locally to avoid
# a circular import.
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Seed values for the mock backend on first creation. Real-looking but
# non-functional values — used only for offline testing of the vault layer.
# They are NEVER real credentials.  The actual values are documented here
# so tests can assert against them.
_MOCK_SEED: Dict[str, str] = {
    "mailbox.primary": "alice@henssler.example.com",
    "mailbox.password": "mock-mailbox-pw-9b7c1a8d",
    "calendar.token": "mock-cal-token-3f81e0aa-9c44-4b9b-83c2-aa90c0aa3b41",
    "knowledge.api_key": "mock-knowledge-key-77a1d5c4-bf39-4b58-9a13-7c6f4a91e2d8",
}

# Backends shipped in this build.
BACKEND_NAMES: Tuple[str, ...] = ("mock", "onepassword")

# Status constants used by doctor().
_STATUS_OK = "ok"
_STATUS_FAIL = "fail"
_STATUS_WARN = "warn"
_STATUS_UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eprint(*args) -> None:
    print(*args, file=sys.stderr)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.match(username):
        raise SystemExit(
            f"agent-penny vault: invalid username '{username}'. "
            "Must be lowercase, start with a letter or underscore, "
            "and contain only [a-z0-9_-] (max 32 chars)."
        )


def _current_unix_user() -> str:
    """Return the current Unix user (pw_name from /etc/passwd)."""
    return pwd.getpwuid(os.getuid()).pw_name


def _resolve_user_home(username: str) -> Path:
    """Resolve a Unix username to its home directory."""
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise SystemExit(
            f"agent-penny vault: user '{username}' not found in /etc/passwd"
        )
    return Path(pw.pw_dir)


def _agent_home_for(username: str) -> Path:
    """Return /home/<user>/.agent-penny for the given user. Must exist."""
    _validate_username(username)
    home_dir = _resolve_user_home(username)
    agent_home = home_dir / ".agent-penny"
    if not agent_home.is_dir():
        raise SystemExit(
            f"agent-penny vault: {agent_home} not initialized. "
            f"Run 'agent-penny user init {username}' first."
        )
    return agent_home


def _audit_log(home: Path, line: str) -> None:
    """Append a timestamped line to the user's audit.log (mode 600)."""
    audit = home / "audit.log"
    if not audit.exists():
        audit.touch(mode=0o600)
    os.chmod(audit, 0o600)
    ts = _now_utc()
    with open(audit, "a", encoding="utf-8") as f:
        f.write(f"{ts} {line}\n")


def _set_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _fingerprint(value: str) -> str:
    """Stable fingerprint of a secret value: full sha256 hex."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _last_n(value: str, n: int = 4) -> str:
    """Return the last n chars of value, or first n if shorter than 8."""
    if len(value) < 8:
        return value[:n]
    return value[-n:]


def _redact(name: str) -> str:
    """Mask a name in audit/log output: keep prefix, hide rest."""
    if len(name) <= 4:
        return name[0] + "***" if name else ""
    return name[:4] + "***"


# ---------------------------------------------------------------------------
# Config helpers — read/write the per-user vault.backend field
# ---------------------------------------------------------------------------


def _read_config(home: Path) -> dict:
    """Read the per-user config.yaml. Empty dict if missing/unreadable."""
    import yaml  # local import — pyyaml is heavy and not on the hot path

    path = home / "config.yaml"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _write_config(home: Path, cfg: dict) -> None:
    """Write the per-user config.yaml atomically. Preserves mode 600."""
    import yaml

    path = home / "config.yaml"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def get_active_backend(username: str) -> str:
    """Read vault.backend from the per-user config. Default: 'mock'."""
    home = _agent_home_for(username)
    cfg = _read_config(home)
    vault_cfg = cfg.get("vault", {})
    if not isinstance(vault_cfg, dict):
        return "mock"
    backend = str(vault_cfg.get("backend", "mock")).strip().lower()
    if backend not in BACKEND_NAMES:
        return "mock"
    return backend


def set_active_backend(username: str, backend: str) -> None:
    """Set vault.backend in the per-user config."""
    if backend not in BACKEND_NAMES:
        raise SystemExit(
            f"agent-penny vault: unknown backend '{backend}'. "
            f"Choices: {', '.join(BACKEND_NAMES)}"
        )
    home = _agent_home_for(username)
    cfg = _read_config(home)
    if "vault" not in cfg or not isinstance(cfg.get("vault"), dict):
        cfg["vault"] = {}
    cfg["vault"]["backend"] = backend
    # Ensure the onepassword.vault key exists with a sensible default.
    onepw = cfg["vault"].get("onepassword")
    if not isinstance(onepw, dict):
        cfg["vault"]["onepassword"] = {"vault": "Personal"}
    _write_config(home, cfg)
    print(f"backend: {backend} (active for user '{username}')")


def get_onepassword_vault_name(username: str) -> str:
    """Return the configured 1Password vault name for this user."""
    home = _agent_home_for(username)
    cfg = _read_config(home)
    onepw = cfg.get("vault", {}).get("onepassword", {})
    if isinstance(onepw, dict):
        name = onepw.get("vault")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return "Personal"


# ---------------------------------------------------------------------------
# Backend ABC
# ---------------------------------------------------------------------------


class VaultBackend(ABC):
    """Abstract vault backend.

    Implementations must be picklable enough for a thin wrapper (the
    backends hold only paths/config, no sockets, so this is fine).
    """

    name: str = "abstract"

    @abstractmethod
    def get(self, name: str) -> str:
        """Return the secret value for `name`. Raises KeyError if missing."""

    @abstractmethod
    def list(self, prefix: str) -> List[str]:
        """Return all secret names starting with `prefix`."""

    def put(self, name: str, value: str) -> None:  # noqa: D401
        """Store a secret. Default: raises NotImplementedError.

        Read-only backends (onepassword) inherit this behaviour.
        """
        raise NotImplementedError(
            f"vault backend '{self.name}' is read-only"
        )

    @abstractmethod
    def doctor(self) -> List[Tuple[str, str, str]]:
        """Run health checks. Returns list of (check_name, status, detail).

        status is one of: ok, warn, fail, unavailable.
        """


# ---------------------------------------------------------------------------
# MockBackend
# ---------------------------------------------------------------------------


class MockBackend(VaultBackend):
    """JSON-file backend at /home/<user>/.agent-penny/vault.mock.json.

    On first access, seeds the file with example secrets. The file is
    mode 600. Concurrent writers are not supported; the CLI is
    single-process.
    """

    name = "mock"

    def __init__(self, username: str) -> None:
        self.username = username
        self.home = _agent_home_for(username)
        self.path = self.home / "vault.mock.json"

    # -- file IO ---------------------------------------------------------

    def _load(self) -> dict:
        if not self.path.exists():
            return {"secrets": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"agent-penny vault: mock file is corrupt ({self.path}): {e}"
            )
        if not isinstance(data, dict):
            data = {"secrets": {}}
        if not isinstance(data.get("secrets"), dict):
            data["secrets"] = {}
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        # Always re-assert the mode on the live file (handles pre-existing
        # files that were written with the wrong mode).
        os.chmod(self.path, 0o600)

    def _ensure_seeded(self, data: dict) -> bool:
        """Seed example secrets if the file is freshly created.

        Returns True if a seed was written, False otherwise. Only seeds
        when the file did not exist BEFORE this call.
        """
        if not self.path.exists():
            data.setdefault("secrets", {})
            for k, v in _MOCK_SEED.items():
                data["secrets"].setdefault(k, v)
            self._save(data)
            return True
        return False

    # -- backend interface -----------------------------------------------

    def get(self, name: str) -> str:
        data = self._load()
        secrets = data.get("secrets", {})
        if name not in secrets:
            raise KeyError(name)
        return str(secrets[name])

    def list(self, prefix: str) -> List[str]:
        data = self._load()
        secrets = data.get("secrets", {})
        return sorted(k for k in secrets.keys() if k.startswith(prefix))

    def put(self, name: str, value: str) -> None:
        if not name:
            raise ValueError("secret name must be non-empty")
        data = self._load()
        data.setdefault("secrets", {})
        existed = name in data["secrets"]
        data["secrets"][name] = value
        self._save(data)
        action = "updated" if existed else "created"
        _audit_log(
            self.home,
            f"event=vault-put backend=mock name={_redact(name)} "
            f"action={action} bytes={len(value.encode('utf-8'))} "
            f"sha256={_fingerprint(value)}",
        )

    def doctor(self) -> List[Tuple[str, str, str]]:
        results: List[Tuple[str, str, str]] = []
        # 1. home exists
        if self.home.is_dir():
            results.append(("home", _STATUS_OK, str(self.home)))
        else:
            results.append((
                "home", _STATUS_FAIL,
                f"{self.home} not initialized",
            ))
            return results
        # 2. file exists / parseable
        if not self.path.exists():
            results.append(("file", _STATUS_WARN, f"missing, will seed on first use: {self.path}"))
        else:
            try:
                with open(self.path, encoding="utf-8") as f:
                    json.load(f)
                results.append(("file", _STATUS_OK, str(self.path)))
            except json.JSONDecodeError as e:
                results.append(("file", _STATUS_FAIL, f"corrupt: {e}"))
        # 3. mode is 600
        if self.path.exists():
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode == 0o600:
                results.append(("mode", _STATUS_OK, "0600"))
            else:
                results.append(("mode", _STATUS_WARN, f"mode is {oct(mode)}, expected 0600"))
        # 4. seed on first read
        try:
            data = self._load()
            seeded = self._ensure_seeded(data)
            if seeded:
                results.append(("seed", _STATUS_OK, f"seeded {len(_MOCK_SEED)} example secrets"))
            else:
                results.append(("seed", _STATUS_OK, "skipped (file already present)"))
        except OSError as e:
            results.append(("seed", _STATUS_FAIL, str(e)))
        # 5. count
        try:
            count = len(self._load().get("secrets", {}))
            results.append(("count", _STATUS_OK, f"{count} secret(s)"))
        except Exception as e:  # pragma: no cover — defensive
            results.append(("count", _STATUS_FAIL, str(e)))
        return results


# ---------------------------------------------------------------------------
# OnePasswordBackend
# ---------------------------------------------------------------------------


class OnePasswordBackend(VaultBackend):
    """Read-only backend that shells out to the `op` CLI.

    Item name == secret name. Field defaults to 'password' but can be
    overridden with 'name#field' syntax. The vault name is read from
    the per-user config.yaml.

    All subprocess invocations have timeouts and never raise
    tracebacks to the caller: every failure is mapped to a clean
    SystemExit with a setup hint.
    """

    name = "onepassword"

    def __init__(self, username: str) -> None:
        self.username = username
        self.home = _agent_home_for(username)
        self.vault_name = get_onepassword_vault_name(username)
        self._op_path: Optional[str] = None

    # -- low-level op invocation ----------------------------------------

    def _op_binary(self) -> str:
        if self._op_path is None:
            self._op_path = shutil.which("op") or ""
        return self._op_path

    def _ensure_op_installed(self) -> None:
        if not self._op_binary():
            plat = sys.platform
            install_hint = (
                "To install 1Password CLI:\n"
                "  macOS:   brew install 1password-cli\n"
                "  Linux:   see https://developer.1password.com/docs/cli/get-started/\n"
                "  Windows: winget install AgileBits.1Password.CLI\n"
            )
            raise SystemExit(
                "agent-penny vault: 1Password CLI ('op') is not installed.\n"
                + install_hint
            )

    def _run(self, args: List[str], timeout: float = 15.0) -> str:
        """Run `op <args>` and return stdout. Raise SystemExit on failure."""
        self._ensure_op_installed()
        try:
            result = subprocess.run(
                ["op", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(
                f"agent-penny vault: 'op {' '.join(args)}' timed out after {timeout}s"
            )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            # The most common failure is "not signed in". Detect and
            # surface a friendly setup hint.
            if (
                "not signed in" in stderr.lower()
                or "account" in stderr.lower() and "sign" in stderr.lower()
                or "no account" in stderr.lower()
                or result.returncode == 1 and not stderr
            ):
                raise SystemExit(
                    "agent-penny vault: 1Password CLI is not signed in to an account.\n"
                    "Run: op signin\n"
                    "Or for service accounts: export OP_SERVICE_ACCOUNT_TOKEN=...\n"
                    f"Original error: {stderr or '(no stderr)'}"
                )
            raise SystemExit(
                f"agent-penny vault: 'op {' '.join(args)}' failed "
                f"(exit {result.returncode}): {stderr}"
            )
        return result.stdout

    # -- backend interface -----------------------------------------------

    def get(self, name: str) -> str:
        if not name:
            raise ValueError("secret name must be non-empty")
        # Allow 'name#field' syntax.
        if "#" in name:
            item, field = name.split("#", 1)
        else:
            item, field = name, "password"
        out = self._run(
            [
                "read",
                f"op://{self.vault_name}/{item}/{field}",
            ]
        )
        return out.rstrip("\n").rstrip("\r")

    def list(self, prefix: str) -> List[str]:
        # `op item list` returns a JSON array.
        out = self._run(["item", "list", "--vault", self.vault_name, "--format", "json"])
        try:
            items = json.loads(out or "[]")
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"agent-penny vault: 'op item list' returned non-JSON: {e}"
            )
        names: List[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = it.get("title")
            if isinstance(title, str) and title.startswith(prefix):
                names.append(title)
        return sorted(set(names))

    def put(self, name: str, value: str) -> None:
        raise NotImplementedError(
            "1Password is read-only from agent-penny. "
            "Use the 1Password app to create or update items."
        )

    def doctor(self) -> List[Tuple[str, str, str]]:
        results: List[Tuple[str, str, str]] = []
        op_path = self._op_binary()
        if not op_path:
            results.append((
                "installed",
                _STATUS_UNAVAILABLE,
                "'op' not found. Install 1Password CLI: "
                "https://developer.1password.com/docs/cli/get-started/",
            ))
            return results
        results.append(("installed", _STATUS_OK, op_path))
        # 2. signed in?
        try:
            self._run(["account", "list"], timeout=8.0)
            results.append(("signed-in", _STATUS_OK, ""))
        except SystemExit as e:
            msg = str(e)
            if "not signed in" in msg.lower():
                results.append((
                    "signed-in", _STATUS_FAIL,
                    "not signed in. Run: op signin  (or set OP_SERVICE_ACCOUNT_TOKEN)",
                ))
            else:
                results.append(("signed-in", _STATUS_FAIL, msg.splitlines()[0]))
        # 3. vault reachable?
        try:
            self._run(["vault", "list"], timeout=8.0)
            results.append(("vault-list", _STATUS_OK, ""))
        except SystemExit as e:
            results.append(("vault-list", _STATUS_FAIL, str(e).splitlines()[0]))
        return results


# ---------------------------------------------------------------------------
# Backend factory + per-user scoping
# ---------------------------------------------------------------------------


def make_backend(username: str, backend: Optional[str] = None) -> VaultBackend:
    """Build the active backend for the given user."""
    if backend is None:
        backend = get_active_backend(username)
    if backend == "mock":
        return MockBackend(username)
    if backend == "onepassword":
        return OnePasswordBackend(username)
    raise SystemExit(
        f"agent-penny vault: unknown backend '{backend}'. "
        f"Choices: {', '.join(BACKEND_NAMES)}"
    )


# ---------------------------------------------------------------------------
# High-level commands
# ---------------------------------------------------------------------------


def cmd_backend_list(args: argparse.Namespace) -> int:
    username = args.user or _current_unix_user()
    active = get_active_backend(username)
    print(f"user:     {username}")
    print(f"active:   {active}")
    print("backends:")
    for name in BACKEND_NAMES:
        marker = "*" if name == active else " "
        print(f"  {marker} {name}")
    return 0


def cmd_backend_set(args: argparse.Namespace) -> int:
    username = args.user or _current_unix_user()
    set_active_backend(username, args.backend)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    username = args.user or _current_unix_user()
    backend = make_backend(username)
    home = _agent_home_for(username)
    try:
        value = backend.get(args.name)
    except KeyError:
        _audit_log(
            home,
            f"event=vault-get backend={backend.name} name={_redact(args.name)} "
            f"action=miss",
        )
        print(f"miss: {args.name} (not found in {backend.name} backend)", file=sys.stderr)
        return 1
    except SystemExit:
        # Audit on failure too (graceful failure should still be observable).
        _audit_log(
            home,
            f"event=vault-get backend={backend.name} name={_redact(args.name)} "
            f"action=error",
        )
        raise
    fp = _fingerprint(value)
    tail = _last_n(value)
    _audit_log(
        home,
        f"event=vault-get backend={backend.name} name={_redact(args.name)} "
        f"action=ok sha256={fp} caller={os.path.basename(sys.argv[0] or 'agent-penny')}",
    )
    # 'OK' + last-4 + sha256, NEVER the value.
    print(f"OK {args.name} last4={tail} sha256={fp}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    username = args.user or _current_unix_user()
    backend = make_backend(username)
    names = backend.list(args.prefix)
    for n in names:
        print(n)
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    username = args.user or _current_unix_user()
    backend_name = get_active_backend(username)
    if backend_name != "mock":
        raise SystemExit(
            f"agent-penny vault: 'put' is only allowed on the mock backend "
            f"(active: {backend_name}). Use 1Password to update secrets."
        )
    backend = make_backend(username, "mock")
    # The base MockBackend.put handles audit logging.
    backend.put(args.name, args.value)
    # Confirmation — never print the value.
    print(f"OK {args.name} stored (mock)")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    """Materialize all secrets for a user into a per-session env file."""
    username = args.user
    if not username:
        username = _current_unix_user()
    _validate_username(username)
    home = _agent_home_for(username)
    out_path = Path(args.out).expanduser()
    if out_path.exists():
        # Per-session env files are ephemeral — refuse to clobber an
        # existing file. Use a different --out.
        raise SystemExit(
            f"agent-penny vault: --out {out_path} already exists. "
            "Remove it or choose a different path."
        )
    backend = make_backend(username)
    # For the mock backend, the seed-on-first-use behaviour would create
    # the file here; we want the user's existing data. So for mock,
    # load and only resolve what's there.
    if isinstance(backend, MockBackend):
        data = backend._load()
        secrets = data.get("secrets", {})
        if not secrets:
            raise SystemExit(
                f"agent-penny vault: no secrets stored for user '{username}'. "
                "Use 'agent-penny vault put' to seed some, or use 'vault resolve' "
                "with a user that has a populated vault.mock.json."
            )
    else:
        # For onepassword: we cannot enumerate all secrets, so we only
        # resolve the keys we know about. This is best-effort.
        secrets = {}
        # 1Password has no 'list all items' that returns the value, so
        # we cannot resolve all secrets without knowing the names.
        # In practice, the agent invocation knows the list of secrets
        # to resolve. We accept that limitation and require a list
        # of names in a future iteration. For now, we error clearly.
        raise SystemExit(
            "agent-penny vault: 'resolve' is only supported on the mock "
            "backend in this build. The 1Password backend cannot enumerate "
            "all secrets at once — provide an explicit list of names in a "
            "future iteration."
        )
    # Write the env file. Env var name = secret name with dashes -> underscores.
    lines: List[str] = []
    lines.append(f"# Generated by 'agent-penny vault resolve'")
    lines.append(f"# user: {username}")
    lines.append(f"# session: {args.session}")
    lines.append(f"# backend: {backend.name}")
    lines.append(f"# generated_at: {_now_utc()}")
    for name, value in sorted(secrets.items()):
        # Env var name = secret name with dashes AND dots -> underscores.
        # Shell env names are [A-Za-z_][A-Za-z0-9_]* — dots are not allowed.
        env_name = name.replace("-", "_").replace(".", "_")
        # Quote the value to survive newlines, spaces, etc.
        quoted = json.dumps(value)
        lines.append(f"export {env_name}={quoted}")
    # Atomic write.
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(out_path)
    # Re-assert 600 in case the rename reset it (it shouldn't, but be
    # explicit — the env file is a credentials surface).
    os.chmod(out_path, 0o600)
    # Audit.
    _audit_log(
        home,
        f"event=vault-resolve backend={backend.name} session={args.session} "
        f"count={len(secrets)} out={out_path} mode=0600",
    )
    print(f"OK wrote {len(secrets)} secret(s) to {out_path} (mode 0600)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    username = args.user or _current_unix_user()
    backend = make_backend(username)
    print(f"backend: {backend.name}  user: {username}")
    results = backend.doctor()
    # Compute exit code: 0 only if all are ok or warn; 1 if any fail or unavailable.
    exit_code = 0
    name_w = max((len(c) for c, _, _ in results), default=8)
    status_w = 11
    for check, status, detail in results:
        if status in (_STATUS_FAIL, _STATUS_UNAVAILABLE):
            exit_code = 1
        line = f"  {check.ljust(name_w)}  {status.ljust(status_w)}  {detail}"
        print(line)
    return exit_code


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def register_cli(parent_parser: argparse.ArgumentParser) -> None:
    """Attach the ``vault`` subcommand tree to a parent parser."""
    sub = parent_parser.add_subparsers(dest="vault_command")

    # Optional --user override on every vault subcommand.
    def _add_user_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--user",
            default=None,
            help=(
                "Unix username whose vault to use. Defaults to the current "
                "Unix user."
            ),
        )

    # backend --------------------------------------------------------
    backend_p = sub.add_parser(
        "backend",
        help="List or set the active vault backend",
        description=(
            "List available backends or switch the active one. The active "
            "backend is stored in /home/<user>/.agent-penny/config.yaml "
            "under 'vault.backend'."
        ),
    )
    _add_user_arg(backend_p)
    backend_sub = backend_p.add_subparsers(dest="vault_backend_command")
    backend_sub.add_parser(
        "list",
        help="List available backends and the active one",
    )
    backend_set_p = backend_sub.add_parser(
        "set",
        help="Set the active backend",
    )
    backend_set_p.add_argument(
        "backend",
        choices=list(BACKEND_NAMES),
        help="Backend to make active",
    )

    # get ------------------------------------------------------------
    get_p = sub.add_parser(
        "get",
        help="Resolve a single secret (never prints the value)",
        description=(
            "Resolve a secret and print 'OK' + last-4 chars + sha256 "
            "fingerprint. The full value is NEVER printed to stdout. "
            "For 'name#field' syntax, the field is read instead of "
            "'password' (1Password backend only)."
        ),
    )
    _add_user_arg(get_p)
    get_p.add_argument("name", help="Secret name (e.g. mailbox.primary)")

    # list -----------------------------------------------------------
    list_p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="List secret names under a prefix",
    )
    _add_user_arg(list_p)
    list_p.add_argument(
        "prefix",
        nargs="?",
        default="",
        help="Prefix to filter by (default: all)",
    )

    # put ------------------------------------------------------------
    put_p = sub.add_parser(
        "put",
        help="Store a secret (mock backend only)",
        description=(
            "Store a secret. ONLY allowed on the mock backend. The "
            "1Password backend raises a clear error and suggests using "
            "the 1Password app."
        ),
    )
    _add_user_arg(put_p)
    put_p.add_argument("name", help="Secret name (e.g. mailbox.primary)")
    put_p.add_argument("value", help="Secret value (will be hashed in audit, never logged in plain)")

    # resolve --------------------------------------------------------
    resolve_p = sub.add_parser(
        "resolve",
        help="Materialize all secrets for a user into a per-session env file (chmod 600)",
        description=(
            "Resolve every secret for the user and write a shell-sourceable "
            "file with one 'export NAME=value' line per secret. The file "
            "is created with mode 0600 and refuses to clobber an existing "
            "file."
        ),
    )
    resolve_p.add_argument(
        "--user",
        required=True,
        help="Unix username whose secrets to resolve",
    )
    resolve_p.add_argument(
        "--session",
        required=True,
        help="Session ID (recorded in audit.log and the file header)",
    )
    resolve_p.add_argument(
        "--out",
        required=True,
        help="Output path for the env file (will be chmod 600)",
    )

    # doctor ---------------------------------------------------------
    doctor_p = sub.add_parser(
        "doctor",
        help="Run health checks on the active vault backend",
    )
    _add_user_arg(doctor_p)


def cmd_vault(args: argparse.Namespace) -> int:
    """Top-level dispatch called by hermes_cli.main."""
    sub = getattr(args, "vault_command", None)
    if sub is None:
        return 2
    if sub == "backend":
        bsub = getattr(args, "vault_backend_command", None)
        if bsub is None or bsub == "list":
            return cmd_backend_list(args)
        if bsub == "set":
            return cmd_backend_set(args)
        return 2
    if sub == "get":
        return cmd_get(args)
    if sub in ("list", "ls"):
        return cmd_list(args)
    if sub == "put":
        return cmd_put(args)
    if sub == "resolve":
        return cmd_resolve(args)
    if sub == "doctor":
        return cmd_doctor(args)
    return 2
