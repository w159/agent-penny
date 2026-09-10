"""Build the escalation-email HTML for an unacknowledged IT trend alert.

This is the email channel of the trend escalation ladder (see
cron/trend_escalation.py) -- it fires only after a trend has gone
unacknowledged in Teams, so unlike the Teams card, it must stand on its
own for a reader who never saw the original card: full context, no
"see the card for details".

House rule (rules/security.md, input-sanitization checklist): ticket
summaries and evidence come from ConnectWise notes written by technicians
and end users -- untrusted input. Every value interpolated into the HTML
below goes through ``html.escape`` first.
"""

from __future__ import annotations

from html import escape
from typing import Any

from cron.trend_escalation import ESCALATION_LADDER

# Henssler brand colors (rules/project-structure.md) -- required on every
# Henssler-facing deliverable, this email included.
_DARK_GREEN = "#154734"
_GOLD = "#C49A22"
_BODY_TEXT = "#333333"
_MUTED_TEXT = "#6b6b6b"
_WHITE = "#ffffff"
_ROW_STRIPE = "#f6f4ee"

_MAX_TICKET_ROWS = 25

_KIND_LABELS = {
    "new": "NEW",
    "update": "UPDATED",
    "escalation": "UNACKNOWLEDGED",
}


def _escalation_hours(alert: dict, trend: dict) -> float:
    """Hours-unacknowledged for the subject/badge, from whichever field is set.

    Alerts may carry an explicit ``level`` (cron/trend_escalation.py's own
    alert shape) or a trend-level ``escalation_level``; either maps onto
    ESCALATION_LADDER's business_hours_elapsed for that rung. Reusing the
    ladder here (read-only import) keeps the hour thresholds in one place
    rather than re-declaring them.
    """
    level = alert.get("level")
    if level is None:
        level = trend.get("escalation_level")
    if level is None:
        return 0.0
    try:
        level = int(level)
    except (TypeError, ValueError):
        return 0.0
    level = max(0, min(level, len(ESCALATION_LADDER) - 1))
    return ESCALATION_LADDER[level]["business_hours_elapsed"]


def _format_hours(hours: float) -> str:
    if hours == int(hours):
        return f"{int(hours)}h"
    return f"{hours:.1f}h"


def build_trend_email(alert: dict, *, dashboard_url: str | None = None) -> tuple[str, str]:
    """Build (subject, html) for the unacknowledged-trend escalation email.

    Args:
        alert: {"trend": {...}, "kind": "new" | "update" | "escalation", ...}.
        dashboard_url: Optional link back to the trend dashboard, shown in
            the footer alongside the Teams-acknowledgment instructions.

    Returns:
        (subject, html) -- html is a complete standalone document.
    """
    trend: dict[str, Any] = alert.get("trend") or {}
    kind = alert.get("kind") or "escalation"

    title = str(trend.get("title") or "Untitled trend")
    trend_id = str(trend.get("trend_id") or "unknown")
    confidence = trend.get("confidence")
    first_seen = str(trend.get("first_seen") or "unknown")
    last_seen = str(trend.get("last_seen") or "unknown")
    ticket_count = trend.get("ticket_count") or 0
    device_count = trend.get("device_count") or 0
    user_count = trend.get("user_count") or 0
    techs = trend.get("techs") or []
    devices = trend.get("devices") or []
    why_related = str(trend.get("why_related") or "Not provided.")
    recommended_action = str(trend.get("recommended_action") or "Not provided.")
    tickets = trend.get("tickets") or []

    hours = _escalation_hours(alert, trend)
    hours_label = _format_hours(hours)

    subject = (
        f"UNACKNOWLEDGED IT TREND ({hours_label}): {title} - "
        f"{ticket_count} tickets, {device_count} devices"
    )

    html = _render_html(
        kind=kind,
        hours_label=hours_label,
        title=title,
        trend_id=trend_id,
        confidence=confidence,
        first_seen=first_seen,
        last_seen=last_seen,
        ticket_count=ticket_count,
        device_count=device_count,
        user_count=user_count,
        techs=techs,
        devices=devices,
        why_related=why_related,
        recommended_action=recommended_action,
        tickets=tickets,
        dashboard_url=dashboard_url,
    )
    return subject, html


