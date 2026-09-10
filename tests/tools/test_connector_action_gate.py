"""Tests for the MSP connector security gate (tools/connector_action_gate.py).

Covers the six cases required by the ticket:
  1. a READ tool executes with no approval
  2. an ACTION tool does NOT execute without approval
  3. an unknown tool name is treated as ACTION (default-deny)
  4. a denied approval means the call never happens
  5. an approval from a non-approver AAD id is rejected
  6. the gate failing internally blocks rather than allows

Each is written to fail before the gate existed and pass after -- see the
implementer's report for the exact before/after pytest output.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools import connector_action_gate as gate


# ---------------------------------------------------------------------------
# 1 & 3: classification -- READ auto-executes, unknown/ACTION default-deny.
# ---------------------------------------------------------------------------

class TestClassification:
    def test_known_read_tool_classifies_read(self):
        assert gate.classify_connector_tool("cw_get_ticket") == gate.READ
        assert gate.classify_connector_tool("ninjaone_devices_list") == gate.READ
        assert gate.classify_connector_tool("threatlocker_status") == gate.READ

    def test_known_action_tool_classifies_action(self):
        assert gate.classify_connector_tool("cipp_disable_user") == gate.ACTION
        assert gate.classify_connector_tool("ninjaone_devices_reboot") == gate.ACTION
        assert gate.classify_connector_tool("cw_create_ticket") == gate.ACTION

    def test_unknown_tool_in_governed_family_defaults_to_action(self):
        """Default-deny: a made-up tool name inside a governed family is
        ACTION, never READ, even though it appears on no explicit list."""
        made_up = "cipp_totally_new_write_tool"
        assert made_up not in gate._READ_TOOLS
        assert made_up not in gate._ACTION_TOOLS
        assert gate.classify_connector_tool(made_up) == gate.ACTION

    def test_unknown_tool_outside_any_governed_family_is_ungated(self):
        """A name that isn't even a connector tool (file/terminal/etc.) is
        not this gate's business -- returns None so the caller does not
        block an unrelated tool."""
        assert gate.classify_connector_tool("write_file") is None
        assert gate.classify_connector_tool("terminal_execute") is None

    def test_paylocity_all_action_despite_read_shaped_names(self):
        # Owner's instruction: classify all paylocity_* as ACTION for now,
        # regardless of "list"/"get" naming, because the live schemas return
        # pay/bank data.
        assert gate.classify_connector_tool("paylocity_employees_list") == gate.ACTION
        assert gate.classify_connector_tool("paylocity_employees_get") == gate.ACTION

    def test_auth_key_tool_is_action_despite_get_name(self):
        assert gate.classify_connector_tool(
            "threatlocker_organizations_get_auth_key"
        ) == gate.ACTION

    def test_suffix_matching_ignores_wire_prefix(self):
        """Coordinator requirement: bare name, mcp__atlas_*__ name, and
        mcp__connectwise_manage__ name for the SAME underlying tool must
        resolve to the same verdict, so a differently-named server entry
        wrapping the same tool can't bypass the gate."""
        bare = "cw_get_ticket"
        atlas_prefixed = "mcp__atlas_connectwise__cw_get_ticket"
        cw_manage_prefixed = "mcp__connectwise_manage__cw_get_ticket"

        verdicts = {
            gate.classify_connector_tool(bare),
            gate.classify_connector_tool(atlas_prefixed),
            gate.classify_connector_tool(cw_manage_prefixed),
        }
        assert verdicts == {gate.READ}

        action_bare = "cipp_disable_user"
        action_prefixed = "mcp__atlas_cipp__cipp_disable_user"
        action_verdicts = {
            gate.classify_connector_tool(action_bare),
            gate.classify_connector_tool(action_prefixed),
        }
        assert action_verdicts == {gate.ACTION}

    def test_namespaced_unknown_tool_is_action(self):
        """Same as test_unknown_tool_in_governed_family_defaults_to_action
        but through the namespaced wire name, per coordinator's example."""
        assert gate.classify_connector_tool(
            "mcp__atlas_cipp__cipp_totally_new_write_tool"
        ) == gate.ACTION


# ---------------------------------------------------------------------------
# 5: approver identity -- config-driven, AAD-object-id, fail-closed on
#    unresolved placeholders.
# ---------------------------------------------------------------------------

