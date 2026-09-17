#!/usr/bin/env bash
# Wrapper around `hermes update` that verifies Penny's Teams card wiring
# before and after. The four wiring-loss incidents (2026-06-30 through
# 2026-09-11) all came from update-time branch switches to main, which
# dirty-file checking never caught; this checks the wiring itself.
set -u
REPO="$HOME/.hermes/hermes-agent"
CHECK="$REPO/scripts/check_card_wiring.py"

run_check() {
    (cd "$REPO" && uv run python "$CHECK" --post-update)
}

echo "[update-guard] pre-update wiring check"
if ! run_check; then
    echo "[update-guard] wiring already unhealthy BEFORE update - aborting" >&2
    exit 2
fi

echo "[update-guard] running: hermes update $*"
hermes update "$@"
status=$?

echo "[update-guard] post-update wiring check"
if ! run_check; then
    echo "[update-guard] CARD WIRING REGRESSED after hermes update" >&2
    echo "[update-guard] recover with: git -C $REPO log -5; compare against tag safety/pre-card-restore-20260917-111154" >&2
    exit 2
fi

echo "[update-guard] wiring intact (hermes update exit: $status)"
exit "$status"