def _confidence_label(confidence: Any) -> str:
    if confidence is None:
        return "n/a"
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return escape(str(confidence))
    if 0 <= value <= 1:
        return f"{value * 100:.0f}%"
    return f"{value:.0f}"


def _stat_cell(label: str, value: str) -> str:
    return (
        f'<td style="padding:12px 8px;text-align:center;border-top:2px solid {_GOLD};">'
        f'<div style="font-size:20px;font-weight:700;color:{_DARK_GREEN};">{escape(value)}</div>'
        f'<div style="font-size:11px;letter-spacing:0.04em;text-transform:uppercase;color:{_MUTED_TEXT};margin-top:2px;">{escape(label)}</div>'
        f"</td>"
    )


def _bullet_list(items: list[Any], *, empty_label: str) -> str:
    if not items:
        return f'<p style="margin:4px 0;color:{_MUTED_TEXT};font-style:italic;">{escape(empty_label)}</p>'
    rows = "".join(
        f'<li style="margin:0 0 4px 0;">{escape(str(item))}</li>' for item in items
    )
    return f'<ul style="margin:4px 0 0 0;padding-left:20px;">{rows}</ul>'


def _ticket_rows(tickets: list[dict]) -> tuple[str, int]:
    shown = tickets[:_MAX_TICKET_ROWS]
    overflow = max(0, len(tickets) - len(shown))
    rows = []
    for index, ticket in enumerate(shown):
        bg = _ROW_STRIPE if index % 2 == 1 else _WHITE
        ticket_id = escape(str(ticket.get("id", "")))
        date = escape(str(ticket.get("date", "")))
        summary = escape(str(ticket.get("summary", "")))
        evidence = escape(str(ticket.get("evidence", "")))
        rows.append(
            f'<tr style="background:{bg};">'
            f'<td style="padding:8px;border-bottom:1px solid #e5e0d0;font-size:13px;white-space:nowrap;">{ticket_id}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e0d0;font-size:13px;white-space:nowrap;">{date}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e0d0;font-size:13px;">{summary}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e0d0;font-size:13px;color:{_MUTED_TEXT};">{evidence}</td>'
            f"</tr>"
        )
    return "".join(rows), overflow