class TestApproverAllowlist:
    def test_unresolved_placeholder_never_matches(self):
        with patch.object(
            gate, "_load_approver_config",
            return_value={"Ernesto": "UNRESOLVED_AAD_ID_ERNESTO"},
        ):
            assert gate.is_authorized_approver("UNRESOLVED_AAD_ID_ERNESTO") is False
            assert gate.is_authorized_approver("") is False
            assert gate.is_authorized_approver("some-random-aad-id") is False

    def test_resolved_id_matches(self):
        with patch.object(
            gate, "_load_approver_config",
            return_value={"Jerry": "11111111-2222-3333-4444-555555555555"},
        ):
            assert gate.is_authorized_approver(
                "11111111-2222-3333-4444-555555555555"
            ) is True

    def test_non_approver_id_rejected(self):
        """An approval click from a real, resolved AAD id that is simply
        not on the approver allowlist is rejected -- not everyone who can
        talk to Penny can approve a connector write."""
        with patch.object(
            gate, "_load_approver_config",
            return_value={"Jerry": "11111111-2222-3333-4444-555555555555"},
        ):
            assert gate.is_authorized_approver(
                "99999999-8888-7777-6666-555555555555"
            ) is False

    def test_config_read_failure_fails_closed(self):
        """A broken config read must not be read as 'no allowlist, allow
        everyone' -- it falls back to the unresolved built-in defaults,
        which match nobody."""
        with patch(
            "hermes_cli.config.load_config_readonly",
            side_effect=RuntimeError("boom"),
        ):
            allowlist = gate._load_approver_config()
            assert all(
                v.startswith(gate.UNRESOLVED_PREFIX) for v in allowlist.values()
            )
            assert gate.is_authorized_approver("anything") is False


# ---------------------------------------------------------------------------
# 2, 4, 6: the blocking approval request itself.
# ---------------------------------------------------------------------------

class TestRequestConnectorActionApproval:
    def test_denied_approval_means_call_never_happens(self):
        with patch(
            "tools.approval._gateway_notify_cbs", {"sess-1": lambda data: None}
        ), patch(
            "tools.approval._await_gateway_decision",
            return_value={"resolved": True, "choice": "deny", "reason": None},
        ), patch.object(gate, "record_audit_event") as audit:
            approved, outcome = gate.request_connector_action_approval(
                session_key="sess-1",
                server_name="atlas-ninjaone",
                tool_name="ninjaone_devices_reboot",
                arguments={"deviceId": "42"},
            )
        assert approved is False
        assert outcome == "denied"
        audit.assert_called_once()
        assert audit.call_args.kwargs["outcome"] == "denied"

    def test_timeout_means_call_never_happens(self):
        with patch(
            "tools.approval._gateway_notify_cbs", {"sess-2": lambda data: None}
        ), patch(
            "tools.approval._await_gateway_decision",
            return_value={"resolved": False, "choice": None, "reason": None},
        ):
            approved, outcome = gate.request_connector_action_approval(
                session_key="sess-2",
                server_name="atlas-cipp",
                tool_name="cipp_reset_password",
                arguments={"userId": "someone@henssler.com"},
            )
        assert approved is False
        assert outcome == "timeout"

    def test_no_gateway_notify_channel_fails_closed(self):
        """No session is listening for approval prompts (e.g. cron/unattended
        context) -- must fail closed, not silently act."""
        with patch("tools.approval._gateway_notify_cbs", {}):
            approved, outcome = gate.request_connector_action_approval(
                session_key="no-such-session",
                server_name="atlas-ninjaone",
                tool_name="ninjaone_devices_reboot",
                arguments={"deviceId": "1"},
            )
        assert approved is False
        assert outcome == "error"

    def test_approval_wait_raising_fails_closed(self):
        """The gate failing internally (an exception mid-approval-wait) must
        block, not allow -- opposite of the outbound-dedup fail-open guard."""
        with patch(
            "tools.approval._gateway_notify_cbs", {"sess-3": lambda data: None}
        ), patch(
            "tools.approval._await_gateway_decision",
            side_effect=RuntimeError("boom"),
        ):
            approved, outcome = gate.request_connector_action_approval(
                session_key="sess-3",
                server_name="atlas-cipp",
                tool_name="cipp_disable_user",
                arguments={"userId": "x@henssler.com"},
            )
        assert approved is False
        assert outcome == "error"

    def test_approved_call_records_audit_with_approver(self):
        with patch(
            "tools.approval._gateway_notify_cbs", {"sess-4": lambda data: None}
        ), patch(
            "tools.approval._await_gateway_decision",
            return_value={
                "resolved": True, "choice": "once",
                "reason": "approved_by:Jerry",
            },
        ), patch.object(gate, "record_audit_event") as audit:
            approved, outcome = gate.request_connector_action_approval(
                session_key="sess-4",
                server_name="atlas-ninjaone",
                tool_name="ninjaone_devices_reboot",
                arguments={"deviceId": "42"},
            )
        assert approved is True
        assert outcome == "approved"
        assert audit.call_args.kwargs["approved_by"] == "Jerry"

    def test_gate_import_failure_in_mcp_handler_fails_closed(self):
        """Simulates the call-site try/except in tools/mcp_tool.py: if
        classify/import itself raises, the handler must return an error
        (block) rather than proceed to the real MCP call."""
        with patch.object(
            gate, "classify_connector_tool", side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError):
                gate.classify_connector_tool("cw_get_ticket")
        # The real fail-closed behavior at the call site is exercised in
        # TestMcpHandlerGate below via the actual handler.


