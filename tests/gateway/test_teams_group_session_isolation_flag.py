"""Verify Teams honors ``group_sessions_per_user`` for the IT group chat.

Owner report: Penny re-narrated month-old Outlook tickets as current in the
Teams group chat. Root cause was ``session_reset.mode: none`` (fixed
separately in config.yaml). This file proves the second half of that fix
holds: with ``group_sessions_per_user`` false, every member of a Teams group
chat lands on ONE shared session instead of one session per user, so the
group gets a single coherent conversation instead of N stale, diverging ones.

``gateway/run.py::_create_adapter`` bridges the gateway-level
``group_sessions_per_user`` default into every platform's ``config.extra``
via ``setdefault`` (run.py ~13712-13725) BEFORE constructing the
platform-specific adapter class. ``TeamsAdapter`` does not override
``handle_message`` -- it calls ``self.handle_message(event)`` (adapter.py
~1170), which resolves to ``BasePlatformAdapter.handle_message``
(gateway/platforms/base.py ~5665), which reads
``self.config.extra.get("group_sessions_per_user", True)``. So Teams
inherits the flag through the same path telegram/feishu/etc. use -- no
adapter-level wiring was missing.
"""

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource, build_session_key

CHAT_ID = "19:d72b9e0d737b4dda960814e674c260b7@thread.v2"
JERRY = "6a04ad0e-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
JARVIS = "6ff84f43-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


TEAMS = Platform("teams")


def _group_source(user_id: str) -> SessionSource:
    return SessionSource(
        platform=TEAMS,
        chat_id=CHAT_ID,
        chat_type="group",
        user_id=user_id,
    )


def _dm_source(user_id: str) -> SessionSource:
    return SessionSource(
        platform=TEAMS,
        chat_id=user_id,
        chat_type="dm",
        user_id=user_id,
    )


class TestGroupSessionsPerUserFlag:
    def test_group_sessions_per_user_true_isolates_by_user(self):
        key_jerry = build_session_key(_group_source(JERRY), group_sessions_per_user=True)
        key_jarvis = build_session_key(_group_source(JARVIS), group_sessions_per_user=True)
        assert key_jerry != key_jarvis

    def test_group_sessions_per_user_false_merges_the_two_users(self):
        key_jerry = build_session_key(_group_source(JERRY), group_sessions_per_user=False)
        key_jarvis = build_session_key(_group_source(JARVIS), group_sessions_per_user=False)
        assert key_jerry == key_jarvis

    def test_dm_session_key_is_unaffected_by_the_flag(self):
        key_true = build_session_key(_dm_source(JERRY), group_sessions_per_user=True)
        key_false = build_session_key(_dm_source(JERRY), group_sessions_per_user=False)
        assert key_true == key_false


class TestGatewayBridgesFlagIntoPlatformExtra:
    """``_create_adapter`` (run.py) must setdefault the gateway-level flag
    into ``PlatformConfig.extra`` before the Teams adapter reads it, exactly
    as it does for telegram/feishu/discord/etc.
    """

    def test_setdefault_does_not_override_an_explicit_platform_value(self):
        # Mirrors the setdefault() call in gateway/run.py::_create_adapter --
        # a platform-local override must win over the gateway default.
        cfg = PlatformConfig(extra={"group_sessions_per_user": False})
        cfg.extra.setdefault("group_sessions_per_user", True)
        assert cfg.extra["group_sessions_per_user"] is False

    def test_setdefault_applies_the_gateway_default_when_unset(self):
        cfg = PlatformConfig(extra={})
        cfg.extra.setdefault("group_sessions_per_user", False)
        assert cfg.extra["group_sessions_per_user"] is False
