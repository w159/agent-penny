"""Generic webhook platform adapter: aiohttp server that validates HMAC-signed POSTs (GitHub, GitLab,
Svix, Linear, generic), renders payloads into agent prompts, and routes responses back (github_comment
or any gateway platform). Routes live under platforms.webhook.extra.routes: events (header filter),
secret (REQUIRED; "INSECURE_NO_AUTH" skips validation, loopback only), prompt template, skills,
deliver/deliver_extra, deliver_only (rendered prompt IS the message). Per-route rate limiting,
idempotency cache, body-size caps checked before reading. Generic HMAC V2 binds a timestamp for
replay protection; body-only V1 is deprecated but accepted with a warning."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import deque
from contextlib import nullcontext, suppress
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import quote

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import AUTONOMOUS_DELIVERY_METADATA_KEY, BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.webhook_filters import DEFAULT_SCRIPT_TIMEOUT_SECONDS, WebhookRouteProcessor
from gateway.response_filters import is_autonomous_silence_response

logger = logging.getLogger(__name__)


# Route-level card builders. A route opts in with ``card: <name>``; the named
# builder rewrites the agent's reply into prose plus a Python-built Adaptive
# Card fence, using the ticket facts the route script already produced.
#
# Why this exists: when the ROUTE PROMPT asked the model to author the card
# JSON itself, 7 of 22 stored card deliveries (32%) carried JSON the Teams
# adapter could not parse — a dropped "value": key, a body array closed one
# bracket early — and each one silently degraded to raw JSON posted in front
# of help-desk techs. ``json.dumps`` cannot emit a syntax error, so building
# the card here removes the failure class rather than reducing it.
_ROUTE_CARD_BUILDERS = {"cw_ticket": "plugins.platforms.teams.ticket_card:render_triage_message"}


def _apply_route_card(content: str, delivery: dict) -> str:
    """Rebuild the route's card in Python, or leave the content alone.

    Never raises and never suppresses, but the two failure modes differ.
    An unknown builder, a missing Teams plugin, or a builder that raises all
    return the model's own text unchanged. A missing or unusable ticket
    payload does NOT: the builder still runs, and returns its own rendering
    of the reply — the verdict token stripped off, no card attached — which
    is shorter than what came in. Either way a message goes out.
    Delivery-suppressing ``[SILENT]`` replies never reach here — ``send()``
    checks for silence before calling this.
    """
    builder_name = (delivery or {}).get("card") or ""
    target = _ROUTE_CARD_BUILDERS.get(builder_name)
    if not target:
        if builder_name:
            logger.warning("[webhook] Unknown card builder: %s", builder_name)
        return content
    try:
        module_path, func_name = target.split(":")
        module = importlib.import_module(module_path)
        return getattr(module, func_name)(content, delivery.get("payload") or {})
    except Exception:
        logger.exception(
            "[webhook] card builder %s failed — delivering the model's text as-is",
            builder_name,
        )
        return content


# _resolve_request_profile sentinel: /p/<profile>/ names a profile this gateway does not serve (→ 404);
# distinct from None (no prefix / default).
_PROFILE_REJECTED = object()
_UNPARSEABLE = object()

_BUILTIN_DELIVER_PLATFORMS = {
    "telegram", "discord", "slack", "signal", "sms", "whatsapp", "matrix", "mattermost",
    "homeassistant", "email", "dingtalk", "feishu", "wecom", "wecom_callback", "weixin",
    "bluebubbles", "qqbot", "yuanbao"}

# ``None`` → aiohttp binds BOTH address families. "0.0.0.0" is IPv4-only (unreachable on IPv6-only
# networks such as Fly.io 6PN); "::" becomes IPv6-only where the kernel sets IPV6_V6ONLY=1, breaking
# the 127.0.0.1 health check. Users can pin a host via ``platforms.webhook.extra.host``.
DEFAULT_HOST = None
DEFAULT_PORT = 8644
_INSECURE_NO_AUTH = "INSECURE_NO_AUTH"
# Subject suppression (route ``dedupe`` block) lives in the gateway_routing
# table under this scope. gateway_routing is namespaced by scope and the
# session store only ever rewrites its own sessions_dir scope, so these rows
# are invisible to — and untouched by — the routing index proper.
_SUPPRESSION_SCOPE = "webhook-subject-suppression"
# Six hours: long enough to collapse a subject's same-day churn (CW ticket
# 94265 produced 10 callbacks in 51 minutes), short enough that the next
# business day's genuinely new state still gets a voice.
DEFAULT_SUPPRESSION_WINDOW_SECONDS = 21600
# A turn holding a subject is exactly the case the full window does not cover:
# it leaves a subject unguarded for the whole length of a turn, so two
# callbacks about one ticket arriving seconds apart both passed the check and
# both spoke.  Measured on this gateway's own runs ("response ready:
# platform=webhook ... time=Xs", n=35 distinct turns): p50 11s, p95 41s, max
# 63s.  Five minutes is ~4.8x the slowest turn ever observed here, so a normal
# turn is always covered, and it is 1/72nd of the default window, so a process
# that dies mid-turn holding a reservation costs the ticket five minutes of
# silence rather than six hours.  Nothing renews it: a reservation always
# expires on its own even if no code ever runs to release it.
DEFAULT_INFLIGHT_RESERVATION_SECONDS = 300


def _suppression_session_key(route_name: str, subject: str) -> str:
    """Row key for one subject on one route.

    Both halves are percent-encoded so the separator is the only bare colon
    in the key.  Joining them raw let a route named ``a:b`` talking about
    ``c`` share a row with a route named ``a`` talking about ``b:c`` — and a
    shared row means one subject silences another.  Ticket ids and route
    names are usually colon-free, so this is normally a no-op.
    """
    return f"{quote(route_name, safe='')}:{quote(subject, safe='')}"


def _parse_suppression_row(raw: Any) -> Dict[str, Any]:
    """Decode one suppression row, or ``{}`` for anything unreadable.

    A corrupt row must not stop the lane: an empty dict reads as "this subject
    has never spoken", which delivers.  Over-reporting is the cheaper wrong
    answer here, same as everywhere else on this path.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _spoken_breakthroughs(row: Dict[str, Any]) -> set:
    """Escalation values this subject has already spent a message on.

    ``breakthroughs`` is the authoritative list.  The single ``breakthrough``
    field is the older row shape and is still read, so a row written before
    this change keeps suppressing its own repeat instead of speaking twice on
    the first event after an upgrade.
    """
    values = row.get("breakthroughs")
    if isinstance(values, list):
        return {str(v) for v in values if v}
    single = row.get("breakthrough")
    return {str(single)} if single else set()

_DYNAMIC_ROUTES_FILENAME = "webhook_subscriptions.json"
_RATE_WINDOW_SECONDS = 60.0
# Hosts that only serve same-machine connections; anything else is a public bind.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "ip6-localhost", "ip6-loopback"})
_V2_REPLAY_WINDOW_SECONDS = 300
_TEMPLATE_KEY_RE = re.compile(r"\{([a-zA-Z0-9_.]+)\}")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
# Credentials `gh` reads; a routed profile's github_comment must use its own, never the process env's.
_GH_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


def _is_loopback_host(host: Optional[str]) -> bool:
    """True when `host` binds only to the local machine (falsy → non-loopback: usually a public default bind)."""
    return bool(host) and host.strip().lower() in _LOOPBACK_HOSTS


def _hmac_str_equal(provided: str, expected: str) -> bool:
    """Timing-safe str equality tolerant of non-ASCII: ``compare_digest`` raises TypeError on non-ASCII
    str and ``provided`` is an attacker-controlled header, so compare as UTF-8 bytes to fail closed."""
    return hmac.compare_digest(provided.encode(), expected.encode())


