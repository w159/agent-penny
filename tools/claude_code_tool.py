#!/usr/bin/env python3
"""Claude Code CLI as a Hermes tool, jailed to the Hermes home directory.

Lets Penny invoke the ``claude`` CLI on her own installation for inspection
and reasoning. Two structural guarantees, both enforced in Python before any
subprocess starts (never by prompt text or by trusting the CLI):

  * FILESYSTEM JAIL — every invocation is confined to ``HERMES_HOME``
    (``get_hermes_home()``). Absolute paths outside the root, ``..``
    traversal, and symlinks resolving outside the root are all rejected by
    resolving real paths (see ``_resolve_jailed_cwd``) before the CLI ever
    runs. The CLI itself is additionally launched with ``--restricted``
    and ``--add-dir <jail>`` so its own file tools are confined too — belt
    and suspenders, not a substitute for the Python check.

  * READ VS WRITE — "read-only" is a property of what the CLI's tools can
    DO, not of how it is invoked, so this does not try to classify the
    prompt or trust an instruction. Instead ``mode="read_only"`` launches
    the CLI with a tool allowlist that contains no write-capable, shell,
    or network tool (``Read,Glob,Grep`` only) — it is structurally unable
    to change state. ``mode="write"`` allows ``Edit,Write,Bash`` and
    requires a Teams/gateway approval (via ``tools.approval.
    request_tool_approval``, reused unmodified) before the subprocess
    starts. Any error in the jail check or the approval call fails CLOSED
    — execution never proceeds on an exception.

Disabled by default: ``check_claude_code_requirements`` requires
``claude_code.enabled: true`` in config.yaml, so the tool does not appear
in any tool listing until an operator opts in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

from tools.path_security import validate_within_dir
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("hermes.audit.claude_code")

# ---------------------------------------------------------------------------
# Resource bounds
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT_SEC = 300
MAX_TIMEOUT_SEC = 900
MIN_TIMEOUT_SEC = 10
MAX_OUTPUT_CHARS = 20_000

# Tool allowlists passed to the CLI's ``--restricted --tools`` gate.
# Read-only: no Edit/Write/Bash/WebFetch/WebSearch — structurally cannot
# change state, install anything, or reach the network.
_READ_ONLY_TOOLS = "Read,Glob,Grep"
# Write-class: adds Edit/Write/Bash. Gated behind Teams approval below.
_WRITE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"

_VALID_MODES = ("read_only", "write")


# ---------------------------------------------------------------------------
# Availability gate — disabled by default
# ---------------------------------------------------------------------------

def check_claude_code_requirements() -> bool:
    """Disabled unless ``claude_code.enabled: true`` in config.yaml, and only
    when the ``claude`` binary is actually resolvable on PATH.

    To enable: set ``claude_code:\\n  enabled: true`` in config.yaml.
    """
    try:
        from hermes_cli.config import load_config, cfg_get
        cfg = load_config()
        enabled = cfg_get(cfg, "claude_code", "enabled", default=False)
    except Exception:
        logger.debug("claude_code_tool: config read failed", exc_info=True)
        return False
    if not isinstance(enabled, bool) or not enabled:
        return False
    return shutil.which("claude") is not None


# ---------------------------------------------------------------------------
# Jail enforcement
# ---------------------------------------------------------------------------

def _resolve_jailed_cwd(cwd_arg: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
    """Resolve *cwd_arg* to a real path guaranteed inside the Hermes jail.

    Returns ``(path, None)`` on success or ``(None, error_message)`` on any
    escape attempt or failure. Defends against:
      * an absolute path outside the jail root
      * ``..`` traversal components
      * a symlink (inside or outside the jail) that resolves outside it
    by resolving the REAL path (``Path.resolve()`` follows symlinks and
    normalizes ``..``) and checking it is still under the resolved root,
    via the same ``validate_within_dir`` helper other jailed tools use.
    """
    try:
        root = get_hermes_home().resolve()
    except Exception as exc:
        return None, f"Failed to resolve jail root: {exc}"

    if not cwd_arg:
        return root, None

    raw = str(cwd_arg).strip()
    if not raw:
        return root, None

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        error = validate_within_dir(candidate, root)
    except Exception as exc:
        # Fail closed: any exception during the safety check is a refusal,
        # never a pass-through to execution.
        return None, f"Jail check failed, refusing: {exc}"
    if error:
        return None, error

    resolved = candidate.resolve()
    if not resolved.is_dir():
        return None, f"Working directory does not exist inside the jail: {resolved}"
    return resolved, None


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def _audit(event: str, **fields) -> None:
    """Structured audit log line. Regulated environment: every invocation,
    approval decision, and outcome is logged, never swallowed.
    """
    try:
        audit_logger.info(json.dumps({"event": event, "ts": time.time(), **fields},
                                      ensure_ascii=False, default=str))
    except Exception:
        logger.warning("claude_code_tool: audit log failed for event %s", event, exc_info=True)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text) - limit} more chars]"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def claude_code_tool(
    prompt: str,
    cwd: Optional[str] = None,
    mode: str = "read_only",
    timeout_sec: Optional[int] = None,
) -> str:
    """Invoke the Claude Code CLI, jailed to HERMES_HOME.

    ``mode="read_only"`` (default) runs with no write/shell/network tool
    available and needs no approval. ``mode="write"`` requires Teams/gateway
    approval before the subprocess starts.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return tool_error("prompt is required.")

    mode = (mode or "read_only").strip()
    if mode not in _VALID_MODES:
        return tool_error(f"mode must be one of {_VALID_MODES}, got {mode!r}.")

    try:
        timeout = int(timeout_sec) if timeout_sec else DEFAULT_TIMEOUT_SEC
    except (TypeError, ValueError):
        return tool_error("timeout_sec must be an integer number of seconds.")
    timeout = max(MIN_TIMEOUT_SEC, min(timeout, MAX_TIMEOUT_SEC))

    # Re-check the disabled-by-default gate defensively — get_definitions()
    # filters on check_fn, but a direct call path (tests, future callers)
    # must not be able to skip it.
    if not check_claude_code_requirements():
        return tool_error("claude_code tool is disabled. Set claude_code.enabled: true "
                           "in config.yaml to enable it.")

    # --- Jail check. Any failure here fails CLOSED. ---
    try:
        jailed_cwd, jail_error = _resolve_jailed_cwd(cwd)
    except Exception as exc:
        _audit("jail_check_error", prompt=prompt[:500], cwd=cwd, error=str(exc))
        return tool_error(f"Jail check raised an error; refusing to run. ({exc})")

    if jail_error:
        _audit("jail_violation", prompt=prompt[:500], cwd=cwd, reason=jail_error)
        return tool_error(f"Refused: {jail_error}")

    jail_root = get_hermes_home().resolve()

    try:
        from tools.approval import get_current_session_key
        session_key = get_current_session_key()
    except Exception:
        session_key = "unknown"

    _audit(
        "invocation_requested",
        session_key=session_key,
        mode=mode,
        prompt=prompt[:2000],
        cwd=str(jailed_cwd),
        jail_root=str(jail_root),
    )

    # --- Approval gate for anything that can change state. ---
    approved_by_note = "not required (read_only)"
    if mode == "write":
        reason = (
            f"Claude Code write-class invocation requested.\n"
            f"Prompt: {prompt[:1500]}\n"
            f"Working directory: {jailed_cwd}\n"
            f"Confined to: {jail_root}\n"
            f"May: modify files, run shell commands, install packages, "
            f"or make network calls — all confined to the jail above."
        )
        rule_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        try:
            from tools.approval import request_tool_approval
            result = request_tool_approval(
                "claude_code",
                reason,
                rule_key=f"claude_code:write:{rule_hash}",
            )
        except Exception as exc:
            # Fail closed: an error in the approval path must never be
            # treated as an approval.
            _audit("approval_error", session_key=session_key, error=str(exc))
            return tool_error(f"Approval gate failed; refusing to run. ({exc})")

        if not result.get("approved"):
            _audit(
                "approval_denied",
                session_key=session_key,
                message=result.get("message"),
            )
            return tool_error(
                result.get("message") or "Denied: write-class Claude Code invocation "
                "was not approved."
            )
        approved_by_note = "approved via gateway/Teams approval gate"
        _audit("approval_granted", session_key=session_key)

    # --- Build and run the jailed subprocess. ---
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return tool_error("claude CLI not found on PATH.")

    tools_list = _WRITE_TOOLS if mode == "write" else _READ_ONLY_TOOLS
    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "json",
        "--add-dir", str(jail_root),
        "--restricted",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--tools", tools_list,
    ]
    if mode == "write":
        # acceptEdits: no interactive per-edit prompt (there is no TTY in
        # -p/print mode to answer one). --restricted still refuses
        # bypassPermissions outright, which is the guarantee we want —
        # the human approval already happened above, this just keeps the
        # single non-interactive run from hanging on a prompt nobody can see.
        cmd += ["--permission-mode", "acceptEdits"]

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(jailed_cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        _audit("timeout", session_key=session_key, mode=mode, timeout_sec=timeout,
               duration_sec=round(duration, 2))
        return tool_error(f"Claude Code invocation timed out after {timeout}s.")
    except Exception as exc:
        # Fail closed on any subprocess-launch error too.
        _audit("execution_error", session_key=session_key, mode=mode, error=str(exc))
        return tool_error(f"Claude Code invocation failed to run: {exc}")

    duration = time.monotonic() - start
    stdout = _truncate(proc.stdout or "")
    stderr = _truncate(proc.stderr or "")

    _audit(
        "completed",
        session_key=session_key,
        mode=mode,
        cwd=str(jailed_cwd),
        exit_code=proc.returncode,
        duration_sec=round(duration, 2),
        approval=approved_by_note,
        result_preview=_truncate(stdout, 500),
    )

    return json.dumps({
        "exit_code": proc.returncode,
        "mode": mode,
        "cwd": str(jailed_cwd),
        "jail_root": str(jail_root),
        "stdout": stdout,
        "stderr": stderr,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Schema + registration
# ---------------------------------------------------------------------------

CLAUDE_CODE_SCHEMA = {
    "name": "claude_code",
    "description": (
        "Invoke the Claude Code CLI against Penny's own Hermes installation, "
        "jailed to the Hermes home directory (HERMES_HOME). Use this to "
        "inspect, search, or reason about Hermes's own code and config. "
        "mode='read_only' (default) has no Edit/Write/Bash/network tool "
        "available and runs without approval. mode='write' can modify "
        "files, run shell commands, install packages, or make network "
        "calls (still confined to the jail) and REQUIRES a Teams/gateway "
        "approval before it runs — expect it to block until a human "
        "approves or denies."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The prompt/task to give the Claude Code CLI.",
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory, relative to or absolute within "
                    "HERMES_HOME. Defaults to HERMES_HOME itself. Any path "
                    "outside the jail (absolute escape, '..' traversal, or "
                    "a symlink resolving outside) is refused."
                ),
            },
            "mode": {
                "type": "string",
                "enum": list(_VALID_MODES),
                "description": (
                    "'read_only' (default, no approval needed, structurally "
                    "cannot write) or 'write' (requires Teams/gateway "
                    "approval first)."
                ),
            },
            "timeout_sec": {
                "type": "integer",
                "description": (
                    f"Wall-clock timeout in seconds "
                    f"({MIN_TIMEOUT_SEC}-{MAX_TIMEOUT_SEC}, default {DEFAULT_TIMEOUT_SEC})."
                ),
            },
        },
        "required": ["prompt"],
    },
}


registry.register(
    name="claude_code",
    toolset="claude_code",
    schema=CLAUDE_CODE_SCHEMA,
    handler=lambda args, **kw: claude_code_tool(
        prompt=args.get("prompt", ""),
        cwd=args.get("cwd"),
        mode=args.get("mode", "read_only"),
        timeout_sec=args.get("timeout_sec"),
    ),
    check_fn=check_claude_code_requirements,
    emoji="🛠️",
    max_result_size_chars=MAX_OUTPUT_CHARS * 2 + 2000,
)
