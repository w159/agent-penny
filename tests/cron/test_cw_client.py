"""
Unit tests for cron/cw_client.py - the live ConnectWise Manage REST client.

No live network calls. urllib.request.urlopen is mocked throughout. The
base-URL doubling trap (see cw_client.py's module docstring) and the
private-key redaction guarantee are the two cases that matter most here;
both were the source of real, misleading failures against live CW.
"""
from __future__ import annotations

import base64
import email.message
import json
import urllib.error
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from cron.cw_client import (
    CWClient,
    CWError,
    _MAX_ATTEMPTS_RATE_LIMIT,
    _MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS,
    _RATE_LIMIT_FLOOR_SECONDS,
    _build_api_root,
    _rate_limit_wait_seconds,
)


ENV_VALUES = {
    "CW_MANAGE_COMPANY_ID": "henssler",
    "CW_MANAGE_PUBLIC_KEY": "pubkey123",
    "CW_MANAGE_PRIVATE_KEY": "supersecretprivatekey",
    "CW_MANAGE_CLIENT_ID": "client-uuid",
    "CW_MANAGE_BASE_URL": "api-na.myconnectwise.net/v4_6_release/apis/3.0/",
}


def _write_env(tmp_path, values=ENV_VALUES):
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{k}={v}" for k, v in values.items()))
    return env_path


def _client(tmp_path, monkeypatch, values=ENV_VALUES):
    """Build a CWClient with process env cleared, forcing file-load path."""
    for name in values:
        monkeypatch.delenv(name, raising=False)
    env_path = _write_env(tmp_path, values)
    return CWClient(env_path=env_path)


def _http_error(code, body=b"{}", retry_after=None):
    """Build an HTTPError with an optional Retry-After header."""
    hdrs = None
    if retry_after is not None:
        hdrs = email.message.Message()
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://x", code=code, msg="error", hdrs=hdrs, fp=BytesIO(body),
    )


def _fake_response(payload):
    """Build a urlopen() context-manager mock returning a JSON body."""
    body = json.dumps(payload).encode()
    mock_response = MagicMock()
    mock_response.read.return_value = body
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = False
    return mock_response


class TestBaseUrlDoublingTrap:
    def test_bare_host_gets_https_prepended(self):
        assert _build_api_root("api-na.myconnectwise.net/v4_6_release/apis/3.0/") == (
            "https://api-na.myconnectwise.net/v4_6_release/apis/3.0"
        )

    def test_host_already_has_scheme_is_unchanged(self):
        assert _build_api_root("https://api-na.myconnectwise.net/v4_6_release/apis/3.0/") == (
            "https://api-na.myconnectwise.net/v4_6_release/apis/3.0"
        )

    def test_both_forms_resolve_to_the_same_root(self):
        bare = _build_api_root("api-na.myconnectwise.net/v4_6_release/apis/3.0/")
        scheme = _build_api_root("https://api-na.myconnectwise.net/v4_6_release/apis/3.0/")
        assert bare == scheme