def _hex_hmac(secret: str, data: bytes) -> str:
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def _timestamp_fresh(raw: str, stale_msg: str, *args) -> bool:
    """True when integer timestamp header *raw* is within the replay window; unparseable → False,
    stale → warn ``stale_msg % args`` and False."""
    try:
        age = abs(int(time.time()) - int(raw))
    except (TypeError, ValueError):
        return False
    if age > _V2_REPLAY_WINDOW_SECONDS:
        logger.warning(stale_msg, *args)
        return False
    return True


def _is_known_platform(name: str) -> bool:
    """Cross-platform delivery target: built-in names or plugin-registered platforms."""
    if name in _BUILTIN_DELIVER_PLATFORMS:
        return True
    with suppress(Exception):
        from gateway.platform_registry import platform_registry
        return platform_registry.is_registered(name)
    return False


def _json_error(message: str, status: int) -> "web.Response":
    return web.json_response({"error": message}, status=status)


def _peek_session_id(store, session_key: str):
    """Prefer the store's lock-held accessor; the private-path fallback is for older stores / test doubles."""
    if callable(peek := getattr(store, "peek_session_id", None)):
        return peek(session_key)
    if hasattr(store, "_ensure_loaded"):
        with suppress(Exception):
            store._ensure_loaded()
    entry = (getattr(store, "_entries", {}) or {}).get(session_key)
    return getattr(entry, "session_id", None) if entry else None


def check_webhook_requirements() -> bool:
    """Check if webhook adapter dependencies are available."""
    return AIOHTTP_AVAILABLE


def _validate_svix_signature(body: bytes, secret: str, msg_id: str, timestamp: str, signature_header: str) -> bool:
    """Svix-compatible signatures (AgentMail): base64 HMAC-SHA256 of "{id}.{timestamp}.{body}"."""
    if not (msg_id and timestamp and signature_header and secret):
        return False
    if not _timestamp_fresh(timestamp, "[webhook] Svix signature timestamp outside replay window"):
        return False
    if secret.startswith("whsec_"):
        try:
            key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        except (binascii.Error, ValueError):
            logger.debug("[webhook] Invalid whsec_ Svix signing secret")
            return False
    else:
        # Some providers document Svix-style headers but hand out raw shared secrets.
        logger.debug("[webhook] Validating Svix-style signature with raw secret")
        key = secret.encode()
    signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed_content, hashlib.sha256).digest()).decode()
    # Multiple space-separated "vN,<base64>" entries during secret rotation.
    for part in signature_header.split():
        version, _, signature = part.partition(",")
        if _ and version == "v1" and _hmac_str_equal(signature, expected):
            return True
    return False


