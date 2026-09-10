#!/usr/bin/env python3
"""
Live ConnectWise Manage REST client for Agent Penny.

Every other consumer of ticket data in this repo (board_watch.py,
trend_detection.py) reads a recorded callback payload log instead of
calling CW directly, because inbound CW ingress used to be down and no
live client existed. This module is that missing client: it exists so a
trend-detection feature can pull assignee, notes, and time-entry data
that the callback log never carried in the first place.

TRAP, confirmed live on 2026-08-18: CW_MANAGE_BASE_URL in .env is
"api-na.myconnectwise.net/v4_6_release/apis/3.0/" - it has NO scheme
AND already contains the versioned API path. Naively doing
f"https://{base}/apis/3.0/{path}" doubles the path and CW's gateway
returns "400 Cannot route. Codebase/company is invalid", which reads
exactly like a bad company id or bad key even though the credentials
are fine. _build_api_root() below prepends a scheme only if missing and
never appends the API path segment - the .env value already has it.

Standard library only: urllib.request, base64, json, concurrent.futures.
No `requests` dependency, matching the rest of this repo's cron/ tree.

create_ticket() is included for a later escalation step and is
deliberately not called anywhere yet.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_ENV_VARS = (
    "CW_MANAGE_COMPANY_ID",
    "CW_MANAGE_PUBLIC_KEY",
    "CW_MANAGE_PRIVATE_KEY",
    "CW_MANAGE_CLIENT_ID",
    "CW_MANAGE_BASE_URL",
)

_MAX_ATTEMPTS = 3
_RETRY_STATUS = {429, 500, 502, 503, 504}

# 429 means "wait longer", not "failed" - CW recovers from a rate limit on
# its own timeline, so it earns a much larger retry budget than a genuine
# 5xx failure. Confirmed live 2026-08-18: the trend job's ~1000 per-ticket
# note requests trip this limiter, and 3 short-backoff attempts (under 10s
# total) exhausted the budget before CW's own 30s cooldown even elapsed.
_MAX_ATTEMPTS_RATE_LIMIT = 8

# CW's own 429 body says "Please try again in 30 seconds" - that is the
# floor used whenever Retry-After is missing or unparseable.
_RATE_LIMIT_FLOOR_SECONDS = 30.0

# Hard cap on cumulative time spent waiting on 429s for a single request,
# so a limiter that never relents can't hang a cron job forever.
_MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS = 180.0

# Spreads parallel notes_for_tickets workers so they don't all wake at the
# same instant and immediately re-trip the limit (thundering herd).
_RATE_LIMIT_JITTER_SECONDS = 5.0


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header as either delay-seconds or an HTTP-date.

    Returns None for a missing or malformed value so the caller can fall
    back to CW's own 30-second floor instead of crashing on bad input.
    """
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return seconds if seconds >= 0 else 0.0
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = (target - datetime.now(timezone.utc)).total_seconds()
    return max(delta, 0.0)


def _rate_limit_wait_seconds(retry_after_header: str | None) -> float:
    """How long to wait after a 429, honoring Retry-After with a 30s floor.

    Jitter is added on top so multiple notes_for_tickets workers hitting
    the limit at once don't all sleep the identical duration and wake in
    lockstep, which would just re-trip the limiter together.
    """
    parsed = _parse_retry_after(retry_after_header)
    base = parsed if parsed is not None else _RATE_LIMIT_FLOOR_SECONDS
    return base + random.uniform(0, _RATE_LIMIT_JITTER_SECONDS)


class CWError(Exception):
    """Raised for any ConnectWise API failure: missing creds, HTTP error, timeout.

    Carries status and body so callers can branch on them, but the
    private key is never included - see _redact() below.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _redact(text: str, private_key: str) -> str:
    """Strip a private key out of a string before it can reach a log line or exception."""
    if not private_key:
        return text
    return text.replace(private_key, "***REDACTED***")


def _load_env_file(env_path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a .env file. Missing file yields an empty dict."""
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _build_api_root(raw_base_url: str) -> str:
    """Normalize CW_MANAGE_BASE_URL into a usable API root.

    See the module docstring TRAP note: the .env value has no scheme but
    already carries the /v4_6_release/apis/3.0 path, so this only adds a
    scheme when missing and never appends the API path itself.
    """
    root = raw_base_url.strip()
    if not root.startswith("http://") and not root.startswith("https://"):
        root = f"https://{root}"
    return root.rstrip("/")


