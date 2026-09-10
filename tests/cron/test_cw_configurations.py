"""Unit tests for cron/cw_configurations.py. No live network calls."""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

from cron.cw_client import CWClient
from cron.cw_configurations import configurations_for_tickets, normalize_configurations

ENV_VALUES = {
    "CW_MANAGE_COMPANY_ID": "henssler",
    "CW_MANAGE_PUBLIC_KEY": "pubkey123",
    "CW_MANAGE_PRIVATE_KEY": "supersecretprivatekey",
    "CW_MANAGE_CLIENT_ID": "client-uuid",
    "CW_MANAGE_BASE_URL": "api-na.myconnectwise.net/v4_6_release/apis/3.0/",
}

# The real shape confirmed live 2026-08-19: a ticket-configurations link
# record carries only id, deviceIdentifier, and _info.name - never a
# top-level name, type, company, or site.
REAL_LINK_RECORD = {
    "id": 1420,
    "deviceIdentifier": "1ec0a90c-a6ac-4cd2-ad56-cd1ab4a4b15c",
    "_info": {
        "name": "GWH-PW0F9N7V",
        "configuration_href": "https://api-na.myconnectwise.net/v4_6_release/apis/3.0//company/configurations/1420",
    },
}

REAL_CI_RECORD = {
    "id": 1420,
    "name": "GWH-PW0F9N7V",
    "type": {"id": 24, "name": "Managed Workstation"},
    "company": {"id": 250, "name": "Henssler Financial"},
    "site": {"id": 1000, "name": "Kennesaw HQ"},
}


def _client(tmp_path, monkeypatch):
    for name in ENV_VALUES:
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{k}={v}" for k, v in ENV_VALUES.items()))
    return CWClient(env_path=env_path)


def _fake_response(payload):
    body = json.dumps(payload).encode()
    mock_response = MagicMock()
    mock_response.read.return_value = body
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = False
    return mock_response


def _routed_urlopen(routes: dict):
    """A urlopen side_effect that dispatches on a substring of the request URL.

    `routes` maps a URL substring to either a payload (200 response) or
    an exception instance/class to raise.
    """

    def _handle(request, timeout=None):
        url = request.full_url
        for substring, outcome in routes.items():
            if substring in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return _fake_response(outcome)
        raise AssertionError(f"unrouted URL in test: {url}")

    return _handle


