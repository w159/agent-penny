"""Send HTML email via Microsoft Graph's application-only sendMail action.

Reuses the existing Graph auth layer (``tools.microsoft_graph_auth``) and the
generic ``MicrosoftGraphClient`` HTTP/retry machinery instead of standing up a
second token flow. The Hermes Teams app registration authenticates the same
way it already does for the Teams webhook (client-credentials, app-only), but
its secrets live in the Teams-flavored env var names (``ENTRA_TENANT_ID`` /
``TEAMS_CLIENT_ID`` / ``TEAMS_CLIENT_SECRET``) rather than the ``MSGRAPH_*``
names ``GraphCredentials.from_env`` expects, so credentials are built directly
here instead of going through that helper.

Docs consulted (mandatory before writing the payload below): Microsoft Learn,
"user: sendMail" (POST /users/{id}/sendMail, JSON message resource, least
privileged permission Mail.Send, application permission type supported).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from tools.microsoft_graph_auth import (
    GraphCredentials,
    MicrosoftGraphAuthError,
    MicrosoftGraphTokenProvider,
)
from tools.microsoft_graph_client import (
    MicrosoftGraphAPIError,
    MicrosoftGraphClient,
    MicrosoftGraphClientError,
)

logger = logging.getLogger(__name__)

# Names in the shared .env for the Teams app registration Mail.Send was
# granted on. Kept distinct from tools.microsoft_graph_auth's MSGRAPH_*
# names -- see module docstring.
_TENANT_ID_ENV = "ENTRA_TENANT_ID"
_CLIENT_ID_ENV = "TEAMS_CLIENT_ID"
_CLIENT_SECRET_ENV = "TEAMS_CLIENT_SECRET"

_MAX_RETRIES = 3


def _load_mail_credentials(environ: dict[str, str] | None = None) -> GraphCredentials:
    """Build Graph credentials from the Teams app registration's env vars.

    Fails loudly and names exactly what is missing -- this is the credential
    boundary for outbound mail, so a silent no-op here would hide a real
    configuration gap (e.g. Mail.Send consent granted but secret not deployed).
    """
    env = environ if environ is not None else os.environ
    tenant_id = (env.get(_TENANT_ID_ENV) or "").strip()
    client_id = (env.get(_CLIENT_ID_ENV) or "").strip()
    client_secret = (env.get(_CLIENT_SECRET_ENV) or "").strip()

    missing = [
        name
        for name, value in (
            (_TENANT_ID_ENV, tenant_id),
            (_CLIENT_ID_ENV, client_id),
            (_CLIENT_SECRET_ENV, client_secret),
        )
        if not value
    ]
    if missing:
        raise MicrosoftGraphAuthError(
            "Cannot send mail via Microsoft Graph: missing "
            f"{', '.join(missing)}. Set these in .env for the Teams app "
            "registration and grant it the Mail.Send application permission "
            "with admin consent."
        )

    return GraphCredentials(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )


def _build_recipients(addresses: list[str]) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": address}} for address in addresses]


async def send_html_mail(
    *,
    to: list[str],
    subject: str,
    html: str,
    sender: str,
    cc: list[str] | None = None,
    client: MicrosoftGraphClient | None = None,
) -> dict[str, Any]:
    """Send an HTML email via ``POST /users/{sender}/sendMail``.

    Returns a structured result instead of raising for expected failure
    modes (bad recipient, throttling that exhausted retries, etc.) so
    callers can log and move on rather than crash a cron pass. Credential
    and consent problems still raise -- those are configuration bugs, not
    expected runtime outcomes, and must fail loudly.

    Args:
        to: Recipient addresses. Required, non-empty.
        subject: Mail subject line.
        html: HTML body content.
        sender: Mailbox to send as, e.g. "penny@henssler.com". Required --
            never hardcoded, the caller decides which mailbox sends.
        cc: Optional CC addresses.
        client: Injected Graph client, for tests. When omitted, one is built
            from the Teams app registration's env credentials.

    Returns:
        {"success": bool, "status": int | None, "error": str | None}
    """
    if not to:
        raise ValueError("send_html_mail requires at least one 'to' recipient.")
    if not sender:
        raise ValueError("send_html_mail requires a 'sender' mailbox.")

    if client is None:
        # Credential lookup failures raise MicrosoftGraphAuthError -- this is
        # the fail-loud path, not something send_html_mail should swallow.
        credentials = _load_mail_credentials()
        token_provider = MicrosoftGraphTokenProvider(credentials)
        client = MicrosoftGraphClient(
            token_provider,
            max_retries=_MAX_RETRIES,
            timeout=30.0,
        )

    message: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html},
        "toRecipients": _build_recipients(to),
    }
    if cc:
        message["ccRecipients"] = _build_recipients(cc)

    try:
        # MicrosoftGraphClient already retries transient 401/429/5xx with
        # exponential backoff up to max_retries -- reuse that instead of a
        # second retry loop layered on top.
        result = await client.send_mail(sender, message, save_to_sent_items=False)
    except MicrosoftGraphAPIError as exc:
        # Expected failure mode (bad recipient, insufficient permission on
        # the mailbox, throttling that outlived the retry budget): report
        # structured, don't crash the caller.
        logger.error(
            "graph_mail.send_html_mail failed",
            extra={
                "sender": sender,
                "status": exc.status_code,
                "error": str(exc),
            },
        )
        return {"success": False, "status": exc.status_code, "error": str(exc)}
    except MicrosoftGraphClientError as exc:
        # Network/transport failure that exhausted MicrosoftGraphClient's
        # own retries.
        logger.error(
            "graph_mail.send_html_mail transport failure",
            extra={"sender": sender, "error": str(exc)},
        )
        return {"success": False, "status": None, "error": str(exc)}
    return {"success": True, "status": result["status_code"], "error": None}
