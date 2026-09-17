"""Branch-state guard: HEAD must still carry the Penny ticket-card wiring.

A bad hermes update can drop plugins/platforms/teams/ticket_card.py or the
_apply_route_card call site in gateway/platforms/webhook.py, silently
breaking Teams card delivery. This script fails loudly when that happens.

Exit codes: 0 healthy, 1 unhealthy, 2 unhealthy under --post-update (after
printing CARD WIRING REGRESSED to stderr).
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

# Resolved from the short WIP SHA 00d199269f via `git rev-parse`. Hardcoded
# (not resolved at runtime) so the guard cannot be fooled by a rewrite that
# reuses or moves the short name; an update regressing past this commit
# trips check 3.
PENNY_PIN_SHA = "00d199269f2a3796c75a55a47071475b22b07db3"

TICKET_CARD_REL = Path("plugins/platforms/teams/ticket_card.py")
WEBHOOK_REL = Path("gateway/platforms/webhook.py")


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def repo_root_from_cwd(start: Path) -> Path:
    return Path(_git(start, "rev-parse", "--show-toplevel"))


def check_ticket_card(repo_root: Path) -> str | None:
    path = repo_root / TICKET_CARD_REL
    if not path.exists():
        return f"missing {TICKET_CARD_REL}"
    # Load by file path so the module executes (catches syntax/name errors)
    # without requiring a full package layout.
    spec = importlib.util.spec_from_file_location("ticket_card", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as err:  # noqa: BLE001 - any failure means broken wiring
        return f"{TICKET_CARD_REL} fails to import: {err}"
    source = path.read_text(encoding="utf-8")
    for symbol in ("parse_verdict", "build_ticket_card"):
        if symbol not in source:
            return f"{TICKET_CARD_REL} does not define {symbol}"
    return None


def check_webhook_call_site(repo_root: Path) -> str | None:
    source = repo_root / WEBHOOK_REL
    if not source.exists():
        return f"missing {WEBHOOK_REL}"
    if "_apply_route_card" not in source.read_text(encoding="utf-8"):
        return f"{WEBHOOK_REL} lacks _apply_route_card"
    return None


def check_penny_ancestor(repo_root: Path, pin_sha: str | None = None) -> str | None:
    pin_sha = pin_sha or PENNY_PIN_SHA
    probe = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", pin_sha, "HEAD"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return f"pin {pin_sha[:10]} is not an ancestor of HEAD"
    return None


def run_checks(repo_root: Path, pin_sha: str | None = None) -> list[str]:
    failures = [
        reason
        for reason in (
            check_ticket_card(repo_root),
            check_webhook_call_site(repo_root),
            check_penny_ancestor(repo_root, pin_sha),
        )
        if reason is not None
    ]
    return failures


def main(argv: list[str] | None = None, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--post-update", action="store_true")
    parser.add_argument("repo", nargs="?", help="repo root (default: git toplevel of cwd)")
    args = parser.parse_args(argv)

    try:
        root = Path(args.repo) if args.repo else (repo_root or repo_root_from_cwd(Path.cwd()))
    except subprocess.CalledProcessError:
        print("UNHEALTHY: cwd is not inside a git repository", file=sys.stderr)
        return 2 if args.post_update else 1
    failures = run_checks(root)
    for reason in failures:
        print(f"UNHEALTHY: {reason}", file=sys.stderr)
    if failures:
        if args.post_update:
            print("CARD WIRING REGRESSED", file=sys.stderr)
            return 2
        return 1
    print("card wiring OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