class TestConfigurationsForTickets:
    def test_returns_dict_keyed_by_ticket_id(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        routes = {
            "/service/tickets/": [REAL_LINK_RECORD],
            "/company/configurations/1420": REAL_CI_RECORD,
        }
        with patch("urllib.request.urlopen", side_effect=_routed_urlopen(routes)):
            result = configurations_for_tickets(client, [101, 102])
        assert set(result.keys()) == {101, 102}
        assert result[101][0]["id"] == 1420

    def test_ticket_with_no_configurations_yields_empty_list_not_dropped(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _fake_response([])
            result = configurations_for_tickets(client, [101])
        assert result == {101: []}

    def test_failure_on_one_ticket_raises_not_silently_empty(self, tmp_path, monkeypatch):
        import urllib.error

        client = _client(tmp_path, monkeypatch)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://x", code=500, msg="err", hdrs=None, fp=BytesIO(b"{}")
            )
            try:
                configurations_for_tickets(client, [101])
                assert False, "expected CWError to propagate"
            except Exception as exc:
                assert "500" in str(exc)

    def test_exhausted_rate_limit_retry_raises_and_is_not_cached_as_success(self, tmp_path, monkeypatch):
        """Regression test for the actual production bug: a ticket whose
        configurations fetch never succeeds must never be indistinguishable
        from a ticket with genuinely zero configurations. It must raise, and
        the caller (trend_corpus_loader.load_corpus) must never write a
        result to the cache when this raises - proven here by asserting the
        function itself raises before returning anything to cache."""
        import urllib.error

        client = _client(tmp_path, monkeypatch)
        monkeypatch.setattr("cron.cw_client.time.sleep", lambda *_: None)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://x", code=429, msg="rate limited", hdrs=None,
                fp=BytesIO(b'{"message": "Please try again in 30 seconds"}'),
            )
            try:
                configurations_for_tickets(client, [101])
                assert False, "expected CWError to propagate on exhausted rate-limit retries"
            except AssertionError:
                raise
            except Exception as exc:
                assert "429" in str(exc) or "rate" in str(exc).lower() or "exhausted" in str(exc).lower()

    def test_resolves_type_company_site_from_the_full_ci_record(self, tmp_path, monkeypatch):
        """A raw link record (id, deviceIdentifier, _info.name only) must
        come back enriched with type/company/site pulled from the full CI
        record, not dropped for lacking a top-level name."""
        client = _client(tmp_path, monkeypatch)
        routes = {
            "/service/tickets/": [dict(REAL_LINK_RECORD)],
            "/company/configurations/1420": REAL_CI_RECORD,
        }
        with patch("urllib.request.urlopen", side_effect=_routed_urlopen(routes)):
            result = configurations_for_tickets(client, [101])
        cfg = result[101][0]
        assert cfg["type"] == {"id": 24, "name": "Managed Workstation"}
        assert cfg["company"] == {"id": 250, "name": "Henssler Financial"}
        assert cfg["site"] == {"id": 1000, "name": "Kennesaw HQ"}
        assert cfg["deviceIdentifier"] == "1ec0a90c-a6ac-4cd2-ad56-cd1ab4a4b15c"

    def test_ci_lookup_failure_degrades_but_keeps_device_identity(self, tmp_path, monkeypatch):
        """Losing type/company/site to a failed CI lookup must never cost
        the device identity already known from the link record."""
        import urllib.error

        client = _client(tmp_path, monkeypatch)
        routes = {
            "/service/tickets/": [dict(REAL_LINK_RECORD)],
            "/company/configurations/1420": urllib.error.HTTPError(
                url="https://x", code=404, msg="not found", hdrs=None, fp=BytesIO(b"{}")
            ),
        }
        with patch("urllib.request.urlopen", side_effect=_routed_urlopen(routes)):
            result = configurations_for_tickets(client, [101])
        cfg = result[101][0]
        assert cfg["id"] == 1420
        assert cfg["deviceIdentifier"] == "1ec0a90c-a6ac-4cd2-ad56-cd1ab4a4b15c"
        assert cfg["type"] is None
        normalized = normalize_configurations([cfg])
        assert normalized == [
            {
                "id": 1420,
                "name": "GWH-PW0F9N7V",
                "type": "",
                "company": "",
                "site": "",
                "device_identifier": "1ec0a90c-a6ac-4cd2-ad56-cd1ab4a4b15c",
            }
        ]

    def test_ci_resolved_once_and_reused_across_tickets_sharing_it(self, tmp_path, monkeypatch):
        """CIs repeat heavily across tickets - the CI detail endpoint must
        be hit once per distinct CI id, not once per ticket."""
        client = _client(tmp_path, monkeypatch)
        ci_calls = []

        def _handle(request, timeout=None):
            url = request.full_url
            if "/company/configurations/1420" in url:
                ci_calls.append(url)
                return _fake_response(REAL_CI_RECORD)
            if "/service/tickets/" in url:
                return _fake_response([dict(REAL_LINK_RECORD)])
            raise AssertionError(f"unrouted URL in test: {url}")

        with patch("urllib.request.urlopen", side_effect=_handle):
            configurations_for_tickets(client, [101, 102, 103])
        assert len(ci_calls) == 1

    def test_only_successful_ci_resolutions_are_written_to_ci_cache(self, tmp_path, monkeypatch):
        """A failed CI lookup must not poison ci_cache - it must be
        retried next time, not permanently treated as resolved-to-nothing."""
        import urllib.error

        client = _client(tmp_path, monkeypatch)
        routes = {
            "/service/tickets/": [dict(REAL_LINK_RECORD)],
            "/company/configurations/1420": urllib.error.HTTPError(
                url="https://x", code=404, msg="not found", hdrs=None, fp=BytesIO(b"{}")
            ),
        }
        ci_cache: dict = {}
        with patch("urllib.request.urlopen", side_effect=_routed_urlopen(routes)):
            configurations_for_tickets(client, [101], ci_cache=ci_cache)
        assert ci_cache == {}


def result_never_bound_on_raise(exc):
    """`result` never gets bound in the try block above when the raise
    happens before assignment - this helper exists only so the except
    branch has something meaningful to assert on `exc` for readability."""
    assert "429" in str(exc) or "rate" in str(exc).lower() or "exhausted" in str(exc).lower()
    return True


class TestNormalizeConfigurations:
    def test_extracts_id_name_type_company_site(self):
        raw = [{
            "id": 5, "name": "GWH-PW0AYB5J", "type": {"name": "Laptop"},
            "company": {"name": "Henssler"}, "site": {"name": "Main"},
        }]
        assert normalize_configurations(raw) == [
            {
                "id": 5, "name": "GWH-PW0AYB5J", "type": "Laptop", "company": "Henssler",
                "site": "Main", "device_identifier": "",
            }
        ]

    def test_empty_list_is_valid_not_an_error(self):
        assert normalize_configurations([]) == []
        assert normalize_configurations(None) == []

    def test_malformed_entries_skipped(self):
        raw = [None, {}, {"name": None}, "not a dict", {"name": "OK-1"}]
        assert normalize_configurations(raw) == [
            {"id": None, "name": "OK-1", "type": "", "company": "", "site": "", "device_identifier": ""}
        ]

    def test_real_link_record_shape_is_not_dropped(self):
        """Regression test for the actual production bug: a real
        ticket-configurations link record (id + deviceIdentifier + _info.name,
        NO top-level name) was silently dropped by the old top-level-name-only
        check, turning every real ticket's configurations into []. This must
        yield one entry, not zero."""
        assert normalize_configurations([REAL_LINK_RECORD]) == [
            {
                "id": 1420,
                "name": "GWH-PW0F9N7V",
                "type": "",
                "company": "",
                "site": "",
                "device_identifier": "1ec0a90c-a6ac-4cd2-ad56-cd1ab4a4b15c",
            }
        ]

    def test_record_with_device_identifier_but_no_name_is_kept(self):
        """Device identity alone is a usable identifier - it must not be
        discarded just because a name never resolved."""
        raw = [{"id": 9, "deviceIdentifier": "abc-123"}]
        assert normalize_configurations(raw) == [
            {"id": 9, "name": "", "type": "", "company": "", "site": "", "device_identifier": "abc-123"}
        ]
