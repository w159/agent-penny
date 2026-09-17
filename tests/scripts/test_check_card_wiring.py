"""Tests for scripts/check_card_wiring.py.

Fakes the repo state in tmp_path (plus a throwaway git repo for the
ancestry check) so the tests never depend on the real hermes checkout.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import check_card_wiring as ccw  # noqa: E402

TICKET_CARD_BODY = (
    "def parse_verdict(text):\n"
    "    return text.strip()\n"
    "\n"
    "def build_ticket_card(verdict):\n"
    "    return {'text': verdict}\n"
)
WEBHOOK_BODY = "def _apply_route_card(card):\n    return card\n"
FAR_PIN = "0" * 40


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "plugins/platforms/teams").mkdir(parents=True)
    (tmp_path / "gateway/platforms").mkdir(parents=True)
    (tmp_path / "plugins/platforms/teams/ticket_card.py").write_text(TICKET_CARD_BODY)
    (tmp_path / "gateway/platforms/webhook.py").write_text(WEBHOOK_BODY)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "wired")
    return tmp_path


def test_healthy_repo_passes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ccw, "PENNY_PIN_SHA", _git(repo, "rev-parse", "HEAD"))
    assert ccw.run_checks(repo) == []


def test_missing_ticket_card_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ccw, "PENNY_PIN_SHA", _git(repo, "rev-parse", "HEAD"))
    (repo / "plugins/platforms/teams/ticket_card.py").unlink()
    failures = ccw.run_checks(repo)
    assert len(failures) == 1
    assert "missing plugins/platforms/teams/ticket_card.py" in failures[0]


def test_missing_apply_route_card_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ccw, "PENNY_PIN_SHA", _git(repo, "rev-parse", "HEAD"))
    (repo / "gateway/platforms/webhook.py").write_text("def other():\n    pass\n")
    failures = ccw.run_checks(repo)
    assert len(failures) == 1
    assert "_apply_route_card" in failures[0]


def test_wrong_head_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate an update that lost the Penny commit: pin points at a SHA
    # that is not in this repo's HEAD ancestry.
    monkeypatch.setattr(ccw, "PENNY_PIN_SHA", FAR_PIN)
    failures = ccw.run_checks(repo)
    assert len(failures) == 1
    assert "not an ancestor of HEAD" in failures[0]


def test_post_update_exit_code(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(ccw, "PENNY_PIN_SHA", FAR_PIN)
    assert ccw.main(["--post-update"], repo_root=repo) == 2
    assert "CARD WIRING REGRESSED" in capsys.readouterr().err