def _render_html(
    *,
    kind: str,
    hours_label: str,
    title: str,
    trend_id: str,
    confidence: Any,
    first_seen: str,
    last_seen: str,
    ticket_count: int,
    device_count: int,
    user_count: int,
    techs: list[Any],
    devices: list[Any],
    why_related: str,
    recommended_action: str,
    tickets: list[dict],
    dashboard_url: str | None,
) -> str:
    badge_text = f"{_KIND_LABELS.get(kind, 'UNACKNOWLEDGED')} - {hours_label} UNACKNOWLEDGED"
    ticket_rows_html, overflow = _ticket_rows(tickets)
    overflow_row = (
        f'<tr><td colspan="4" style="padding:8px;font-size:12px;color:{_MUTED_TEXT};font-style:italic;">'
        f"... and {overflow} more</td></tr>"
        if overflow
        else ""
    )
    dashboard_line = (
        f' or view it on the <a href="{escape(dashboard_url)}" style="color:{_DARK_GREEN};">trend dashboard</a>'
        if dashboard_url
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
</head>
<body style="margin:0;padding:0;background:{_WHITE};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{_WHITE};">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;width:100%;font-family:Arial,Helvetica,sans-serif;color:{_BODY_TEXT};">

<tr><td style="background:{_DARK_GREEN};padding:24px 28px;">
<div style="display:inline-block;background:{_GOLD};color:{_DARK_GREEN};font-size:11px;font-weight:700;letter-spacing:0.05em;padding:4px 10px;border-radius:3px;margin-bottom:10px;">{escape(badge_text)}</div>
<div style="color:{_WHITE};font-size:20px;font-weight:700;line-height:1.3;">{escape(title)}</div>
</td></tr>

<tr><td style="padding:0 28px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
{_stat_cell("Tickets", str(ticket_count))}
{_stat_cell("Devices", str(device_count))}
{_stat_cell("Users", str(user_count))}
{_stat_cell("Confidence", _confidence_label(confidence))}
</tr>
<tr>
{_stat_cell("First seen", first_seen)}
{_stat_cell("Last seen", last_seen)}
<td colspan="2" style="border-top:2px solid {_GOLD};"></td>
</tr>
</table>
</td></tr>

<tr><td style="padding:20px 28px 0 28px;">
<div style="font-size:14px;font-weight:700;color:{_DARK_GREEN};text-transform:uppercase;letter-spacing:0.03em;margin-bottom:6px;">Why these are related</div>
<p style="margin:0;font-size:14px;line-height:1.5;">{escape(why_related)}</p>
</td></tr>

<tr><td style="padding:20px 28px 0 28px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#faf7ee;border-left:4px solid {_GOLD};">
<tr><td style="padding:14px 16px;">
<div style="font-size:14px;font-weight:700;color:{_DARK_GREEN};text-transform:uppercase;letter-spacing:0.03em;margin-bottom:6px;">Recommended action</div>
<p style="margin:0;font-size:14px;line-height:1.5;">{escape(recommended_action)}</p>
</td></tr>
</table>
</td></tr>

<tr><td style="padding:20px 28px 0 28px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr valign="top">
<td width="50%" style="padding-right:10px;">
<div style="font-size:14px;font-weight:700;color:{_DARK_GREEN};text-transform:uppercase;letter-spacing:0.03em;margin-bottom:6px;">Affected devices</div>
{_bullet_list(devices, empty_label="None recorded.")}
</td>
<td width="50%" style="padding-left:10px;">
<div style="font-size:14px;font-weight:700;color:{_DARK_GREEN};text-transform:uppercase;letter-spacing:0.03em;margin-bottom:6px;">Technicians involved</div>
{_bullet_list(techs, empty_label="None recorded.")}
</td>
</tr>
</table>
</td></tr>

<tr><td style="padding:20px 28px 0 28px;">
<div style="font-size:14px;font-weight:700;color:{_DARK_GREEN};text-transform:uppercase;letter-spacing:0.03em;margin-bottom:6px;">Member tickets</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:2px solid {_GOLD};">
<tr style="background:{_DARK_GREEN};">
<td style="padding:8px;color:{_WHITE};font-size:12px;text-transform:uppercase;letter-spacing:0.03em;">Ticket</td>
<td style="padding:8px;color:{_WHITE};font-size:12px;text-transform:uppercase;letter-spacing:0.03em;">Date</td>
<td style="padding:8px;color:{_WHITE};font-size:12px;text-transform:uppercase;letter-spacing:0.03em;">Summary</td>
<td style="padding:8px;color:{_WHITE};font-size:12px;text-transform:uppercase;letter-spacing:0.03em;">Evidence</td>
</tr>
{ticket_rows_html}
{overflow_row}
</table>
</td></tr>

<tr><td style="padding:24px 28px 28px 28px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #e5e0d0;">
<tr><td style="padding-top:14px;font-size:12px;color:{_MUTED_TEXT};line-height:1.6;">
Trend ID: {escape(trend_id)}<br/>
Generated by Agent Penny.<br/>
To acknowledge, reply in the Teams Triage channel{dashboard_line} or click Acknowledge on the card.
</td></tr>
</table>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""
