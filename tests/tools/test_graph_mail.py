"""Tests for tools/graph_mail.py."""

from __future__ import annotations

import pytest

from tools.graph_mail import _load_mail_credentials, send_html_mail
from tools.microsoft_graph_auth import MicrosoftGraphAuthError
from tools.microsoft_graph_client import MicrosoftGraphAPIError


class _FakeClient:
    """Stand-in for MicrosoftGraphClient.send_mail -- no real HTTP call."""

    def __init__(self, *, result=None, raises=None):
        self.result = result or {"sent": True, "status_code": 202}
        self.raises = raises
        self.calls: list[dict] = []

    async def send_mail(self, sender, message, *, save_to_sent_items=False, headers=None):
        self.calls.append(
            {
                "sender": sender,
                "message": message,
                "save_to_sent_items": save_to_sent_items,
            }
        )
        if self.raises is not None:
            raise self.raises
        return self.result


class TestLoadMailCredentials:
    def test_raises_and_names_missing_vars(self):
        with pytest.raises(MicrosoftGraphAuthError) as exc:
            _load_mail_credentials({})
        assert "ENTRA_TENANT_ID" in str(exc.value)
        assert "TEAMS_CLIENT_ID" in str(exc.value)
        assert "TEAMS_CLIENT_SECRET" in str(exc.value)

    def test_builds_credentials_from_teams_env_vars(self):
        creds = _load_mail_credentials(
            {
                "ENTRA_TENANT_ID": "tenant-1",
                "TEAMS_CLIENT_ID": "client-1",
                "TEAMS_CLIENT_SECRET": "secret-1",
            }
        )
        assert creds.tenant_id == "tenant-1"
        assert creds.client_id == "client-1"
        assert creds.client_secret == "secret-1"


@pytest.mark.anyio
class TestSendHtmlMail:
    async def test_sends_with_expected_payload_shape(self):
        fake = _FakeClient()
        result = await send_html_mail(
            to=["a@example.com", "b@example.com"],
            subject="Test subject",
            html="<p>hi</p>",
            sender="penny@henssler.com",
            cc=["c@example.com"],
            client=fake,
        )

        assert result == {"success": True, "status": 202, "error": None}
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["sender"] == "penny@henssler.com"
        assert call["message"]["subject"] == "Test subject"
        assert call["message"]["body"] == {"contentType": "HTML", "content": "<p>hi</p>"}
        assert call["message"]["toRecipients"] == [
            {"emailAddress": {"address": "a@example.com"}},
            {"emailAddress": {"address": "b@example.com"}},
        ]
        assert call["message"]["ccRecipients"] == [
            {"emailAddress": {"address": "c@example.com"}}
        ]

    async def test_omits_cc_recipients_when_not_given(self):
        fake = _FakeClient()
        await send_html_mail(
            to=["a@example.com"],
            subject="s",
            html="<p>x</p>",
            sender="penny@henssler.com",
            client=fake,
        )
        assert "ccRecipients" not in fake.calls[0]["message"]

    async def test_requires_at_least_one_recipient(self):
        with pytest.raises(ValueError):
            await send_html_mail(
                to=[], subject="s", html="<p>x</p>", sender="penny@henssler.com"
            )

    async def test_requires_sender(self):
        with pytest.raises(ValueError):
            await send_html_mail(
                to=["a@example.com"], subject="s", html="<p>x</p>", sender=""
            )

    async def test_returns_structured_failure_on_api_error_instead_of_raising(self):
        api_error = MicrosoftGraphAPIError(
            403, "POST", "https://graph.microsoft.com/v1.0/users/x/sendMail", "Forbidden"
        )
        fake = _FakeClient(raises=api_error)
        result = await send_html_mail(
            to=["a@example.com"],
            subject="s",
            html="<p>x</p>",
            sender="penny@henssler.com",
            client=fake,
        )
        assert result["success"] is False
        assert result["status"] == 403
        assert "Forbidden" in result["error"]

    async def test_missing_credentials_fail_loudly_when_no_client_injected(self, monkeypatch):
        for name in ("ENTRA_TENANT_ID", "TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET"):
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(MicrosoftGraphAuthError) as exc:
            await send_html_mail(
                to=["a@example.com"], subject="s", html="<p>x</p>", sender="penny@henssler.com"
            )
        assert "TEAMS_CLIENT_SECRET" in str(exc.value)
