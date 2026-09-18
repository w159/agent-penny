"""MSP connector tool security gate.

Owner's rule (verbatim, from ticket): "all require approval and she's NEVER
allowed to act upon these tools without express approval; unless it's a read
action to include up to date information in a response or related ticket."

This module is the single choke point for that rule. It does three things:

  1. Classifies every connector tool call as READ (auto-execute) or ACTION
     (blocks on a named approver) via ``classify_connector_tool()``. Any tool
     name inside a governed connector family that is not on the READ list is
     ACTION -- default-deny, never default-read (see CONNECTOR_TOOL_CLASS).

  2. Checks the person clicking "approve" against a config-driven,
     AAD-object-id approver allowlist (``connector_approvals.approvers`` in
     config.yaml) -- NOT the general TEAMS_ALLOWED_USERS chat roster, which
     authorizes far more people to talk to Penny than are authorized to
     approve a connector write.

  3. Blocks the calling thread on a real human decision by reusing
     ``tools.approval``'s existing gateway-approval queue
     (``_await_gateway_decision`` / ``resolve_gateway_approval`` /
     ``has_blocking_approval``) -- it does not reimplement approval
     plumbing, per instruction. It also writes an audit-log line for every
     ACTION attempt regardless of outcome (FTC Safeguards / SEC Reg S-P /
     GLBA recordkeeping).

Tool-name matching is suffix-based, not exact-registry-name based: a
connector tool can reach the gate either as a bare name (``cw_get_ticket``,
used directly by tests and by handler closures inside tools/mcp_tool.py,
which already hold the bare name) or as a prefixed wire name
(``mcp__atlas_connectwise__cw_get_ticket``, ``mcp__connectwise_manage__cw_get_ticket``).
The SAME underlying tool must classify identically no matter which MCP
server entry wraps it, so classification strips any ``mcp__<server>__``
prefix and looks up the bare suffix -- never the full registry name.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

READ = "read"
ACTION = "action"

# ---------------------------------------------------------------------------
# Declarative classification -- kept as a Python constant, not an external
# YAML file. An examiner-facing security control should not be editable
# without a code change and its accompanying tests; a separate YAML file
# would let the allowlist drift out of sync with what this module's tests
# actually cover. Reviewed/edited here, in one place, under version control.
# ---------------------------------------------------------------------------

_READ_TOOLS: frozenset[str] = frozenset({
    # auvik -- everything (read-only connector, no write tools exist)
    "auvik_alerts_get", "auvik_alerts_list",
    "auvik_billing_client_usage", "auvik_billing_device_usage",
    "auvik_components_get", "auvik_components_list",
    "auvik_configurations_get", "auvik_configurations_list",
    "auvik_devices_get", "auvik_devices_get_details",
    "auvik_devices_get_extended", "auvik_devices_get_lifecycle",
    "auvik_devices_get_warranty", "auvik_devices_list",
    "auvik_devices_list_details", "auvik_devices_list_extended",
    "auvik_devices_list_lifecycle", "auvik_devices_list_warranty",
    "auvik_entities_get_audit", "auvik_entities_get_note",
    "auvik_entities_list_audits", "auvik_entities_list_notes",
    "auvik_interfaces_get", "auvik_interfaces_list",
    "auvik_navigate",
    "auvik_networks_get", "auvik_networks_get_detail",
    "auvik_networks_list", "auvik_networks_list_detail",
    "auvik_statistics_component", "auvik_statistics_device",
    "auvik_statistics_device_availability", "auvik_statistics_interface",
    "auvik_statistics_oid", "auvik_statistics_service",
    "auvik_status",
    "auvik_tenants_detail", "auvik_tenants_get_detail", "auvik_tenants_list",

    # blumira
    "blumira_navigate", "blumira_status",

    # cipp -- list/get/ping/status/bec_check only
    "cipp_bec_check",
    "cipp_get_tenant_alignment", "cipp_get_tenant_details",
    "cipp_get_tenant_drift", "cipp_get_version",
    "cipp_list_alert_queue", "cipp_list_audit_logs", "cipp_list_bpa",
    "cipp_list_conditional_access_policies", "cipp_list_csp_licenses",
    "cipp_list_domain_health", "cipp_list_gdap_invites",
    "cipp_list_gdap_roles", "cipp_list_groups", "cipp_list_licenses",
    "cipp_list_logs", "cipp_list_mailbox_permissions", "cipp_list_mailboxes",
    "cipp_list_mfa_users", "cipp_list_named_locations",
    "cipp_list_scheduled_items", "cipp_list_standard_templates",
    "cipp_list_standards", "cipp_list_tenants", "cipp_list_user_devices",
    "cipp_list_user_groups", "cipp_list_users",
    "cipp_ping", "cipp_status",

    # knowbe4 -- everything (read-only connector)
    "knowbe4_account_get", "knowbe4_account_risk_score_history",
    "knowbe4_back",
    "knowbe4_groups_get", "knowbe4_groups_list", "knowbe4_groups_members",
    "knowbe4_groups_risk_score_history",
    "knowbe4_navigate",
    "knowbe4_phishing_campaign_tests", "knowbe4_phishing_campaigns_get",
    "knowbe4_phishing_campaigns_list", "knowbe4_phishing_security_test_get",
    "knowbe4_phishing_security_test_recipient",
    "knowbe4_phishing_security_test_recipients",
    "knowbe4_phishing_security_tests_list",
    "knowbe4_policies_get", "knowbe4_policies_list",
    "knowbe4_reporting_phishing_summary", "knowbe4_reporting_risk_overview",
    "knowbe4_reporting_training_summary",
    "knowbe4_status",
    "knowbe4_store_purchases_get", "knowbe4_store_purchases_list",
    "knowbe4_training_campaigns_get", "knowbe4_training_campaigns_list",
    "knowbe4_training_enrollments_get", "knowbe4_training_enrollments_list",
    "knowbe4_users_get", "knowbe4_users_list",
    "knowbe4_users_risk_score_history",

    # ninjaone -- list/get/search/inventory/activities/alerts/summary/
    # status/services/policies/jobs/tasks/directory/groups/organizations
    # read variants/tickets read variants/patch installs/vuln scan groups/
    # auth status/navigate/queries_run
    "ninjaone_activities_list",
    "ninjaone_alerts_list", "ninjaone_alerts_summary",
    "ninjaone_auth_status",
    "ninjaone_devices_activities", "ninjaone_devices_alerts",
    "ninjaone_devices_get", "ninjaone_devices_inventory",
    "ninjaone_devices_list", "ninjaone_devices_os_patch_installs",
    "ninjaone_devices_search", "ninjaone_devices_services",
    "ninjaone_directory_list",
    "ninjaone_groups_device_ids", "ninjaone_groups_list",
    "ninjaone_jobs_list",
    "ninjaone_navigate",
    "ninjaone_organizations_devices", "ninjaone_organizations_get",
    "ninjaone_organizations_list", "ninjaone_organizations_locations",
    "ninjaone_policies_get", "ninjaone_policies_list",
    "ninjaone_queries_run",
    "ninjaone_scripts_list",
    "ninjaone_status",
    "ninjaone_tasks_list",
    "ninjaone_tickets_comments", "ninjaone_tickets_get",
    "ninjaone_tickets_list",
    "ninjaone_vulnerability_scan_groups",

    # spanning
    "spanning_audit_list", "spanning_audit_list_all",
    "spanning_backups_list", "spanning_backups_list_all",
    "spanning_license_get",
    "spanning_navigate",
    "spanning_restores_get", "spanning_restores_wait_for",
    "spanning_services_list",
    "spanning_status",
    "spanning_users_get", "spanning_users_list", "spanning_users_list_all",

    # threatlocker
    "threatlocker_approvals_get",
    "threatlocker_approvals_get_permit_application",
    "threatlocker_approvals_list", "threatlocker_approvals_pending_count",
    "threatlocker_audit_file_history", "threatlocker_audit_get",
    "threatlocker_audit_search",
    "threatlocker_computer_groups_dropdown", "threatlocker_computer_groups_list",
    "threatlocker_computers_get", "threatlocker_computers_get_checkins",
    "threatlocker_computers_list",
    "threatlocker_navigate",
    "threatlocker_organizations_for_move_computers",
    "threatlocker_organizations_list_children",
    "threatlocker_status",

    # vanta -- everything (read-only connector)
    "vanta_controls_get", "vanta_controls_list",
    "vanta_documents_get", "vanta_documents_list",
    "vanta_frameworks_get", "vanta_frameworks_list",
    "vanta_frameworks_list_controls",
    "vanta_integrations_get", "vanta_integrations_get_resource",
    "vanta_integrations_list", "vanta_integrations_list_resource_kinds",
    "vanta_integrations_list_resources",
    "vanta_monitored_computers_get", "vanta_monitored_computers_list",
    "vanta_navigate",
    "vanta_people_get", "vanta_people_list",
    "vanta_policies_get", "vanta_policies_list",
    "vanta_risk_scenarios_get", "vanta_risk_scenarios_list",
    "vanta_status",
    "vanta_tests_get", "vanta_tests_list",
    "vanta_vendors_get", "vanta_vendors_list",
    "vanta_vulnerabilities_get", "vanta_vulnerabilities_list",

    # connectwise -- cw_get_*, cw_search_*, cw_list_*, cw_status,
    # cw_test_connection (applies under either server prefix: cw_ tools are
    # shared vocabulary between atlas-connectwise and connectwise-manage)
    "cw_get_activity", "cw_get_agreement", "cw_get_agreement_additions",
    "cw_get_catalog_item", "cw_get_company", "cw_get_configuration",
    "cw_get_contact", "cw_get_invoice", "cw_get_member",
    "cw_get_opportunity", "cw_get_project", "cw_get_project_ticket",
    "cw_get_project_ticket_notes", "cw_get_ticket", "cw_get_ticket_notes",
    "cw_get_time_entry",
    "cw_list_boards", "cw_list_catalog_categories",
    "cw_list_catalog_subcategories", "cw_list_manufacturers",
    "cw_list_priorities", "cw_list_statuses",
    "cw_search_activities", "cw_search_agreements", "cw_search_catalog_items",
    "cw_search_companies", "cw_search_configurations", "cw_search_contacts",
    "cw_search_invoices", "cw_search_members", "cw_search_opportunities",
    "cw_search_opportunity_forecasts", "cw_search_opportunity_notes",
    "cw_search_project_tickets", "cw_search_projects",
    "cw_search_sales_stages", "cw_search_tickets", "cw_search_time_entries",
    "cw_status", "cw_test_connection",
})

# Every ACTION tool named here is redundant with the default-deny rule below
# (anything governed and not in _READ_TOOLS is ACTION) -- listed anyway so
# the classification is legible and testable against the spec line by line.
_ACTION_TOOLS: frozenset[str] = frozenset({
    "cipp_add_scheduled_item", "cipp_create_group",
    "cipp_create_standard_template", "cipp_create_user",
    "cipp_delete_standard_template", "cipp_disable_user", "cipp_edit_user",
    "cipp_offboard_user", "cipp_reset_mfa", "cipp_reset_password",
    "cipp_revoke_sessions", "cipp_run_standards_check",
    "cipp_set_email_forwarding", "cipp_set_out_of_office",

    "ninjaone_alerts_reset", "ninjaone_alerts_reset_all",
    "ninjaone_devices_custom_fields_update", "ninjaone_devices_maintenance",
    "ninjaone_devices_patch_run", "ninjaone_devices_reboot",
    "ninjaone_devices_script_run", "ninjaone_devices_service_control",
    "ninjaone_organizations_create", "ninjaone_sign_in", "ninjaone_sign_out",
    "ninjaone_tickets_add_comment", "ninjaone_tickets_create",
    "ninjaone_tickets_update",

    "spanning_restores_queue",

    "threatlocker_approvals_approve",
    # Returns a live auth key despite the "get" name -- a credential
    # disclosure, not a read.
    "threatlocker_organizations_get_auth_key",

    "cw_create_ticket", "cw_update_ticket", "cw_create_company",
    "cw_update_company", "cw_create_contact", "cw_create_project",
    "cw_create_activity", "cw_create_time_entry", "cw_create_catalog_item",
    "cw_update_catalog_item", "cw_add_ticket_note",
    "cw_add_project_ticket_note",

    # Paylocity returns pay rates, gross/net pay, deductions, taxes, and
    # (behind full:true) bank routing numbers. The owner corrected that
    # Paylocity is not payroll for their use, but the live schemas still
    # return that data -- classified ACTION so nothing reads it without a
    # human saying yes. Flagged for the owner: this may be too strict for
    # legitimate read-only HR lookups; reclassifying any paylocity_* tool
    # to READ is the owner's call, not this gate's.
    "paylocity_cost_centers_list", "paylocity_deductions_list",
    "paylocity_direct_deposit_list", "paylocity_earnings_company_list",
    "paylocity_earnings_employee_list", "paylocity_employees_get",
    "paylocity_employees_list", "paylocity_job_codes_list",
    "paylocity_legacy_employees_get", "paylocity_legacy_employees_list",
    "paylocity_lookup_codes_list", "paylocity_navigate",
    "paylocity_pay_grades_list", "paylocity_pay_statements_summary",
    "paylocity_status", "paylocity_taxes_local_list",
})

# A connector tool call is only governed by this gate when its bare name
# starts with one of these family prefixes. Anything outside these families
# (file tools, terminal, web search, ...) passes through ungated -- this
# module only ever narrows what Penny can do with MSP connectors, it does
# not become a second approval system for unrelated tools.
_GOVERNED_PREFIXES: tuple[str, ...] = (
    "auvik_", "blumira_", "cipp_", "knowbe4_", "ninjaone_",
    "paylocity_", "spanning_", "threatlocker_", "vanta_", "cw_",
)

_MCP_WIRE_DELIM = "__"


def _bare_tool_name(name: str) -> str:
    """Strip an ``mcp__<server>__`` wire prefix down to the bare tool name.

    Accepts a bare name unchanged. Matching happens on this suffix, never
    on the full registry name, so the same underlying tool classifies the
    same way regardless of which MCP server entry currently wraps it
    (``mcp__atlas_connectwise__cw_get_ticket`` and
    ``mcp__connectwise_manage__cw_get_ticket`` are the same governed tool).
    """
    name = (name or "").strip()
    if _MCP_WIRE_DELIM in name:
        return name.rsplit(_MCP_WIRE_DELIM, 1)[-1]
    return name


def is_governed_connector_tool(name: str) -> bool:
    """True when this tool (bare or wire name) is inside a governed family."""
    bare = _bare_tool_name(name)
    return any(bare.startswith(prefix) for prefix in _GOVERNED_PREFIXES)


def classify_connector_tool(name: str) -> Optional[str]:
    """Classify a connector tool call as READ or ACTION.

    Returns ``None`` when the tool is not part of a governed connector
    family at all -- the caller should not gate it (this is not a general
    tool-approval system, only the MSP-connector one).

    Default-deny is mandatory: within a governed family, any tool not on
    the explicit READ list is ACTION, including a name nobody has heard of
    yet. Never defaults an unknown tool to READ.
    """
    bare = _bare_tool_name(name)
    if not is_governed_connector_tool(bare):
        return None
    if bare in _READ_TOOLS:
        return READ
    return ACTION

# ---------------------------------------------------------------------------
# Capability-tool extension -- same READ/ACTION scheme, applied to the
# browser, computer_use, vision, and tts toolsets (2026-09-18 capability
# review). These are native tools, not MCP connector calls, so they are
# governed by name (and, for ``computer_use``, by its ``action`` argument)
# rather than by an MCP server prefix. The rule mirrors the connector rule
# verbatim: anything that only reads/observes (a webpage, a screenshot, an
# image, speaking text aloud) auto-executes; anything that mutates external
# state (a browser click/keystroke/form submission, a computer_use click or
# keystroke that changes something) blocks on the same named-approver flow
# as a NinjaOne device action. Default-deny applies here too.
# ---------------------------------------------------------------------------

_CAPABILITY_READ_TOOLS: frozenset[str] = frozenset({
    # browser -- navigation and observation only; no page mutation.
    "browser_navigate", "browser_snapshot", "browser_scroll", "browser_back",
    "browser_get_images", "browser_vision", "browser_console",
    # vision / tts -- describing an image or speaking text aloud is a read,
    # never a mutation of anything outside the conversation.
    "vision_analyze", "video_analyze", "text_to_speech",
})

_CAPABILITY_ACTION_TOOLS: frozenset[str] = frozenset({
    # browser -- these are exactly the primitives a form submission,
    # checkout, or purchase is built from.
    "browser_click", "browser_type", "browser_press",
})

# computer_use is one tool multiplexed over an ``action`` argument (cua-driver
# style). Only the observational actions are READ; every other action (click
# variants, keyboard input, drag) is ACTION by default-deny.
_COMPUTER_USE_READ_ACTIONS: frozenset[str] = frozenset({
    "screenshot", "cursor_position", "wait",
})

_CAPABILITY_GOVERNED_NAMES: frozenset[str] = _CAPABILITY_READ_TOOLS | _CAPABILITY_ACTION_TOOLS | frozenset({"computer_use"})


def classify_capability_tool(name: str, arguments: Optional[dict] = None) -> Optional[str]:
    """Classify a browser/computer_use/vision/tts tool call as READ or ACTION.

    ``None`` means the tool is outside this extension's scope entirely (the
    caller must not gate it here). ``computer_use`` classifies by its
    ``action`` argument since one tool name covers both a screenshot and a
    mutating click.
    """
    bare = _bare_tool_name(name)
    if bare == "computer_use":
        action = str((arguments or {}).get("action", "")).strip().lower()
        return READ if action in _COMPUTER_USE_READ_ACTIONS else ACTION
    if bare not in _CAPABILITY_GOVERNED_NAMES:
        return None
    return READ if bare in _CAPABILITY_READ_TOOLS else ACTION



# ---------------------------------------------------------------------------
# Approver allowlist -- person-scoped, AAD object id, config-driven.
# ---------------------------------------------------------------------------

# Placeholder marker prefix. An approver entry that still carries this value
# has never been resolved to a real AAD object id and must never match any
# real clicker id -- fail closed, not fail open. See report: no name->AAD-id
# mapping for Ernesto/Jerry/Scarlet exists anywhere in .env or roster.md as
# of this writing; TEAMS_ALLOWED_USERS lists 4 raw ids with no name labels,
# so guessing which id belongs to which person was refused per instruction.
UNRESOLVED_PREFIX = "UNRESOLVED_AAD_ID_"

_DEFAULT_APPROVERS: dict[str, str] = {
    "Ernesto": f"{UNRESOLVED_PREFIX}ERNESTO",
    "Jerry": f"{UNRESOLVED_PREFIX}JERRY",
    "Scarlet": f"{UNRESOLVED_PREFIX}SCARLET",
}


def _load_approver_config() -> dict[str, str]:
    """Read ``connector_approvals.approvers`` from config.yaml.

    Falls back to the built-in (unresolved-placeholder) defaults on any
    read failure -- fail closed, since a config read failure must never be
    read as "no allowlist configured, allow everyone".
    """
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        approvals_cfg = cfg.get("connector_approvals", {}) or {}
        approvers = approvals_cfg.get("approvers", {}) or {}
        if isinstance(approvers, dict) and approvers:
            return {str(k): str(v) for k, v in approvers.items()}
    except Exception as exc:
        logger.warning("Failed to load connector_approvals.approvers: %s", exc)
    return dict(_DEFAULT_APPROVERS)


def get_approver_allowlist() -> dict[str, str]:
    """Return the current name -> AAD-object-id approver map."""
    return _load_approver_config()


def is_authorized_approver(aad_object_id: str) -> bool:
    """True only when *aad_object_id* matches a resolved (non-placeholder) approver.

    An unresolved placeholder entry can never match a real id -- the
    placeholder string itself is never a legal AAD object id -- so a
    name whose id has not been filled in simply cannot approve anything,
    rather than accidentally matching every clicker (fail closed).
    """
    if not aad_object_id:
        return False
    allowlist = get_approver_allowlist()
    for name, configured_id in allowlist.items():
        if not configured_id or configured_id.startswith(UNRESOLVED_PREFIX):
            continue
        if configured_id == aad_object_id:
            return True
    return False


def approver_name_for_id(aad_object_id: str) -> Optional[str]:
    """Reverse lookup for audit logging -- best effort, may return None."""
    if not aad_object_id:
        return None
    allowlist = get_approver_allowlist()
    for name, configured_id in allowlist.items():
        if configured_id and not configured_id.startswith(UNRESOLVED_PREFIX):
            if configured_id == aad_object_id:
                return name
    return None


# ---------------------------------------------------------------------------
# Audit trail -- every ACTION attempt, approved or not.
# ---------------------------------------------------------------------------

_AUDIT_LOCK = threading.Lock()


def _audit_log_path() -> Path:
    try:
        from hermes_cli.config import get_hermes_home
        home = get_hermes_home()
    except Exception:
        home = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
    return Path(home) / "logs" / "connector_action_audit.jsonl"


def record_audit_event(
    *,
    session_key: str,
    requested_by: str,
    server_name: str,
    tool_name: str,
    arguments: dict,
    outcome: str,
    approved_by: Optional[str] = None,
    detail: str = "",
) -> None:
    """Append one audit line. Never raises -- a logging failure must not
    itself block or silently permit a connector action; the gate's fail
    behavior is decided before this is called, this only records it.
    """
    entry = {
        "ts": time.time(),
        "session_key": session_key,
        "requested_by": requested_by,
        "server": server_name,
        "tool": tool_name,
        # Arguments can carry PII/secrets (employee names, device ids); keep
        # them in the audit trail (examiners need to see the target) but cap
        # size so a huge payload can't blow up the log file.
        "arguments": {k: str(v)[:500] for k, v in (arguments or {}).items()},
        "outcome": outcome,          # "approved" | "denied" | "timeout" | "error" | "unauthorized_approver"
        "approved_by": approved_by,  # approver name, or None
        "detail": detail,
    }
    try:
        path = _audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:
        logger.error("connector_action_gate: failed to write audit log: %s", exc)


# ---------------------------------------------------------------------------
# Blocking approval request -- reuses tools.approval's gateway queue.
# ---------------------------------------------------------------------------


def _describe_target(tool_name: str, arguments: dict) -> str:
    """Best-effort human-readable target line: which device/user/ticket."""
    if not arguments:
        return "(no parameters)"
    candidate_keys = (
        "deviceId", "device_id", "userId", "user_id", "username",
        "email", "ticketId", "ticket_id", "id", "name", "companyId",
        "company_id",
    )
    parts = []
    for key in candidate_keys:
        if key in arguments:
            parts.append(f"{key}={arguments[key]}")
    if not parts:
        # Fall back to the first couple of args so the approver still sees
        # *something* concrete rather than a blank target.
        parts = [f"{k}={v}" for k, v in list(arguments.items())[:3]]
    return ", ".join(parts) if parts else "(no identifying parameters)"


class ConnectorApprovalError(RuntimeError):
    """Raised when the gate itself fails -- callers must treat this as deny."""


def request_connector_action_approval(
    *,
    session_key: str,
    server_name: str,
    tool_name: str,
    arguments: dict,
    requested_by: str = "Penny",
) -> tuple[bool, str]:
    """Block until a named approver approves or denies this connector ACTION.

    Returns ``(approved, outcome)`` where *outcome* is one of "approved",
    "denied", "timeout", "unauthorized_approver", or "error". Never raises
    for an ordinary deny/timeout -- only a genuine internal failure surfaces
    as ``("error")``, and the caller (the mcp_tool.py call site) must treat
    that as a deny (fail closed), never as an allow.

    Every attempt is written to the audit log before returning, regardless
    of outcome.

    Deliberately does NOT consult ``tools.approval.is_approved`` /
    session-YOLO / the permanent allowlist: the owner's rule is that Penny
    is never allowed to act on these tools without express approval, every
    single time, so this bypasses none of the shortcuts the general
    dangerous-command approval flow offers for repeat operations.
    """
    bare = _bare_tool_name(tool_name)
    approval_data = {
        "command": f"connector:{server_name}.{bare}",
        "description": (
            f"MSP connector action -- {server_name}.{bare} "
            f"-> {_describe_target(bare, arguments)}"
        ),
        "pattern_key": f"connector_action:{bare}",
        "pattern_keys": [f"connector_action:{bare}"],
        # Marker consulted by the platform adapter's card-click handler so
        # it checks the connector approver allowlist instead of (or in
        # addition to) the general chat roster before resolving.
        "requires_connector_approver": True,
        "connector_server": server_name,
        "connector_tool": bare,
        "connector_arguments": arguments,
    }

    try:
        from tools.approval import (
            _await_gateway_decision,
            _gateway_notify_cbs,
            _lock as _approval_lock,
        )
    except Exception as exc:
        logger.error("connector_action_gate: failed to import approval plumbing: %s", exc)
        record_audit_event(
            session_key=session_key, requested_by=requested_by,
            server_name=server_name, tool_name=bare, arguments=arguments,
            outcome="error", detail=f"import failure: {exc}",
        )
        return False, "error"

    with _approval_lock:
        notify_cb = _gateway_notify_cbs.get(session_key)

    if notify_cb is None:
        # No gateway session is listening for approval prompts (e.g. a cron
        # job, a probe script, an unattended context). Fail closed: an
        # ACTION connector tool must never execute silently just because
        # nobody is present to click approve.
        record_audit_event(
            session_key=session_key, requested_by=requested_by,
            server_name=server_name, tool_name=bare, arguments=arguments,
            outcome="error", detail="no approval notify channel registered for session",
        )
        return False, "error"

    try:
        result = _await_gateway_decision(
            session_key, notify_cb, approval_data, surface="connector_action",
        )
    except Exception as exc:
        logger.error("connector_action_gate: approval wait failed: %s", exc)
        record_audit_event(
            session_key=session_key, requested_by=requested_by,
            server_name=server_name, tool_name=bare, arguments=arguments,
            outcome="error", detail=f"approval wait raised: {exc}",
        )
        return False, "error"

    choice = result.get("choice")
    resolved = result.get("resolved")

    if not resolved or choice in (None, "deny"):
        outcome = "denied" if resolved else "timeout"
        record_audit_event(
            session_key=session_key, requested_by=requested_by,
            server_name=server_name, tool_name=bare, arguments=arguments,
            outcome=outcome,
        )
        return False, outcome

    # "once" / "session" / "always" are all treated identically -- see
    # approve_always investigation in the report: persistence to skip a
    # future prompt is deliberately not honored for connector actions, so
    # every one of these choices means "approved this one call", nothing
    # more. The identity check that authorized the click already happened
    # inside the platform adapter's card handler (person-scoped, against
    # the connector approver allowlist, not the general chat roster) before
    # resolve_gateway_approval() was ever called -- this function trusts
    # that gate, it does not re-verify identity here (it has none to check;
    # the clicker's id never reaches this side of the queue).
    approved_by = None
    reason = result.get("reason") or ""
    if reason.startswith("approved_by:"):
        approved_by = reason[len("approved_by:"):]

    record_audit_event(
        session_key=session_key, requested_by=requested_by,
        server_name=server_name, tool_name=bare, arguments=arguments,
        outcome="approved", approved_by=approved_by,
    )
    return True, "approved"


# ---------------------------------------------------------------------------
# Handler wrapper -- single call site for browser/computer_use registration
# loops to gate an ACTION-classified capability call the same way
# ``tools/mcp_tool_handlers.py`` gates an ACTION-classified connector call.
# Reuses ``request_connector_action_approval`` unmodified (server_name is
# "browser" / "computer_use" instead of an MSP connector name) -- one
# approval mechanism, not a second one for native tools.
# ---------------------------------------------------------------------------


def require_capability_approval(server_name: str, tool_name: str):
    """Decorator factory: wraps a ``(args, **kw) -> result`` handler so an
    ACTION-classified call blocks on express approval before it runs, and a
    READ-classified (or ungoverned) call passes straight through. Fails
    closed on any error in the gate itself, mirroring the MCP wiring.
    ``tool_name`` is fixed at registration time -- ``computer_use`` still
    classifies per-call by its ``action`` argument inside
    ``classify_capability_tool``."""

    def _decorate(handler):
        def _wrapped(args: dict, **kw):
            try:
                verdict = classify_capability_tool(tool_name, args)
            except Exception as exc:  # pragma: no cover - defensive, mirrors MCP wiring
                logger.error(
                    "connector_action_gate: capability classify failed for %s/%s: %s -- failing closed",
                    server_name, tool_name, exc,
                )
                return tool_error_denied(tool_name)
            if verdict != ACTION:
                return handler(args, **kw)
            try:
                from tools.approval import get_current_session_key
                session_key = get_current_session_key(default="")
            except Exception:
                session_key = ""
            try:
                approved, outcome = request_connector_action_approval(
                    session_key=session_key, server_name=server_name,
                    tool_name=tool_name, arguments=args or {},
                )
            except Exception as exc:
                logger.error(
                    "connector_action_gate: capability approval raised for %s/%s: %s -- failing closed",
                    server_name, tool_name, exc,
                )
                return tool_error_denied(tool_name)
            if not approved:
                return (
                    f'{{"error": "Tool \'{tool_name}\' requires express approval from a named '
                    f'approver before it can run and was not approved (outcome: {outcome}). '
                    f'Do not retry without a human approving it."}}'
                )
            return handler(args, **kw)

        _wrapped.__name__ = getattr(handler, "__name__", "_gated_capability_handler")
        return _wrapped

    return _decorate


def tool_error_denied(tool_name: str) -> str:
    """Fail-closed JSON error string for a capability gate internal failure."""
    return (
        f'{{"error": "Tool \'{tool_name}\' blocked: the capability security gate '
        f'failed to evaluate this call and fails closed on error."}}'
    )