class CWClient:
    """Thin REST client for ConnectWise Manage, credentials loaded from .env.

    The gateway process is known not to export CW_MANAGE_* vars, so file
    loading is mandatory rather than a fallback. A real process env var
    still wins over the file, for local overrides during testing.
    """

    def __init__(self, *, env_path: Path | None = None, timeout: int = 30) -> None:
        import os

        resolved_env_path = env_path or (get_hermes_home() / ".env")
        file_values = _load_env_file(resolved_env_path)

        resolved: dict[str, str] = {}
        for name in _ENV_VARS:
            value = os.environ.get(name, "").strip() or file_values.get(name, "").strip()
            if not value:
                raise CWError(f"Missing required ConnectWise credential: {name} (checked process env and {resolved_env_path})")
            resolved[name] = value

        self._company_id = resolved["CW_MANAGE_COMPANY_ID"]
        self._public_key = resolved["CW_MANAGE_PUBLIC_KEY"]
        self._private_key = resolved["CW_MANAGE_PRIVATE_KEY"]
        self._client_id = resolved["CW_MANAGE_CLIENT_ID"]
        self._api_root = _build_api_root(resolved["CW_MANAGE_BASE_URL"])
        self._timeout = timeout

        auth_pair = f"{self._company_id}+{self._public_key}:{self._private_key}"
        self._auth_header = "Basic " + base64.b64encode(auth_pair.encode()).decode()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._auth_header,
            "clientId": self._client_id,
            "Accept": "application/json",
        }

    def get(self, path: str, **params: Any) -> Any:
        """GET a CW Manage path with query params, retrying transient failures.

        Raises CWError on any non-2xx response after retries are exhausted.
        Never returns a silent empty result for an auth failure - that
        would be indistinguishable from CW legitimately returning zero
        rows, which callers rely on.
        """
        url = f"{self._api_root}/{path.lstrip('/')}"
        if params:
            # Built as a plain string, not inline in the f-string expression
            # below: a backslash-escaped quote inside an f-string expression
            # is a SyntaxError on Python < 3.12 (PEP 701 relaxed this in
            # 3.12), and this repo's pyproject.toml declares 3.11 support.
            # The quote character itself needs no escaping here anyway --
            # it is embedded in a single-quoted literal.
            safe_chars = '[]()<>=,"'
            query = "&".join(f"{key}={quote(str(value), safe=safe_chars)}" for key, value in params.items())
            url = f"{url}?{query}"

        last_error: Exception | None = None
        rate_limit_total_wait = 0.0
        attempt = 0
        while True:
            attempt += 1
            request = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    body = response.read().decode("utf-8")
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429:
                    if attempt >= _MAX_ATTEMPTS_RATE_LIMIT:
                        raise CWError(
                            _redact(
                                f"CW GET {path} failed: exhausted {_MAX_ATTEMPTS_RATE_LIMIT} "
                                f"rate-limit retries: {exc.code} {body}",
                                self._private_key,
                            ),
                            status=exc.code,
                            body=_redact(body, self._private_key),
                        ) from None
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    wait = _rate_limit_wait_seconds(retry_after)
                    rate_limit_total_wait += wait
                    if rate_limit_total_wait > _MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS:
                        raise CWError(
                            f"CW GET {path} gave up after exceeding the "
                            f"{_MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS}s total rate-limit wait cap "
                            f"(attempt {attempt}/{_MAX_ATTEMPTS_RATE_LIMIT})",
                            status=exc.code,
                        ) from None
                    logger.warning(
                        "CW GET %s rate limited (429), waiting %.1fs (attempt %s/%s)",
                        path, wait, attempt, _MAX_ATTEMPTS_RATE_LIMIT,
                    )
                    time.sleep(wait)
                    last_error = exc
                    continue
                if exc.code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                    wait = (2 ** (attempt - 1)) + random.uniform(0, 1.0)
                    logger.warning(
                        "CW GET %s failed with %s (attempt %s/%s), retrying in %.1fs",
                        path, exc.code, attempt, _MAX_ATTEMPTS, wait,
                    )
                    time.sleep(wait)
                    last_error = exc
                    continue
                raise CWError(
                    _redact(f"CW GET {path} failed: {exc.code} {body}", self._private_key),
                    status=exc.code,
                    body=_redact(body, self._private_key),
                ) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < _MAX_ATTEMPTS:
                    wait = (2 ** (attempt - 1)) + random.uniform(0, 1.0)
                    logger.warning(
                        "CW GET %s network error (attempt %s/%s): %s, retrying in %.1fs",
                        path, attempt, _MAX_ATTEMPTS, exc, wait,
                    )
                    time.sleep(wait)
                    last_error = exc
                    continue
                raise CWError(_redact(f"CW GET {path} failed: {exc}", self._private_key)) from None

    def paged(self, path: str, conditions: str, *, page_size: int = 1000) -> list[dict]:
        """Fetch every page of a list endpoint, stopping when a short page comes back."""
        results: list[dict] = []
        page = 1
        while True:
            batch = self.get(path, conditions=conditions, pageSize=page_size, page=page, orderBy="id asc")
            if not batch:
                break
            results.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return results

    def tickets_since(self, since_iso: str) -> list[dict]:
        """All service tickets with dateEntered after since_iso (UTC ISO 8601)."""
        return self.paged("/service/tickets", f"dateEntered>[{since_iso}]")

    def ticket_notes(self, ticket_id: int) -> list[dict]:
        """All notes on a single ticket, including detail description and resolution flags."""
        return self.get(f"/service/tickets/{ticket_id}/notes", pageSize=200)

    def notes_for_tickets(self, ticket_ids: Sequence[int], *, workers: int = 4) -> dict[int, list[dict]]:
        """Fetch notes for many tickets in parallel, keyed by ticket id.

        A failure on one ticket's notes raises rather than silently
        dropping that ticket from the result - partial data must be
        visible as an error, not absorbed as "no notes".

        Default lowered from 12 to 4 after a live 429 was observed
        2026-08-18: 12 parallel workers against ~1000 tickets tripped
        CW's rate limiter. Still overridable by the caller.
        """
        notes_by_ticket: dict[int, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_id = {executor.submit(self.ticket_notes, ticket_id): ticket_id for ticket_id in ticket_ids}
            for future in as_completed(future_to_id):
                ticket_id = future_to_id[future]
                notes_by_ticket[ticket_id] = future.result()
        return notes_by_ticket

    def time_entries_since(self, since_iso: str) -> list[dict]:
        """All time entries with dateEntered after since_iso (UTC ISO 8601)."""
        return self.paged("/time/entries", f"dateEntered>[{since_iso}]")

    def send(self, method: str, path: str, payload: Any) -> Any:
        """POST or PATCH a JSON body. Used by the outage writeback.

        Kept as one narrow helper rather than separate post/patch methods so
        there is exactly one place where this client is capable of changing
        production data, and one place to audit.
        """
        if method not in ("POST", "PATCH"):
            raise ValueError(f"unsupported write method: {method}")

        url = f"{self._api_root}/{path.lstrip('/')}"
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CWError(
                _redact(f"CW {method} {path} failed: {exc.code} {detail}", self._private_key),
                status=exc.code,
                body=_redact(detail, self._private_key),
            ) from None

    def create_ticket(
        self,
        *,
        summary: str,
        initial_description: str,
        board_name: str,
        company_id: int,
        priority_name: str | None = None,
    ) -> dict:
        """Create a service ticket. Not called anywhere yet - reserved for a later escalation step.

        Looks up the board id by name instead of hardcoding one, since
        board ids are environment-specific.
        """
        boards = self.get("/service/boards", conditions=f'name="{board_name}"')
        if not boards:
            raise CWError(f"No ConnectWise board found matching name={board_name!r}")
        board_id = boards[0]["id"]

        payload: dict[str, Any] = {
            "summary": summary,
            "board": {"id": board_id},
            "company": {"id": company_id},
            "initialDescription": initial_description,
        }
        if priority_name:
            payload["priority"] = {"name": priority_name}

        url = f"{self._api_root}/service/tickets"
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise CWError(
                _redact(f"CW POST /service/tickets failed: {exc.code} {body}", self._private_key),
                status=exc.code,
                body=_redact(body, self._private_key),
            ) from None