class TestAuthHeader:
    def test_auth_header_is_basic_company_plus_pubkey_colon_privatekey(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        headers = client._headers()
        expected_pair = "henssler+pubkey123:supersecretprivatekey"
        expected = "Basic " + base64.b64encode(expected_pair.encode()).decode()
        assert headers["Authorization"] == expected

    def test_client_id_header_present(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        assert client._headers()["clientId"] == "client-uuid"
        assert client._headers()["Accept"] == "application/json"


class TestMissingCredentials:
    def test_missing_var_raises_cwerror_naming_the_var(self, tmp_path, monkeypatch):
        values = dict(ENV_VALUES)
        del values["CW_MANAGE_PRIVATE_KEY"]
        for name in ENV_VALUES:
            monkeypatch.delenv(name, raising=False)
        env_path = _write_env(tmp_path, values)
        with pytest.raises(CWError, match="CW_MANAGE_PRIVATE_KEY"):
            CWClient(env_path=env_path)

    def test_empty_env_file_raises(self, tmp_path, monkeypatch):
        for name in ENV_VALUES:
            monkeypatch.delenv(name, raising=False)
        env_path = tmp_path / ".env"
        env_path.write_text("")
        with pytest.raises(CWError):
            CWClient(env_path=env_path)

    def test_process_env_wins_over_file(self, tmp_path, monkeypatch):
        env_path = _write_env(tmp_path)
        monkeypatch.setenv("CW_MANAGE_COMPANY_ID", "override-company")
        client = CWClient(env_path=env_path)
        assert client._company_id == "override-company"


class TestPagination:
    def test_paged_stops_when_short_page_returned(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        page_one = [{"id": i} for i in range(1000)]
        page_two = [{"id": i} for i in range(1000, 1050)]

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [
                _fake_response(page_one),
                _fake_response(page_two),
            ]
            result = client.paged("/service/tickets", "dateEntered>[2026-08-01T00:00:00Z]")

        assert len(result) == 1050
        assert mock_urlopen.call_count == 2

    def test_paged_stops_immediately_on_empty_first_page(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _fake_response([])
            result = client.paged("/service/tickets", "dateEntered>[2026-08-01T00:00:00Z]")
        assert result == []
        assert mock_urlopen.call_count == 1


class TestRetry:
    def test_retries_500_then_succeeds(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        error = urllib.error.HTTPError(
            url="https://x", code=500, msg="Internal Server Error",
            hdrs=None, fp=BytesIO(b"boom"),
        )
        success = _fake_response({"count": 5})

        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep"):
            mock_urlopen.side_effect = [error, success]
            result = client.get("/service/tickets/count")

        assert result == {"count": 5}
        assert mock_urlopen.call_count == 2

    def test_exhausts_retries_and_raises_cwerror(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        def make_error():
            return urllib.error.HTTPError(
                url="https://x", code=503, msg="Service Unavailable",
                hdrs=None, fp=BytesIO(b"unavailable"),
            )

        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep"):
            mock_urlopen.side_effect = [make_error(), make_error(), make_error()]
            with pytest.raises(CWError):
                client.get("/service/tickets/count")
        assert mock_urlopen.call_count == 3

    def test_non_retryable_400_raises_immediately(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        error = urllib.error.HTTPError(
            url="https://x", code=400, msg="Bad Request",
            hdrs=None, fp=BytesIO(b"Cannot route. Codebase/company is invalid"),
        )
        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep"):
            mock_urlopen.side_effect = [error]
            with pytest.raises(CWError):
                client.get("/service/tickets/count")
        assert mock_urlopen.call_count == 1


class TestRateLimitRetry:
    def test_429_with_retry_after_seconds_waits_about_that_long(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        success = _fake_response({"ok": True})

        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep") as mock_sleep:
            mock_urlopen.side_effect = [_http_error(429, retry_after="30"), success]
            result = client.get("/service/tickets")

        assert result == {"ok": True}
        waited = mock_sleep.call_args[0][0]
        assert 30.0 <= waited < 30.0 + 10.0  # base 30s plus up to _RATE_LIMIT_JITTER_SECONDS

    def test_429_with_http_date_retry_after_computes_positive_wait(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        success = _fake_response({"ok": True})
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=45), usegmt=True)

        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep") as mock_sleep:
            mock_urlopen.side_effect = [_http_error(429, retry_after=future), success]
            client.get("/service/tickets")

        waited = mock_sleep.call_args[0][0]
        assert waited > 0
        assert waited < 60  # sane, roughly the ~45s until the target date

    def test_429_with_malformed_retry_after_falls_back_to_floor(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        success = _fake_response({"ok": True})

        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep") as mock_sleep:
            mock_urlopen.side_effect = [_http_error(429, retry_after="soon"), success]
            client.get("/service/tickets")

        waited = mock_sleep.call_args[0][0]
        assert waited >= _RATE_LIMIT_FLOOR_SECONDS

    def test_429_without_retry_after_waits_at_least_the_floor(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        success = _fake_response({"ok": True})

        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep") as mock_sleep:
            mock_urlopen.side_effect = [_http_error(429), success]
            client.get("/service/tickets")

        waited = mock_sleep.call_args[0][0]
        assert waited >= _RATE_LIMIT_FLOOR_SECONDS

    def test_500_still_uses_short_exponential_backoff(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        success = _fake_response({"ok": True})

        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep") as mock_sleep:
            mock_urlopen.side_effect = [_http_error(500), success]
            client.get("/service/tickets")

        waited = mock_sleep.call_args[0][0]
        assert waited < _RATE_LIMIT_FLOOR_SECONDS  # proves the 30s floor did not leak into 5xx

    def test_429_then_429_then_success_returns_payload(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        success = _fake_response({"ok": True})

        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep"):
            mock_urlopen.side_effect = [
                _http_error(429, retry_after="1"),
                _http_error(429, retry_after="1"),
                success,
            ]
            result = client.get("/service/tickets")

        assert result == {"ok": True}
        assert mock_urlopen.call_count == 3

    def test_total_wait_cap_raises_cwerror_naming_the_cap(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)

        # Retry-After of 100s per attempt blows through the total wait
        # cap in two attempts, well before the _MAX_ATTEMPTS_RATE_LIMIT
        # budget of 8 would ever be reached.
        errors = [_http_error(429, retry_after="100") for _ in range(_MAX_ATTEMPTS_RATE_LIMIT)]
        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep"):
            mock_urlopen.side_effect = errors
            with pytest.raises(CWError, match=str(_MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS)):
                client.get("/service/tickets")

    def test_rate_limit_wait_jitter_varies_and_is_never_negative(self):
        waits = {_rate_limit_wait_seconds("30") for _ in range(20)}
        assert all(w >= 0 for w in waits)
        assert len(waits) > 1


class TestPrivateKeyRedaction:
    def test_private_key_never_appears_in_error_message(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        error = urllib.error.HTTPError(
            url="https://x", code=400, msg="Bad Request",
            hdrs=None,
            fp=BytesIO(f"leaked {ENV_VALUES['CW_MANAGE_PRIVATE_KEY']} in body".encode()),
        )
        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep"):
            mock_urlopen.side_effect = [error]
            with pytest.raises(CWError) as exc_info:
                client.get("/service/tickets/count")

        message = str(exc_info.value)
        assert ENV_VALUES["CW_MANAGE_PRIVATE_KEY"] not in message
        assert exc_info.value.body is not None
        assert ENV_VALUES["CW_MANAGE_PRIVATE_KEY"] not in exc_info.value.body


class TestTicketHelpers:
    def test_tickets_since_builds_date_entered_condition(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _fake_response([{"id": 1}])
            client.tickets_since("2026-08-15T00:00:00Z")
        called_url = mock_urlopen.call_args[0][0].full_url
        assert "dateEntered" in called_url
        assert "2026-08-15T00%3A00%3A00Z" in called_url or "2026-08-15T00:00:00Z" in called_url

    def test_notes_for_tickets_returns_dict_keyed_by_id(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _fake_response([{"id": 1, "text": "note"}])
            result = client.notes_for_tickets([101, 102])
        assert set(result.keys()) == {101, 102}
        assert result[101] == [{"id": 1, "text": "note"}]