class WebhookAdapter(BasePlatformAdapter):
    """Generic webhook receiver that triggers agent runs from HTTP POSTs."""

    # Event-triggered, no human present: startup auto-resume must FINISH the interrupted work, not ask "what next?".
    # The startup auto-resume turn must instruct the model to FINISH the interrupted work instead of
    # emitting an interactive acknowledgement that abandons the task (#57056).
    interactive_resume: bool = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBHOOK)
        extra = config.extra
        # Empty string / null host normalises to None ("bind all families").
        self._host: Optional[str] = extra.get("host", DEFAULT_HOST) or None
        self._port: int = int(extra.get("port", DEFAULT_PORT))
        self._global_secret: str = extra.get("secret", "")
        self._static_routes: Dict[str, dict] = extra.get("routes", {})
        self._dynamic_routes: Dict[str, dict] = {}
        self._dynamic_routes_mtime: float = 0.0
        self._routes: Dict[str, dict] = dict(self._static_routes)
        self._runner = None
        self._v1_signature_warned: set[str] = set()  # routes already warned about legacy V1 (once per route)
        # Keyed by session chat_id; read by EVERY send() (interim status messages AND the final
        # response) so never pop on send(). TTL-pruned on each POST.
        self._delivery_info: Dict[str, dict] = {}
        self._delivery_info_created: Dict[str, float] = {}
        self._delivery_info_order: Deque[tuple[float, str]] = deque()
        self.gateway_runner = None  # set externally; needed for cross-platform delivery
        # Idempotency: TTL cache of recently processed delivery IDs.
        self._seen_deliveries: Dict[str, float] = {}
        self._idempotency_ttl: int = 3600  # 1 hour
        self._seen_deliveries_next_prune_at: float = 0.0
        # Subject suppression: at most one delivery per subject (a ticket, an
        # incident, an order) per window.  Solves a different problem than
        # _seen_deliveries above — that one collapses HTTP retries of a single
        # delivery attempt, this one collapses genuinely distinct events that
        # are all about the same thing.  State lives in state.db so a gateway
        # restart does not re-open every window.
        self._suppression_window_default: int = int(
            extra.get("suppression_window_seconds", DEFAULT_SUPPRESSION_WINDOW_SECONDS)
        )
        self._inflight_reservation_seconds: int = int(
            extra.get("inflight_reservation_seconds", DEFAULT_INFLIGHT_RESERVATION_SECONDS)
        )
        self._suppression_db = None
        self._suppression_db_unavailable = False
        self._rate_counts: Dict[str, Deque[float]] = {}  # per-route hit timestamps in a fixed window
        self._rate_limit: int = int(extra.get("rate_limit", 30))  # per minute
        self._max_body_bytes: int = int(extra.get("max_body_bytes", 1_048_576))  # 1MB
        self._script_timeout_seconds: int = int(extra.get("script_timeout_seconds", DEFAULT_SCRIPT_TIMEOUT_SECONDS))
        self._route_processor = WebhookRouteProcessor(script_timeout_seconds=self._script_timeout_seconds)

    # --- Lifecycle ---

    def _validate_route(self, name: str, route: dict) -> None:
        """Startup validation: secret required; INSECURE_NO_AUTH only on loopback (crash early on a public footgun)."""
        secret = route.get("secret", self._global_secret)
        if not secret:
            raise ValueError(f"[webhook] Route '{name}' has no HMAC secret. Set 'secret' on the route or globally. "
                             f"For testing without auth, set secret to '{_INSECURE_NO_AUTH}'.")
        if secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
            raise ValueError(f"[webhook] Route '{name}' uses INSECURE_NO_AUTH secret but is bound to non-loopback "
                             f"host '{self._host}'. INSECURE_NO_AUTH is for local testing only. "
                             f"Refusing to start to prevent accidental exposure.")
        if route.get("deliver_only"):
            deliver = route.get("deliver", "log")
            if not deliver or deliver == "log":
                raise ValueError(f"[webhook] Route '{name}' has deliver_only=true but deliver is '{deliver}'. Direct "
                                 f"delivery requires a real target (telegram, discord, slack, github_comment, etc.).")

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._reload_dynamic_routes()
        for name, route in self._routes.items():
            self._validate_route(name, route)
        # client_max_size enforces the cap on every read path, including chunked bodies without
        # Content-Length that bypass the header check.
        app = web.Application(client_max_size=self._max_body_bytes)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/webhooks/{route_name}", self._handle_webhook)
        # /p/<profile>/ routes the event to that profile (honored only under gateway.multiplex_profiles).
        app.router.add_post("/p/{profile}/webhooks/{route_name}", self._handle_webhook)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # SO_REUSEADDR: on macOS (BSD) two wildcard/specific sockets can silently split traffic while
        # both report success → disable. On Linux it only permits rebinding past TIME_WAIT (a quick
        # restart would otherwise fail to bind for ~60s) → keep the default.
        site = web.TCPSite(self._runner, self._host, self._port,
                           reuse_address=False if sys.platform == "darwin" else None)
        try:
            await site.start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            logger.error("[webhook] Could not bind %s:%d: %s. Set a different host or port in config.yaml under "
                         "platforms.webhook.extra.", self._host or "all IPv4+IPv6 interfaces", self._port, exc)
            return False
        self._mark_connected()
        logger.info("[webhook] Listening on %s:%d — routes: %s", self._host or "* (all interfaces, IPv4+IPv6)",
                    self._port, ", ".join(self._routes.keys()) or "(none configured)")
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        logger.info("[webhook] Disconnected")

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Deliver the agent's response to the destination stored for ``chat_id``
        (``webhook:{route}:{delivery_id}``) — read with ``.get()``, never popped."""
        # Autonomous lane (no human reader): the loose marker matcher shared with cron (marker on its own
        # first/last line), because models add a sentence explaining why they stayed quiet, which the
        # interactive exact-match rule would deliver.
        if is_autonomous_silence_response(content):
            logger.info("[webhook] Response for %s is a silence marker — not delivering", chat_id)
            # The turn produced nothing to say, so the subject stops being
            # held right here instead of waiting out the reservation — this is
            # what keeps silence costing nothing for the NEXT event.
            self._release_subject_reservation(self._delivery_info.get(chat_id), consume=False)
            return SendResult(success=True)
        delivery = self._delivery_info.get(chat_id, {})
        deliver_type = delivery.get("deliver", "log")

        # After the silence check on purpose: a card must never resurrect a
        # reply the route decided not to deliver.
        content = _apply_route_card(content, delivery)
        if deliver_type == "log":
            logger.info("[webhook] Response for %s: %s", chat_id, content[:200])
            self._commit_subject_delivery(delivery)
            return SendResult(success=True)
        if deliver_type == "github_comment":
            result = await self._deliver_github_comment(content, delivery)
        elif self.gateway_runner and _is_known_platform(deliver_type):
            result = await self._deliver_cross_platform(deliver_type, content, delivery)
        else:
            logger.warning("[webhook] Unknown deliver type: %s", deliver_type)
            return SendResult(success=False, error=f"Unknown deliver type: {deliver_type}")
        # Only a target that accepted the message opens the subject's window.
        # A rejected send left the channel just as quiet as a [SILENT] turn,
        # so it frees the subject rather than holding it for the full run.
        if result.success:
            self._commit_subject_delivery(delivery)
        else:
            self._release_subject_reservation(delivery, consume=False)
        return result

    def _prune_delivery_info(self, now: float) -> None:
        """Drop delivery_info entries older than the idempotency TTL (bounds the dict by ``rate_limit * TTL``
        even when runs never produce a final response)."""
        created, order = self._delivery_info_created, self._delivery_info_order
        if len(order) < len(created):
            order = self._delivery_info_order = deque(
                (at, key) for key, at in sorted(created.items(), key=lambda kv: kv[1]))
        cutoff = now - self._idempotency_ttl
        while order and order[0][0] < cutoff:
            created_at, key = order.popleft()
            if created.get(key) == created_at:
                self._delivery_info.pop(key, None)
                created.pop(key, None)

    def _prune_seen_deliveries(self, now: float) -> None:
        """Occasionally prune expired delivery IDs without scanning every POST."""
        if now < self._seen_deliveries_next_prune_at:
            return
        cutoff = now - self._idempotency_ttl
        for k in [k for k, t in self._seen_deliveries.items() if t < cutoff]:
            self._seen_deliveries.pop(k, None)
        self._seen_deliveries_next_prune_at = now + min(60.0, max(1.0, self._idempotency_ttl / 10))

    def _record_rate_limit_hit(self, route_name: str, now: float) -> bool:
        """Return True if route is still within limit after recording this hit."""
        if not isinstance(window := self._rate_counts.get(route_name), deque):
            window = self._rate_counts[route_name] = deque(window or ())
        cutoff = now - _RATE_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self._rate_limit:
            return False
        window.append(now)
        return True

    def _record_delivery_id(self, delivery_id: str, now: float) -> bool:
        """Return True when this delivery should be processed."""
        if (seen_at := self._seen_deliveries.get(delivery_id)) is not None and now - seen_at < self._idempotency_ttl:
            return False
        if seen_at is not None:
            self._seen_deliveries.pop(delivery_id, None)
        self._seen_deliveries[delivery_id] = now
        if len(self._seen_deliveries) > max(self._rate_limit * 2, 128):
            self._prune_seen_deliveries(now)
        return True

    def _suppression_store(self):
        """SessionDB holding the subject-suppression index, or None.

        Opened lazily so routes without a ``dedupe`` block never touch the
        DB, and failure to open degrades to "deliver everything" — a noisy
        lane is recoverable, a silently dropped lane is not.
        """
        if self._suppression_db is None and not self._suppression_db_unavailable:
            try:
                from hermes_state import SessionDB

                self._suppression_db = SessionDB()
            except Exception as e:
                self._suppression_db_unavailable = True
                logger.warning(
                    "[webhook] subject suppression off, state.db unavailable: %s", e
                )
        return self._suppression_db

    def _check_subject_suppression(
        self,
        route_name: str,
        route_config: dict,
        payload: dict,
        now: float,
        delivery_id: str = "",
    ) -> tuple[bool, dict]:
        """Decide whether this event may speak about its subject right now.

        Route config (all optional — absent ``key`` leaves the route
        completely unchanged)::

            dedupe:
              key: "{ticket_id}"          # prompt-template syntax
              window_seconds: 21600
              breakthrough_key: "{event}" # value that re-opens the window
              breakthrough_values: [reopened]

        The full window is still not opened until a message actually lands
        (``_commit_subject_delivery``).  Most events on a lane like this end
        in ``[SILENT]``; charging a silent turn for the window would suppress
        the next real escalation before the agent ever saw it.

        What this DOES write is a short in-flight reservation on the subject,
        so a second event arriving while the first turn is still running is
        held instead of starting a second turn about the same ticket.  The
        reservation is released the moment that turn ends without speaking
        (``_release_subject_reservation``), so silence still costs nothing.
        The read and the reservation write happen with no ``await`` between
        them, which on a single event loop makes the pair atomic against
        every other request in flight.

        Returns ``(allow, detail)``; ``detail`` carries subject, window and
        remaining seconds so the caller can say out loud why it went quiet.
        Every failure mode here (bad template, unreadable DB, corrupt row)
        allows the delivery: over-reporting is the cheaper wrong answer.
        """
        cfg = route_config.get("dedupe") or {}
        key_template = cfg.get("key")
        if not key_template:
            return True, {}

        subject = self._render_prompt(key_template, payload, "", "").strip()
        # An unresolved token renders as the literal "{ticket_id}" — that
        # would collapse every event on the route into one subject.
        if not subject or "{" in subject or subject.lower() in ("none", "null"):
            logger.warning(
                "[webhook] dedupe key %r did not resolve on route %s; delivering",
                key_template,
                route_name,
            )
            return True, {}

        window = int(cfg.get("window_seconds", self._suppression_window_default))
        if window <= 0:
            return True, {}

        db = self._suppression_store()
        if db is None:
            return True, {}

        breakthrough = ""
        bt_template = cfg.get("breakthrough_key")
        if bt_template:
            breakthrough = self._render_prompt(bt_template, payload, "", "").strip()
        allowed_breakthroughs = {
            str(v) for v in (cfg.get("breakthrough_values") or [])
        }

        session_key = _suppression_session_key(route_name, subject)
        detail: Dict[str, Any] = {
            "subject": subject,
            "window": window,
            "breakthrough": breakthrough,
        }
        try:
            entries = db.load_gateway_routing_entries(scope=_SUPPRESSION_SCOPE)
        except Exception as e:
            logger.warning("[webhook] suppression read failed, delivering: %s", e)
            return True, {}

        previous = _parse_suppression_row(entries.get(session_key))

        # A turn about this subject is already running.  It is reading the
        # same ticket this event describes, so whatever this event carries is
        # already in front of it — running a second turn just produces a
        # second Teams message about one ticket, which is the duplication the
        # window exists to prevent.
        reserved_until = float(previous.get("reserved_until") or 0.0)
        if reserved_until > now:
            detail["remaining"] = reserved_until - now
            detail["in_flight"] = True
            return False, detail

        delivered_at = float(previous.get("delivered_at") or 0.0)
        elapsed = now - delivered_at
        if delivered_at and 0 <= elapsed < window:
            # Break through only for an escalation the route named, and only
            # once per DISTINCT value — so a ticket that reopens twice still
            # speaks once.  Comparing against every value already spoken for,
            # not just the most recent one, is what makes that hold when a
            # second allowed escalation lands in between the two reopens.
            escalated = (
                breakthrough
                and breakthrough in allowed_breakthroughs
                and breakthrough not in _spoken_breakthroughs(previous)
            )
            if not escalated:
                detail["remaining"] = window - elapsed
                return False, detail
            detail["escalated"] = True

        # Everything the commit needs, so the delivery path never has to
        # re-render the templates against a payload it no longer holds.
        # ``token`` identifies THIS turn's reservation, so a late finisher
        # cannot release a hold that a newer turn has since taken.
        pending = {
            "session_key": session_key,
            "subject": subject,
            "route": route_name,
            "breakthrough": breakthrough,
            "window": window,
            "token": delivery_id or f"{session_key}@{now}",
        }
        self._reserve_subject(db, session_key, previous, pending, now)
        detail["pending"] = pending
        return True, detail

    def _reserve_subject(
        self, db, session_key: str, previous: dict, pending: dict, now: float
    ) -> None:
        """Hold the subject for the length of one turn.

        Merged onto the existing row rather than replacing it: an escalation
        that just broke through must not erase ``delivered_at`` or the
        escalations already spoken for, or the next repeat would speak again.
        """
        ttl = self._inflight_reservation_seconds
        if ttl <= 0:
            return
        row = dict(previous)
        row.update(
            {
                "subject": pending["subject"],
                "route": pending["route"],
                "window": pending["window"],
                "reserved_until": now + ttl,
                "reserved_by": pending["token"],
            }
        )
        row.setdefault("delivered_at", 0.0)
        try:
            db.save_gateway_routing_entry(
                session_key, json.dumps(row), scope=_SUPPRESSION_SCOPE
            )
        except Exception as e:
            # Failing to reserve means a concurrent event runs rather than
            # being held — noisy, not silent, which is the right failure.
            logger.warning("[webhook] suppression reserve failed: %s", e)

    def _delete_suppression_rows(self, db, keys: list) -> None:
        """SessionDB exposes no per-key delete for a scope; an atomic scope
        replace without the dropped keys is the equivalent."""
        entries = db.load_gateway_routing_entries(scope=_SUPPRESSION_SCOPE)
        remaining = {k: v for k, v in entries.items() if k not in set(keys)}
        if len(remaining) != len(entries):
            db.replace_gateway_routing_entries(remaining, scope=_SUPPRESSION_SCOPE)

    def _commit_subject_delivery(self, delivery: Optional[dict]) -> None:
        """Open the subject's window, now that a message has actually landed.

        Popped rather than read: ``send()`` fires once per outgoing message
        (interim status notices as well as the final answer), and the window
        should start at the first one instead of being pushed forward by
        each.  A turn that ends in silence never reaches here, so it costs
        the subject nothing.
        """
        if not isinstance(delivery, dict):
            return
        pending = delivery.pop("suppression", None)
        if not pending:
            return
        db = self._suppression_store()
        if db is None:
            return
        self._record_subject_delivery(db, pending, time.time())

    def _release_subject_reservation(
        self, delivery: Optional[dict], *, consume: bool = True
    ) -> None:
        """Give the subject its voice back after a turn that said nothing.

        Silence, a send the target rejected, and a run that raised all land
        here.  Only the turn that took the hold may drop it (``reserved_by``),
        so a zombie run finishing long after its reservation expired cannot
        unlock a subject that a newer turn is currently holding.

        ``consume=False`` frees the subject but leaves the pending entry in
        place, for the mid-run callers: ``send()`` fires once per outgoing
        message, and a rejected interim notice must not stop the real answer
        that follows it from opening the window.  The end-of-run callers, for
        which there is no "later message", consume it.

        A process that dies never reaches this at all; that case is covered by
        the reservation's own expiry, not by cleanup.
        """
        if not isinstance(delivery, dict):
            return
        pending = delivery.get("suppression")
        if not pending:
            return
        if consume:
            delivery.pop("suppression", None)
        db = self._suppression_store()
        if db is None:
            return
        session_key = pending["session_key"]
        try:
            entries = db.load_gateway_routing_entries(scope=_SUPPRESSION_SCOPE)
        except Exception as e:
            logger.warning("[webhook] suppression release skipped: %s", e)
            return
        row = _parse_suppression_row(entries.get(session_key))
        if row.get("reserved_by") != pending.get("token"):
            return
        try:
            if float(row.get("delivered_at") or 0.0) <= 0:
                # The row exists only to carry this reservation — nothing was
                # ever said about the subject, so leave no trace behind.
                self._delete_suppression_rows(db, [session_key])
                return
            row.pop("reserved_until", None)
            row.pop("reserved_by", None)
            db.save_gateway_routing_entry(
                session_key, json.dumps(row), scope=_SUPPRESSION_SCOPE
            )
        except Exception as e:
            # Same failure direction as everywhere else here: a reservation
            # left behind expires on its own within minutes.
            logger.warning("[webhook] suppression release failed: %s", e)

    def _record_subject_delivery(self, db, pending: dict, now: float) -> None:
        """Upgrade the in-flight hold to a full window, and prune expired rows."""
        session_key = pending["session_key"]
        window = pending["window"]
        # Read before writing so the escalations this subject has already
        # spoken for survive the upgrade; dropping them is what let a ticket
        # break through twice on the same value.
        try:
            current = _parse_suppression_row(
                db.load_gateway_routing_entries(scope=_SUPPRESSION_SCOPE).get(
                    session_key
                )
            )
        except Exception as e:
            logger.warning("[webhook] suppression read failed on commit: %s", e)
            current = {}
        spoken_for = _spoken_breakthroughs(current)
        current_delivered_at = float(current.get("delivered_at") or 0.0)
        current_window = float(current.get("window") or window)
        if not current_delivered_at or now - current_delivered_at >= current_window:
            # A new window: the escalations the last window spoke for do not
            # carry into it, or a reopen after the window lapsed would be
            # swallowed — the strongest signal this lane has.
            spoken_for = set()
        if pending["breakthrough"]:
            spoken_for.add(str(pending["breakthrough"]))
        entry = {
            "subject": pending["subject"],
            "route": pending["route"],
            "delivered_at": now,
            "breakthrough": pending["breakthrough"],
            "breakthroughs": sorted(spoken_for),
            "window": window,
        }
        try:
            db.save_gateway_routing_entry(
                session_key, json.dumps(entry), scope=_SUPPRESSION_SCOPE
            )
        except Exception as e:
            # Failing to record means the next event repeats rather than
            # suppressing — noisy, not silent, which is the right failure.
            logger.warning("[webhook] suppression write failed: %s", e)
            return

        # Read the index back instead of reusing the snapshot taken at check
        # time: a whole agent run sits between the two, and pruning against a
        # stale view can delete a row written during it.
        try:
            entries = db.load_gateway_routing_entries(scope=_SUPPRESSION_SCOPE)
        except Exception as e:
            logger.warning("[webhook] suppression prune skipped: %s", e)
            return

        # Each row carries the window it was written under, so pruning stays
        # correct when routes disagree about how long to stay quiet.
        stale = []
        for key, raw in entries.items():
            if key == session_key:
                continue
            try:
                row = json.loads(raw)
                # A subject whose turn is still running has a row with no
                # delivery on it yet; pruning by delivered_at alone would
                # treat that as ancient and free the subject mid-turn.
                if float(row.get("reserved_until") or 0.0) > now:
                    continue
                row_window = float(row.get("window") or window)
                if now - float(row.get("delivered_at") or 0.0) >= row_window:
                    stale.append(key)
            except (ValueError, TypeError):
                stale.append(key)
        if stale:
            try:
                self._delete_suppression_rows(db, stale)
            except Exception as e:
                logger.warning("[webhook] suppression prune failed: %s", e)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "webhook"}

    def toolsets_for_source(self, source) -> Optional[List[str]]:
        """Per-route ``toolsets`` override (config.yaml or a manual key in webhook_subscriptions.json —
        deliberately NOT settable via `hermes webhook subscribe`, so an agent-created subscription
        cannot self-grant tools)."""
        parts = str(getattr(source, "chat_id", "") or "").split(":", 2)
        if len(parts) < 2 or parts[0] != "webhook":
            return None
        route_config = self._routes.get(parts[1])
        toolsets = route_config.get("toolsets") if isinstance(route_config, dict) else None
        if not isinstance(toolsets, list):
            return None
        return [str(t).strip() for t in toolsets if str(t).strip()] or None

    # --- HTTP handlers ---

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "webhook"})

    def _dynamic_route_allowed(self, name: str, route: dict) -> bool:
        """An empty effective secret would make _handle_webhook skip HMAC validation → reject such
        dynamic routes; INSECURE_NO_AUTH is loopback-only."""
        effective_secret = route.get("secret", self._global_secret)
        if not effective_secret:
            logger.warning("[webhook] Dynamic route '%s' skipped: 'secret' is missing or empty. Set a valid HMAC "
                           "secret, or use '%s' to explicitly disable auth (testing only).", name, _INSECURE_NO_AUTH)
            return False
        if effective_secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
            logger.warning("[webhook] Dynamic route '%s' skipped: INSECURE_NO_AUTH is only allowed on loopback "
                           "hosts. Current host: '%s'.", name, self._host)
            return False
        return True

    def _reload_dynamic_routes(self) -> None:
        """Reload agent-created subscriptions from disk if the file changed."""
        from hermes_constants import get_hermes_home
        subs_path = get_hermes_home() / _DYNAMIC_ROUTES_FILENAME
        if not subs_path.exists():
            if self._dynamic_routes:
                self._dynamic_routes, self._routes = {}, dict(self._static_routes)
                logger.debug("[webhook] Dynamic subscriptions file removed, cleared dynamic routes")
            return
        try:
            mtime = subs_path.stat().st_mtime
            if mtime <= self._dynamic_routes_mtime:
                return  # No change
            data = json.loads(subs_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            self._dynamic_routes = {  # static routes take precedence
                k: v for k, v in data.items() if k not in self._static_routes and self._dynamic_route_allowed(k, v)}
            self._routes = {**self._dynamic_routes, **self._static_routes}
            self._dynamic_routes_mtime = mtime
            logger.info("[webhook] Reloaded %d dynamic route(s): %s", len(self._dynamic_routes),
                        ", ".join(self._dynamic_routes.keys()) or "(none)")
        except Exception as e:
            logger.error("[webhook] Failed to reload dynamic routes: %s", e)

    def _resolve_request_profile(self, request: "web.Request"):
        """Resolve + validate the /p/<profile>/ URL prefix: None (no prefix, or multiplexing off and the
        prefix names this gateway's own profile), the profile name (served under multiplexing), or
        ``_PROFILE_REJECTED`` (unknown / not served → 404)."""
        profile = (request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        cfg = getattr(self.gateway_runner, "config", None)
        if not getattr(cfg, "multiplex_profiles", False):
            # Only a self-referential prefix may fall through to the bare route; anything else fails
            # closed (silently ignoring the prefix served the owner's routes under another profile's URL).
            with suppress(Exception):
                from hermes_cli.profiles import profile_matches_home
                if profile_matches_home(profile):
                    return None
            return _PROFILE_REJECTED
        try:
            from hermes_cli.profiles import profiles_to_serve
            allowlist = getattr(cfg, "multiplex_profile_allowlist", None)
            served = {name for name, _ in profiles_to_serve(multiplex=True, profile_allowlist=allowlist)}
        except Exception:
            return _PROFILE_REJECTED
        return profile if profile in served else _PROFILE_REJECTED

    @staticmethod
    def _route_allows_profile(route_config: dict, request_profile: Optional[str]) -> bool:
        """Omitting ``profile`` binds a route to default; an explicit null/blank/non-string fails closed."""
        configured = route_config.get("profile") if "profile" in route_config else "default"
        if not isinstance(configured, str) or not configured.strip():
            return False
        return configured.strip() == (request_profile or "default")

    @staticmethod
    def _profile_scope(profile: Optional[str]):
        """Runtime scope for a resolved ``/p/<profile>/`` prefix; bare routes get a no-op."""
        if not profile or not isinstance(profile, str):
            return nullcontext()
        from gateway.run import _profile_runtime_scope
        from hermes_cli.profiles import get_profile_dir
        return _profile_runtime_scope(get_profile_dir(profile))

    async def _read_authenticated_body(self, request: "web.Request", route_name: str,
                                       route_config: dict) -> "tuple[Optional[bytes], Optional[web.Response]]":
        """Auth-before-body: size-cap, read, then HMAC-validate. Returns ``(body, None)`` or ``(None, response)``."""
        if (request.content_length or 0) > self._max_body_bytes:
            return None, _json_error("Payload too large", 413)
        try:
            raw_body = await request.read()
        except web.HTTPRequestEntityTooLarge:  # client_max_size tripped — chunked or lying Content-Length
            return None, _json_error("Payload too large", 413)
        except Exception as e:
            logger.error("[webhook] Failed to read body: %s", e)
            return None, _json_error("Bad request", 400)
        if len(raw_body) > self._max_body_bytes:  # defense in depth if the server-level limit was bypassed
            return None, _json_error("Payload too large", 413)
        # Missing/empty secrets fail closed here too (not only in connect()), so direct handler reuse
        # cannot become an unauthenticated dispatch surface.
        secret = route_config.get("secret", self._global_secret)
        if not secret:
            logger.error("[webhook] Route %s has no HMAC secret; refusing request", route_name)
            return None, _json_error("Webhook route is missing an HMAC secret", 403)
        if secret != _INSECURE_NO_AUTH and not self._validate_signature(request, raw_body, secret):
            logger.warning("[webhook] Invalid signature for route %s", route_name)
            return None, _json_error("Invalid signature", 401)
        return raw_body, None

    @staticmethod
    def _parse_body(raw_body: bytes) -> Any:
        """JSON, falling back to form-encoded; ``_UNPARSEABLE`` when neither parses."""
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            try:
                import urllib.parse
                return dict(urllib.parse.parse_qsl(raw_body.decode("utf-8")))
            except Exception:
                return _UNPARSEABLE

    async def _handle_deliver_only(self, prompt: str, payload: Any, route_config: dict, route_name: str,
                                   event_type: str, delivery_id: str, profile: Optional[str] = None,
                                   suppression: Optional[dict] = None) -> "web.Response":
        """deliver_only: the rendered prompt IS the message — skip the agent, reuse the same
        auth/rate-limit/idempotency/template pipeline."""
        delivery = {"deliver": route_config.get("deliver", "log"), "payload": payload, "profile": profile,
                    "deliver_extra": self._render_delivery_extra(route_config.get("deliver_extra", {}), payload),
                    # Consumed by the commit below when the target accepts.
                    "suppression": suppression}
        logger.info("[webhook] direct-deliver event=%s route=%s target=%s msg_len=%d delivery=%s", event_type,
                    route_name, delivery["deliver"], len(prompt), delivery_id)
        failed = {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id}
        try:
            result = await self._direct_deliver(prompt, delivery)
        except Exception:
            logger.exception("[webhook] direct-deliver failed route=%s delivery=%s", route_name, delivery_id)
            # Nothing landed, so the subject must not stay held: this lane
            # has no agent run and therefore no on_processing_complete.
            self._release_subject_reservation(delivery)
            return web.json_response(failed, status=502)
        if result.success:
            self._commit_subject_delivery(delivery)
            return web.json_response({"status": "delivered", "route": route_name, "target": delivery["deliver"],
                                      "delivery_id": delivery_id}, status=200)
        # Target rejected it — 502 with a generic error (don't leak adapter detail).
        logger.warning("[webhook] direct-deliver target rejected route=%s target=%s error=%s", route_name,
                       delivery["deliver"], result.error)
        return web.json_response(failed, status=502)

    def _resolve_route(self, request: "web.Request") -> "tuple[str, Optional[dict], Any, Optional[web.Response]]":
        """Route + profile lookup for a POST; ``(route_name, route_config, profile, error_response)``."""
        self._reload_dynamic_routes()  # hot-reload dynamic subscriptions (mtime-gated, cheap)
        route_name = request.match_info.get("route_name", "")
        route_config = self._routes.get(route_name)
        profile = self._resolve_request_profile(request)
        if profile is _PROFILE_REJECTED:
            return route_name, None, profile, _json_error("Unknown or unconfigured profile", 404)
        if not route_config:
            return route_name, None, profile, _json_error(f"Unknown route: {route_name}", 404)
        if not self._route_allows_profile(route_config, profile):
            logger.warning("[webhook] Route %s is not authorized for profile %r", route_name, profile or "default")
            # Same as unknown-route so profile mismatches can't enumerate route bindings.
            return route_name, None, profile, _json_error(f"Unknown route: {route_name}", 404)
        # Disabled routes stay in the subscriptions file (dashboard can re-enable) but reject events.
        # Only an explicit ``enabled: false`` turns a route off.
        if route_config.get("enabled", True) is False:
            return route_name, None, profile, _json_error(f"Route disabled: {route_name}", 403)
        return route_name, route_config, profile, None

    @staticmethod
    def _apply_skills(prompt: str, skills: list) -> str:
        """Inject the first matching skill via build_skill_invocation_message() directly — /skill-name slash
        commands would be intercepted by the command parser."""
        try:
            from agent.skill_commands import build_skill_invocation_message, get_skill_commands
            skill_cmds = get_skill_commands()
            for skill_name in skills:
                cmd_key = f"/{skill_name}"
                if cmd_key in skill_cmds:
                    skill_content = build_skill_invocation_message(cmd_key, user_instruction=prompt)
                    if skill_content:
                        return skill_content
                else:
                    logger.warning("[webhook] Skill '%s' not found", skill_name)
        except Exception as e:
            logger.warning("[webhook] Skill loading failed: %s", e)
        return prompt

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """POST /webhooks/{route_name} — receive and process a webhook event."""
        route_name, route_config, profile, error_response = self._resolve_route(request)
        if error_response is None:
            raw_body, error_response = await self._read_authenticated_body(request, route_name, route_config)
        if error_response is not None:
            return error_response
        # Rate limiting (after auth)
        if not self._record_rate_limit_hit(route_name, time.time()):
            return _json_error("Rate limit exceeded", 429)
        payload = self._parse_body(raw_body)
        if payload is _UNPARSEABLE:
            return _json_error("Cannot parse body", 400)
        headers = request.headers
        event_type = (headers.get("X-GitHub-Event", "") or headers.get("X-GitLab-Event", "")
                      or payload.get("event_type", "") or payload.get("type", "") or "unknown")
        allowed_events = route_config.get("events", [])
        if allowed_events and event_type not in allowed_events:
            logger.debug("[webhook] Ignoring event %s for route %s (allowed: %s)", event_type, route_name,
                         allowed_events)
            return web.json_response({"status": "ignored", "event": event_type})
        if not self._route_processor.route_filters_match(route_config, payload, event_type, request.headers):
            logger.info("[webhook] filtered event=%s route=%s", event_type, route_name)
            return web.json_response({"status": "ignored", "reason": "filter", "route": route_name})
        # Script, prompt render and skill lookup read the profile's home (skills/, config); the runner
        # only enters the routed profile's scope later around handle_message, so enter it here.
        # See #67277.
        with self._profile_scope(profile):
            script = route_config.get("script")
            if script:
                # Shells out (up to its timeout) — worker thread so the loop isn't blocked; to_thread
                # copies contextvars so the profile scope follows.
                keep, transformed_payload = await asyncio.to_thread(
                    self._route_processor.run_route_script, script, payload)
                if not keep:
                    logger.info("[webhook] script ignored event=%s route=%s", event_type, route_name)
                    return web.json_response({"status": "ignored", "reason": "script", "route": route_name})
                payload = transformed_payload or payload
            prompt = self._render_prompt(route_config.get("prompt", ""), payload, event_type, route_name)
            if skills := route_config.get("skills", []):
                prompt = self._apply_skills(prompt, skills)
        delivery_id = headers.get("X-GitHub-Delivery", headers.get("svix-id", headers.get(
            "webhook-id", headers.get("X-Request-ID", str(int(time.time() * 1000))))))
        now = time.time()  # idempotency: skip duplicate deliveries (webhook retries)
        if not self._record_delivery_id(delivery_id, now):
            logger.info("[webhook] Skipping duplicate delivery %s", delivery_id)
            return web.json_response({"status": "duplicate", "delivery_id": delivery_id}, status=200)
        # Subject suppression runs after the retry check so a provider retry can neither
        # consume nor refresh a subject's window.  One subject speaks once per window
        # however many distinct events it generates.
        allow, suppression = self._check_subject_suppression(route_name, route_config, payload, now, delivery_id)
        if not allow:
            logger.info(
                "[webhook] suppressed route=%s subject=%s window=%ds "
                "quiet_for_another=%.0fs reason=%s delivery=%s",
                route_name, suppression.get("subject"), suppression.get("window"),
                suppression.get("remaining", 0.0),
                "turn_in_flight" if suppression.get("in_flight") else "window", delivery_id)
            return web.json_response(
                {"status": "suppressed", "route": route_name, "subject": suppression.get("subject"),
                 "retry_after": int(suppression.get("remaining", 0.0)), "delivery_id": delivery_id},
                status=200)
        if suppression.get("escalated"):
            logger.info(
                "[webhook] escalation broke suppression route=%s subject=%s "
                "escalation=%s window=%ds delivery=%s",
                route_name, suppression.get("subject"), suppression.get("breakthrough"),
                suppression.get("window"), delivery_id)
        if route_config.get("deliver_only"):
            return await self._handle_deliver_only(prompt, payload, route_config, route_name, event_type, delivery_id,
                                                   profile, suppression.get("pending"))
        return self._dispatch_agent_run(request, route_config, route_name, profile, payload, prompt, event_type,
                                        delivery_id, now, suppression.get("pending"))

    def _dispatch_agent_run(self, request, route_config: dict, route_name: str, profile, payload: Any, prompt: str,
                            event_type: str, delivery_id: str, now: float,
                            suppression_pending: Optional[dict] = None) -> "web.Response":
        """Record delivery info, spawn the agent run, and return 202 immediately."""
        # delivery_id in the session key → concurrent webhooks on one route get independent runs.
        session_chat_id = f"webhook:{route_name}:{delivery_id}"
        # ``profile`` rides along so the reply leg (``send`` → ``_deliver_cross_platform``) egresses through
        # THIS profile's adapter, home channel and secrets — not the first profile that has the platform.
        # ``card``/``payload`` are carried so send() can build the route's card from the same facts the
        # prompt was rendered from (the deliver_only branch above already keeps the payload for the same reason).
        self._delivery_info[session_chat_id] = {
            "deliver": route_config.get("deliver", "log"), "profile": profile,
            "deliver_extra": self._render_delivery_extra(route_config.get("deliver_extra", {}), payload),
            "card": route_config.get("card"), "payload": payload,
            # Consumed by the first send() that actually reaches the target.
            "suppression": suppression_pending}
        self._delivery_info_created[session_chat_id] = now
        self._delivery_info_order.append((now, session_chat_id))
        self._prune_delivery_info(now)
        source = self.build_source(chat_id=session_chat_id, chat_name=f"webhook/{route_name}", chat_type="webhook",
                                   user_id=f"webhook:{route_name}", user_name=route_name)
        if profile and isinstance(profile, str):
            source.profile = profile
        event = MessageEvent(text=prompt, message_type=MessageType.TEXT, source=source, raw_message=payload,
                             message_id=delivery_id)
        logger.info("[webhook] %s event=%s route=%s prompt_len=%d delivery=%s", request.method, event_type, route_name,
                    len(prompt), delivery_id)
        # The per-delivery session is closed by ``on_processing_complete`` once the run finishes
        # (``handle_message`` is fire-and-forget, so nothing can be closed here).
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return web.json_response({"status": "accepted", "route": route_name, "event": event_type,
                                  "delivery_id": delivery_id}, status=202)

    async def on_processing_complete(self, event: "MessageEvent", outcome: Any) -> None:
        """Close the one-shot per-delivery session: ``prune_sessions`` only reaps rows with ``ended_at`` set, so
        unclosed webhook sessions leak unbounded. Fires at the true end of the run; ``end_session()`` is
        first-reason-wins.

        Also the release point for the subject's in-flight reservation:
        ``_commit_subject_delivery`` pops the pending entry the moment a
        message lands, so anything still sitting here when the run ends means
        the turn said nothing (or the target rejected it) — and the subject
        gets its voice back immediately rather than waiting out the hold.
        Success, failure and cancellation all reach here, so no path leaks a
        reservation for longer than the run itself."""
        self._release_subject_reservation(self._delivery_info.get(event.source.chat_id))
        await self._end_webhook_session(event, event.source.chat_id)

    async def _end_webhook_session(self, event: "MessageEvent", session_chat_id: str) -> None:
        """Mark the per-delivery session ended via ``SessionDB.end_session`` (never a hand-written UPDATE),
        resolving session_id from the SAME source the run was keyed on."""
        runner = self.gateway_runner
        session_db, store = getattr(runner, "_session_db", None), getattr(runner, "session_store", None)
        key_fn = getattr(runner, "_session_key_for_source", None)
        if runner is None or session_db is None or store is None or key_fn is None:
            return
        try:
            session_key = key_fn(event.source)
            session_id = _peek_session_id(store, session_key)
            if not session_id:
                logger.debug("[webhook] No session_id to close for %s (key=%s)", session_chat_id, session_key)
                return
            # AsyncSessionDB forwards end_session via to_thread; plain SessionDB is sync.
            result = session_db.end_session(session_id, "webhook_complete")
            if asyncio.iscoroutine(result):
                await result
            logger.debug("[webhook] Closed session %s for delivery %s", session_id, session_chat_id)
        except Exception as e:
            logger.debug("[webhook] Failed to close session for %s: %s", session_chat_id, e)

    # --- Signature validation ---

    def _validate_signature(self, request: "web.Request", body: bytes, secret: str) -> bool:
        """Validate webhook signature (GitHub, GitLab, Svix, Standard Webhooks, Linear, generic HMAC-SHA256)."""
        headers = request.headers

        def _header(name: str) -> str:
            return headers.get(name, "") or headers.get(name.lower(), "") or headers.get(name.upper(), "")

        # Svix / AgentMail: signed content is "{id}.{timestamp}.{raw_body}". Standard Webhooks
        # (webhook-*; GitLab signing tokens) is the same scheme under other header names, but GitLab
        # sends webhook-id/webhook-timestamp on EVERY delivery and webhook-signature only when a signing
        # token is configured, so only the signature header commits to this path — a legacy
        # X-Gitlab-Token install must keep validating below (#47451, #101837).
        svix = [_header(name) for name in ("svix-id", "svix-timestamp", "svix-signature")]
        if not any(svix) and _header("webhook-signature"):
            svix = [_header(name) for name in ("webhook-id", "webhook-timestamp", "webhook-signature")]
        if any(svix):
            return _validate_svix_signature(body, secret, *svix)
        # Linear (any header case): hex HMAC of the body. GitHub: sha256=<hex>. GitLab: plain token.
        for provided, expected in (
                (_header("linear-signature"), lambda: _hex_hmac(secret, body)),
                (headers.get("X-Hub-Signature-256", ""), lambda: "sha256=" + _hex_hmac(secret, body)),
                (headers.get("X-Gitlab-Token", ""), lambda: secret)):
            if provided:
                return _hmac_str_equal(provided, expected())
        route_name = request.match_info.get("route_name", "")
        # Generic V2: X-Webhook-Signature-V2 = hex HMAC-SHA256 of "<timestamp>.<body>", X-Webhook-Timestamp
        # required. Presence of the V2 header COMMITS to V2 — it must not fall through to V1 on a
        # missing/bad timestamp, or an attacker could strip the timestamp from a captured mixed V1+V2
        # request and replay it against the still-present body-only V1 signature.
        v2_sig = headers.get("X-Webhook-Signature-V2", "")
        if v2_sig:
            v2_timestamp = headers.get("X-Webhook-Timestamp", "")
            if not v2_timestamp:
                logger.warning("[webhook] Route '%s' sent X-Webhook-Signature-V2 with no X-Webhook-Timestamp — "
                               "rejecting rather than falling back to legacy V1", route_name)
                return False
            if not _timestamp_fresh(
                    v2_timestamp, "[webhook] Route '%s' generic HMAC V2 timestamp outside replay window", route_name):
                return False
            return _hmac_str_equal(v2_sig, _hex_hmac(secret, v2_timestamp.encode() + b"." + body))
        # Generic V1 (legacy, deprecated): body-only HMAC → replays indefinitely.
        generic_sig = headers.get("X-Webhook-Signature", "")
        if generic_sig:
            if route_name not in self._v1_signature_warned:
                self._v1_signature_warned.add(route_name)
                logger.warning("[webhook] Route '%s' uses legacy body-only HMAC (no timestamp), which is vulnerable "
                               "to replay attacks. Add an 'X-Webhook-Timestamp' header and switch to "
                               "'X-Webhook-Signature-V2' (HMAC-SHA256 of '<timestamp>.<body>').", route_name)
            return _hmac_str_equal(generic_sig, _hex_hmac(secret, body))
        logger.debug("[webhook] Secret configured but no signature header found")
        return False

    # --- Prompt rendering ---

    def _render_prompt(self, template: str, payload: dict, event_type: str, route_name: str) -> str:
        """Render a prompt template with dot-notation payload access (``{pull_request.title}``);
        ``{__raw__}`` dumps the whole payload as indented JSON (truncated to 4000 chars)."""
        if not template:
            truncated = json.dumps(payload, indent=2)[:4000]
            return f"Webhook event '{event_type}' on route '{route_name}':\n\n```json\n{truncated}\n```"

        def _resolve(match: re.Match) -> str:
            key = match.group(1)
            if key == "__raw__":
                return json.dumps(payload, indent=2)[:4000]
            if key == "event_type":
                return event_type
            value: Any = payload
            for part in key.split("."):
                if not isinstance(value, dict):
                    return f"{{{key}}}"
                value = value.get(part, f"{{{key}}}")
            return json.dumps(value, indent=2)[:2000] if isinstance(value, (dict, list)) else str(value)

        return _TEMPLATE_KEY_RE.sub(_resolve, template)

    def _render_delivery_extra(self, extra: dict, payload: dict) -> dict:
        """Render delivery_extra template values with payload data."""
        return {key: self._render_prompt(value, payload, "", "") if isinstance(value, str) else value
                for key, value in extra.items()}

    # --- Response delivery ---

    async def _direct_deliver(self, content: str, delivery: dict) -> SendResult:
        """deliver_only: dispatch *content* to the same delivery helpers agent-mode ``send()`` uses."""
        deliver_type = delivery.get("deliver", "log")
        if deliver_type == "log":  # startup validation rejects deliver_only + log; guard defensively
            logger.info("[webhook] direct-deliver log-only: %s", content[:200])
            return SendResult(success=True)
        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)
        return await self._deliver_cross_platform(deliver_type, content, delivery)

    async def _deliver_github_comment(self, content: str, delivery: dict) -> SendResult:
        """Post agent response as a GitHub PR/issue comment via ``gh`` CLI."""
        extra = delivery.get("deliver_extra", {})
        repo, pr_number = extra.get("repo", ""), extra.get("pr_number", "")
        if not repo or not pr_number:
            logger.error("[webhook] github_comment delivery missing repo or pr_number")
            return SendResult(success=False, error="Missing repo or pr_number")
        try:  # input validation (prevent CLI argument injection)
            pr_int = int(pr_number)
            if pr_int <= 0:
                raise ValueError("non-positive")
        except (ValueError, TypeError):
            logger.error("[webhook] invalid pr_number: %r", pr_number)
            return SendResult(success=False, error="Invalid pr_number")
        if not _REPO_RE.fullmatch(repo):
            logger.error("[webhook] invalid repo format: %r", repo)
            return SendResult(success=False, error="Invalid repo format")
        try:
            # Off-loop: `gh` does network I/O up to its 30s timeout; inline it froze every adapter and
            # timer on the gateway event loop.
            # Running it inline froze every adapter and timer on the gateway event loop for the duration
            # (Pattern A, #91912 class). asyncio.to_thread keeps the loop serving while the subprocess runs;
            # the worker thread is bounded by the subprocess timeout below.
            result = await asyncio.to_thread(
                subprocess.run, ["gh", "pr", "comment", str(pr_int), "--repo", repo, "--body", content],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
                env=self._github_env(delivery.get("profile")))
            if result.returncode == 0:
                logger.info("[webhook] Posted comment on %s#%s", repo, pr_number)
                return SendResult(success=True)
            logger.error("[webhook] gh pr comment failed: %s", result.stderr)
            return SendResult(success=False, error=result.stderr)
        except FileNotFoundError:
            logger.error("[webhook] 'gh' CLI not found — install GitHub CLI for github_comment delivery")
            return SendResult(success=False, error="gh CLI not installed")
        except Exception as e:
            logger.error("[webhook] github_comment delivery error: %s", e)
            return SendResult(success=False, error=str(e))

    def _github_env(self, profile: Optional[str]) -> Optional[dict]:
        """``gh`` environment for a delivery: a routed profile authenticates with ITS ``GH_TOKEN`` /
        ``GITHUB_TOKEN`` from the profile secret scope; under multiplex ``os.environ`` carries the default
        profile's, so those keys are dropped when the profile has none (fail closed, ``gh`` then falls to
        its own stored login). ``None`` (inherit) for bare/default-bound routes."""
        if not profile or not isinstance(profile, str) or profile == "default":
            return None
        from agent.secret_scope import get_secret
        env = {k: v for k, v in os.environ.items() if k not in _GH_TOKEN_VARS}
        with self._profile_scope(profile):
            for name in _GH_TOKEN_VARS:
                if value := get_secret(name):
                    env[name] = value
        return env

    def _find_adapter(self, target_platform: Platform, profile: Optional[str]):
        """The routed profile's own adapter, fail-closed. A ``/p/<profile>/`` route must never post as
        another profile's bot, and a bare (default-bound) route must not borrow a platform parked only on
        a secondary profile — both directions leaked before #65939."""
        return self.gateway_runner._authorization_adapter(target_platform, profile)

    async def _deliver_cross_platform(self, platform_name: str, content: str, delivery: dict) -> SendResult:
        """Route response to another platform (telegram, discord, etc.)."""
        if not self.gateway_runner:
            return SendResult(success=False, error="No gateway runner for cross-platform delivery")
        try:
            target_platform = Platform(platform_name)
        except ValueError:
            return SendResult(success=False, error=f"Unknown platform: {platform_name}")
        profile = delivery.get("profile")
        if not (adapter := self._find_adapter(target_platform, profile)):
            return SendResult(success=False, error=f"Platform {platform_name} not connected")
        extra = delivery.get("deliver_extra", {})
        chat_id = extra.get("chat_id", "")
        # Whole leg under the routed profile's scope: the home channel comes from THAT profile's config
        # (``self.gateway_runner.config`` is the default profile's), and the adapter's send reads its
        # credentials through the profile secret scope.
        with self._profile_scope(profile):
            if not chat_id:
                home = self._delivery_config(profile).get_home_channel(target_platform)
                if not home:
                    return SendResult(success=False, error=f"No chat_id or home channel for {platform_name}")
                chat_id = home.chat_id
            # Every webhook delivery is an unsolicited push: an HTTP POST from an
            # external service, acknowledged with 202 before the run even starts,
            # landing in a chat where nobody asked a question and nobody is
            # waiting on an answer.  Marking it lets the Teams adapter apply
            # the cap it already applies to cron output — without it, a
            # 6000-char ConnectWise summary still arrives as two or three
            # posts.  Adapters that do not read the key are unaffected.
            metadata: Dict[str, Any] = {AUTONOMOUS_DELIVERY_METADATA_KEY: True}
            thread_id = extra.get("message_thread_id") or extra.get("thread_id")  # Telegram forum topics
            if thread_id:
                metadata["thread_id"] = thread_id
            return await adapter.send(chat_id, content, metadata=metadata)

    def _delivery_config(self, profile: Optional[str]):
        """Gateway config of the profile a delivery is bound to (call inside ``_profile_scope``)."""
        if not profile or not isinstance(profile, str):
            return self.gateway_runner.config
        from gateway.config import load_gateway_config
        return load_gateway_config()
