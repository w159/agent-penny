"""Tests for the browser/computer_use/vision/tts capability extension of the
connector security gate (tools/connector_action_gate.py).

Mirrors tests/tools/test_connector_action_gate.py's structure: classification
first, then the handler-wrapping decorator that blocks an ACTION call on
express approval the same way an MSP connector ACTION does.
"""

from __future__ import annotations

from unittest.mock import patch

from tools import connector_action_gate as gate


class TestCapabilityClassification:
    def test_read_only_tools_classify_read(self):
        assert gate.classify_capability_tool("browser_navigate") == gate.READ
        assert gate.classify_capability_tool("browser_snapshot") == gate.READ
        assert gate.classify_capability_tool("vision_analyze") == gate.READ
        assert gate.classify_capability_tool("text_to_speech") == gate.READ

    def test_mutating_browser_tools_classify_action(self):
        assert gate.classify_capability_tool("browser_click") == gate.ACTION
        assert gate.classify_capability_tool("browser_type") == gate.ACTION
        assert gate.classify_capability_tool("browser_press") == gate.ACTION

    def test_computer_use_classifies_by_action_argument(self):
        assert gate.classify_capability_tool("computer_use", {"action": "screenshot"}) == gate.READ
        assert gate.classify_capability_tool("computer_use", {"action": "cursor_position"}) == gate.READ
        assert gate.classify_capability_tool("computer_use", {"action": "left_click"}) == gate.ACTION
        assert gate.classify_capability_tool("computer_use", {"action": "key"}) == gate.ACTION
        assert gate.classify_capability_tool("computer_use", {"action": "type"}) == gate.ACTION

    def test_computer_use_defaults_to_action_when_action_missing_or_unknown(self):
        # Default-deny: an unrecognized or absent action must never fall
        # back to READ just because it isn't on the known-safe list.
        assert gate.classify_capability_tool("computer_use", {}) == gate.ACTION
        assert gate.classify_capability_tool("computer_use", {"action": "some_future_gesture"}) == gate.ACTION

    def test_tool_outside_scope_returns_none(self):
        assert gate.classify_capability_tool("write_file") is None
        assert gate.classify_capability_tool("cw_get_ticket") is None


class TestCapabilityHandlerWrapper:
    def test_read_tool_executes_without_approval(self):
        calls = []

        def handler(args, **kw):
            calls.append(args)
            return "ok"

        wrapped = gate.require_capability_approval("browser", "browser_navigate")(handler)
        result = wrapped({"url": "https://example.com"})
        assert result == "ok"
        assert calls == [{"url": "https://example.com"}]

    def test_action_tool_does_not_execute_without_approval(self):
        calls = []

        def handler(args, **kw):
            calls.append(args)
            return "ok"

        wrapped = gate.require_capability_approval("browser", "browser_click")(handler)
        with patch.object(
            gate, "request_connector_action_approval",
            return_value=(False, "denied"),
        ):
            result = wrapped({"ref": "e1"})
        assert calls == []
        assert "requires express approval" in result

    def test_action_tool_executes_after_approval(self):
        calls = []

        def handler(args, **kw):
            calls.append(args)
            return "ok"

        wrapped = gate.require_capability_approval("browser", "browser_type")(handler)
        with patch.object(
            gate, "request_connector_action_approval",
            return_value=(True, "approved"),
        ):
            result = wrapped({"ref": "e1", "text": "hello"})
        assert calls == [{"ref": "e1", "text": "hello"}]
        assert result == "ok"

    def test_computer_use_click_gated_but_screenshot_is_not(self):
        calls = []

        def handler(args, **kw):
            calls.append(args)
            return "ok"

        wrapped = gate.require_capability_approval("computer_use", "computer_use")(handler)

        # A screenshot passes straight through -- no approval call at all.
        with patch.object(gate, "request_connector_action_approval") as mock_approve:
            result = wrapped({"action": "screenshot"})
            mock_approve.assert_not_called()
        assert result == "ok"

        # A click blocks pending approval.
        with patch.object(
            gate, "request_connector_action_approval",
            return_value=(False, "timeout"),
        ):
            result = wrapped({"action": "left_click", "coordinate": [10, 10]})
        assert len(calls) == 1  # only the screenshot ran
        assert "requires express approval" in result

    def test_gate_internal_failure_fails_closed(self):
        def handler(args, **kw):
            raise AssertionError("must not run when the gate itself fails")

        wrapped = gate.require_capability_approval("browser", "browser_click")(handler)
        with patch.object(
            gate, "classify_capability_tool", side_effect=RuntimeError("boom"),
        ):
            result = wrapped({"ref": "e1"})
        assert "blocked" in result
