"""Tests for tools.claude_code_tool — the jailed, approval-gated Claude Code CLI tool.

Covers:
  * filesystem jail escapes (absolute path, '..' traversal, symlink, cwd arg)
  * read-only runs without approval
  * write-class requires approval and does not run when denied
  * an internal error in the jail or approval path fails closed
  * timeout and oversized-output handling
  * disabled-by-default gating
"""

import json
import subprocess

import pytest

import tools.claude_code_tool as cct


@pytest.fixture(autouse=True)
def _enable_tool(monkeypatch):
    """Most tests exercise the handler directly, past the disabled-by-default
    gate — the gate itself is tested separately below.
    """
    monkeypatch.setattr(cct, "check_claude_code_requirements", lambda: True)
    monkeypatch.setattr(cct.shutil, "which", lambda name: "/usr/bin/claude")
    yield


@pytest.fixture
def jail_root(tmp_path, monkeypatch):
    root = tmp_path / "hermes_home"
    root.mkdir()
    monkeypatch.setattr(cct, "get_hermes_home", lambda: root)
    return root


def _fake_completed(stdout="ok", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                        stdout=stdout, stderr=stderr)


class TestFilesystemJail:
    def test_absolute_path_outside_jail_refused(self, jail_root, monkeypatch, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        called = {"ran": False}
        monkeypatch.setattr(cct.subprocess, "run", lambda *a, **k: called.update(ran=True) or _fake_completed())

        result = cct.claude_code_tool(prompt="hi", cwd=str(outside), mode="read_only")

        assert "Refused" in result or "error" in result.lower()
        assert called["ran"] is False

    def test_dotdot_traversal_refused(self, jail_root, monkeypatch):
        called = {"ran": False}
        monkeypatch.setattr(cct.subprocess, "run", lambda *a, **k: called.update(ran=True) or _fake_completed())

        result = cct.claude_code_tool(prompt="hi", cwd="../../etc", mode="read_only")

        assert "Refused" in result or "error" in result.lower()
        assert called["ran"] is False

    def test_symlink_escape_refused(self, jail_root, monkeypatch, tmp_path):
        outside = tmp_path / "secret_outside"
        outside.mkdir()
        link = jail_root / "link_out"
        link.symlink_to(outside, target_is_directory=True)
        called = {"ran": False}
        monkeypatch.setattr(cct.subprocess, "run", lambda *a, **k: called.update(ran=True) or _fake_completed())

        result = cct.claude_code_tool(prompt="hi", cwd="link_out", mode="read_only")

        assert "Refused" in result or "error" in result.lower()
        assert called["ran"] is False

    def test_cwd_argument_absolute_escape_refused(self, jail_root, monkeypatch, tmp_path):
        # A sibling directory that happens to share the jail root as a
        # string prefix (e.g. hermes_home_evil) must not pass a naive
        # startswith() check — only real containment counts.
        evil = tmp_path / (jail_root.name + "_evil")
        evil.mkdir()
        called = {"ran": False}
        monkeypatch.setattr(cct.subprocess, "run", lambda *a, **k: called.update(ran=True) or _fake_completed())

        result = cct.claude_code_tool(prompt="hi", cwd=str(evil), mode="read_only")

        assert "Refused" in result or "error" in result.lower()
        assert called["ran"] is False

    def test_relative_path_inside_jail_allowed(self, jail_root, monkeypatch):
        sub = jail_root / "scratch"
        sub.mkdir()
        captured = {}

        def fake_run(cmd, cwd=None, **kw):
            captured["cwd"] = cwd
            return _fake_completed()

        monkeypatch.setattr(cct.subprocess, "run", fake_run)

        result = cct.claude_code_tool(prompt="hi", cwd="scratch", mode="read_only")

        assert captured["cwd"] == str(sub.resolve())
        assert json.loads(result)["exit_code"] == 0


class TestReadVsWrite:
    def test_read_only_runs_without_approval(self, jail_root, monkeypatch):
        def fail_approval(*a, **k):
            pytest.fail("approval should not be requested for read_only mode")

        monkeypatch.setattr("tools.approval.request_tool_approval", fail_approval)
        monkeypatch.setattr(cct.subprocess, "run", lambda *a, **k: _fake_completed())

        result = cct.claude_code_tool(prompt="inspect the code", mode="read_only")

        parsed = json.loads(result)
        assert parsed["exit_code"] == 0

    def test_read_only_tool_allowlist_has_no_write_tools(self, jail_root, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _fake_completed()

        monkeypatch.setattr(cct.subprocess, "run", fake_run)
        cct.claude_code_tool(prompt="hi", mode="read_only")

        tools_idx = captured["cmd"].index("--tools") + 1
        tools_arg = captured["cmd"][tools_idx]
        for forbidden in ("Bash", "Edit", "Write", "WebFetch", "WebSearch"):
            assert forbidden not in tools_arg.split(",")

    def test_write_mode_requires_approval_before_running(self, jail_root, monkeypatch):
        calls = {"approval": False, "subprocess": False}

        def fake_approval(tool_name, reason, **kw):
            calls["approval"] = True
            assert tool_name == "claude_code"
            assert "may modify files" in reason.lower() or "run shell commands" in reason.lower()
            return {"approved": True, "message": None}

        def fake_run(cmd, **kw):
            calls["subprocess"] = True
            return _fake_completed()

        monkeypatch.setattr("tools.approval.request_tool_approval", fake_approval)
        monkeypatch.setattr(cct.subprocess, "run", fake_run)

        result = cct.claude_code_tool(prompt="fix the bug", mode="write")

        assert calls["approval"] is True
        assert calls["subprocess"] is True
        assert json.loads(result)["exit_code"] == 0

    def test_write_mode_denied_does_not_run(self, jail_root, monkeypatch):
        def fake_approval(*a, **k):
            return {"approved": False, "message": "denied by Ernesto"}

        def fail_run(*a, **k):
            pytest.fail("subprocess must not run when approval is denied")

        monkeypatch.setattr("tools.approval.request_tool_approval", fake_approval)
        monkeypatch.setattr(cct.subprocess, "run", fail_run)

        result = cct.claude_code_tool(prompt="delete something", mode="write")

        assert "denied by Ernesto" in result

    def test_write_mode_tool_allowlist_includes_edit_bash(self, jail_root, monkeypatch):
        captured = {}

        def fake_approval(*a, **k):
            return {"approved": True, "message": None}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _fake_completed()

        monkeypatch.setattr("tools.approval.request_tool_approval", fake_approval)
        monkeypatch.setattr(cct.subprocess, "run", fake_run)
        cct.claude_code_tool(prompt="hi", mode="write")

        tools_idx = captured["cmd"].index("--tools") + 1
        tools_arg = captured["cmd"][tools_idx].split(",")
        for expected in ("Bash", "Edit", "Write"):
            assert expected in tools_arg


class TestFailClosed:
    def test_jail_check_internal_error_fails_closed(self, jail_root, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(cct, "_resolve_jailed_cwd", boom)
        called = {"ran": False}
        monkeypatch.setattr(cct.subprocess, "run", lambda *a, **k: called.update(ran=True) or _fake_completed())

        result = cct.claude_code_tool(prompt="hi", mode="read_only")

        assert called["ran"] is False
        assert "error" in result.lower() or "refus" in result.lower()

    def test_approval_internal_error_fails_closed(self, jail_root, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("gateway unreachable")

        monkeypatch.setattr("tools.approval.request_tool_approval", boom)
        called = {"ran": False}
        monkeypatch.setattr(cct.subprocess, "run", lambda *a, **k: called.update(ran=True) or _fake_completed())

        result = cct.claude_code_tool(prompt="hi", mode="write")

        assert called["ran"] is False
        assert "error" in result.lower() or "refus" in result.lower()


class TestResourceBounds:
    def test_timeout_fires(self, jail_root, monkeypatch):
        def fake_run(cmd, timeout=None, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        monkeypatch.setattr(cct.subprocess, "run", fake_run)

        result = cct.claude_code_tool(prompt="hi", mode="read_only", timeout_sec=15)

        assert "timed out" in result.lower()

    def test_oversized_output_truncated(self, jail_root, monkeypatch):
        huge = "x" * (cct.MAX_OUTPUT_CHARS + 5000)
        monkeypatch.setattr(cct.subprocess, "run", lambda *a, **k: _fake_completed(stdout=huge))

        result = cct.claude_code_tool(prompt="hi", mode="read_only")
        parsed = json.loads(result)

        assert len(parsed["stdout"]) < len(huge)
        assert "truncated" in parsed["stdout"]

    def test_timeout_sec_clamped_to_max(self, jail_root, monkeypatch):
        captured = {}

        def fake_run(cmd, timeout=None, **kw):
            captured["timeout"] = timeout
            return _fake_completed()

        monkeypatch.setattr(cct.subprocess, "run", fake_run)
        cct.claude_code_tool(prompt="hi", mode="read_only", timeout_sec=999999)

        assert captured["timeout"] == cct.MAX_TIMEOUT_SEC


class TestDisabledByDefault:
    def test_disabled_when_config_unset(self, monkeypatch):
        # Do NOT use the autouse override — exercise the real check_fn.
        import importlib
        importlib.reload(cct)

        def fake_load_config():
            return {}

        def fake_cfg_get(cfg, *keys, default=None):
            return default

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)
        monkeypatch.setattr("hermes_cli.config.cfg_get", fake_cfg_get)

        assert cct.check_claude_code_requirements() is False

        result = cct.claude_code_tool(prompt="hi", mode="read_only")
        assert "disabled" in result.lower()

    def test_enabled_when_config_true_and_binary_present(self, monkeypatch):
        def fake_load_config():
            return {"claude_code": {"enabled": True}}

        def fake_cfg_get(cfg, section, key, default=None):
            return cfg.get(section, {}).get(key, default)

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)
        monkeypatch.setattr("hermes_cli.config.cfg_get", fake_cfg_get)
        monkeypatch.setattr(cct.shutil, "which", lambda name: "/usr/bin/claude")

        assert cct.check_claude_code_requirements() is True