# ---------------------------------------------------------------------------
# End-to-end through the real call site in tools/mcp_tool.py.
# ---------------------------------------------------------------------------

class TestMcpHandlerGate:
    """Exercises tools.mcp_tool._make_tool_handler's gate integration
    directly, without a live MCP server -- server/session plumbing below the
    gate is irrelevant to what's under test here (whether the handler even
    reaches it)."""

    def _make_handler(self, tool_name: str, server_name: str = "atlas-ninjaone"):
        import tools.mcp_tool as mcp_tool
        return mcp_tool._make_tool_handler(server_name, tool_name, tool_timeout=5.0)

    def test_read_tool_executes_with_no_approval(self):
        """A READ tool must reach past the gate to the connection lookup
        (which fails here for lack of a live server -- proving the gate did
        NOT block it; if it had, the error text would name the gate, not
        'is not connected')."""
        handler = self._make_handler("ninjaone_devices_list")
        with patch("tools.mcp_tool._get_connected_server_for_call", return_value=None):
            result = handler({})
        assert "not connected" in result
        assert "approval" not in result.lower()

    def test_action_tool_does_not_execute_without_approval(self):
        handler = self._make_handler("ninjaone_devices_reboot")
        with patch(
            "tools.connector_action_gate.request_connector_action_approval",
            return_value=(False, "denied"),
        ), patch(
            "tools.mcp_tool._get_connected_server_for_call"
        ) as get_server:
            result = handler({"deviceId": "1"})
        get_server.assert_not_called()
        assert "requires express approval" in result or "not approved" in result

    def test_action_tool_executes_after_approval(self):
        handler = self._make_handler("ninjaone_devices_reboot")
        with patch(
            "tools.connector_action_gate.request_connector_action_approval",
            return_value=(True, "approved"),
        ), patch(
            "tools.mcp_tool._get_connected_server_for_call", return_value=None,
        ) as get_server:
            result = handler({"deviceId": "1"})
        get_server.assert_called_once()
        assert "not connected" in result

    def test_unknown_namespaced_tool_blocks_as_action(self):
        handler = self._make_handler(
            "cipp_totally_new_write_tool", server_name="atlas-cipp",
        )
        with patch(
            "tools.connector_action_gate.request_connector_action_approval",
            return_value=(False, "denied"),
        ) as req, patch("tools.mcp_tool._get_connected_server_for_call") as get_server:
            handler({})
        req.assert_called_once()
        get_server.assert_not_called()

    def test_gate_internal_failure_blocks_the_call(self):
        """The gate itself failing (classify raises) must block, not allow."""
        handler = self._make_handler("ninjaone_devices_reboot")
        with patch(
            "tools.connector_action_gate.classify_connector_tool",
            side_effect=RuntimeError("boom"),
        ), patch("tools.mcp_tool._get_connected_server_for_call") as get_server:
            result = handler({"deviceId": "1"})
        get_server.assert_not_called()
        assert "blocked" in result.lower()
