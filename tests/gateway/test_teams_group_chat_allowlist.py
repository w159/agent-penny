"""Regression tests for TEAMS_GROUP_ALLOWED_CHATS.

Before this fix, Teams was absent from the chat-scoped allowlist map in
``gateway/authz_mixin.py::_is_user_authorized``, so every member of the IT
group chat other than the one id in TEAMS_ALLOWED_USERS was silently
default-denied (observed in production: Jarvis Williams and Ernesto Velarde,
gateway.run "Unauthorized user" warnings from June through August 2026).

The fix authorizes an entire admitted Teams group/channel chat by chat ID,
mirroring the existing TELEGRAM_GROUP_ALLOWED_CHATS / QQ_GROUP_ALLOWED_USERS
pattern. It deliberately does NOT touch TEAMS_ALLOW_ALL_USERS or
GATEWAY_ALLOW_ALL_USERS -- DMs from arbitrary tenant users must keep
default-denying, since Penny's toolset includes CIPP M365 admin writes,
ConnectWise, NinjaOne, and ThreatLocker actions.
"""

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource

IT_GROUP_CHAT_ID = "19:d72b9e0d737b4dda960814e674c260b7@thread.v2"
OTHER_GROUP_CHAT_ID = "19:zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz@thread.v2"

# The actual regression: Jarvis was in the IT group chat but not in
# TEAMS_ALLOWED_USERS, and got silently ignored.
JARVIS_AAD_ID = "6ff84f43-2fac-4366-926d-382cb712deae"

# The one id that was already present in TEAMS_ALLOWED_USERS before the fix.
ALLOWLISTED_AAD_ID = "010a8823-1689-473a-8cde-3a26c406beae"


def _runner():
    return object.__new__(GatewayRunner)


def _teams_source(*, chat_id, chat_type, user_id):
    return SessionSource(
        platform=Platform("teams"),
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name="Test User",
    )


def test_teams_group_member_not_in_allowed_users_is_authorized_via_group_chat(monkeypatch):
    """The core regression: a non-allowlisted user IS authorized in the
    admitted IT group chat."""
    monkeypatch.setenv("TEAMS_GROUP_ALLOWED_CHATS", IT_GROUP_CHAT_ID)
    monkeypatch.delenv("TEAMS_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    source = _teams_source(chat_id=IT_GROUP_CHAT_ID, chat_type="group", user_id=JARVIS_AAD_ID)

    assert _runner()._is_user_authorized(source) is True


def test_teams_group_member_in_different_unlisted_chat_is_not_authorized(monkeypatch):
    """The same user in a chat NOT on the allowlist stays default-denied."""
    monkeypatch.setenv("TEAMS_GROUP_ALLOWED_CHATS", IT_GROUP_CHAT_ID)
    monkeypatch.delenv("TEAMS_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    source = _teams_source(chat_id=OTHER_GROUP_CHAT_ID, chat_type="group", user_id=JARVIS_AAD_ID)

    assert _runner()._is_user_authorized(source) is False


def test_teams_dm_from_group_authorized_user_is_not_authorized(monkeypatch):
    """Owner's decision guard: the chat-scoped allowlist must never reach a
    DM. Widening this later must fail loudly."""
    monkeypatch.setenv("TEAMS_GROUP_ALLOWED_CHATS", IT_GROUP_CHAT_ID)
    monkeypatch.delenv("TEAMS_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    # A DM's chat_id is conventionally the user's own id.
    source = _teams_source(chat_id=JARVIS_AAD_ID, chat_type="dm", user_id=JARVIS_AAD_ID)

    assert _runner()._is_user_authorized(source) is False


def test_teams_allowed_users_member_still_authorized_in_group(monkeypatch):
    """No regression: the id already in TEAMS_ALLOWED_USERS keeps working.

    TEAMS_ALLOWED_USERS is resolved via the plugin registry (Teams isn't a
    built-in Platform enum member), so this unit test registers a stub entry
    matching what ``plugins/platforms/teams/adapter.py::register`` passes in
    production (allowed_users_env="TEAMS_ALLOWED_USERS").
    """
    from gateway.platform_registry import PlatformEntry, platform_registry

    stub_entry = PlatformEntry(
        name="teams",
        label="Teams",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
        allowed_users_env="TEAMS_ALLOWED_USERS",
        allow_all_env="TEAMS_ALLOW_ALL_USERS",
    )
    monkeypatch.setattr(platform_registry, "get", lambda name: stub_entry if name == "teams" else None)

    monkeypatch.setenv("TEAMS_GROUP_ALLOWED_CHATS", IT_GROUP_CHAT_ID)
    monkeypatch.setenv("TEAMS_ALLOWED_USERS", ALLOWLISTED_AAD_ID)
    monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    source = _teams_source(chat_id=OTHER_GROUP_CHAT_ID, chat_type="group", user_id=ALLOWLISTED_AAD_ID)

    assert _runner()._is_user_authorized(source) is True


def test_teams_group_allowed_chats_unset_preserves_today_behavior(monkeypatch):
    """With the new var unset, a non-allowlisted user in a group chat is
    still default-denied -- unchanged from before the fix."""
    monkeypatch.delenv("TEAMS_GROUP_ALLOWED_CHATS", raising=False)
    monkeypatch.delenv("TEAMS_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    source = _teams_source(chat_id=IT_GROUP_CHAT_ID, chat_type="group", user_id=JARVIS_AAD_ID)

    assert _runner()._is_user_authorized(source) is False


def test_telegram_group_chat_allowlist_unaffected_by_teams_addition(monkeypatch):
    """Telegram's existing chat-scoped allowlist keeps working unchanged."""
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-100")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100",
        chat_type="group",
        user_id=None,
        user_name=None,
    )

    assert _runner()._is_user_authorized(source) is True


def test_qq_group_chat_allowlist_unaffected_by_teams_addition(monkeypatch):
    """QQ's existing chat-scoped allowlist keeps working unchanged."""
    monkeypatch.setenv("QQ_GROUP_ALLOWED_USERS", "qq-group-1")
    monkeypatch.delenv("QQ_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    source = SessionSource(
        platform=Platform.QQBOT,
        chat_id="qq-group-1",
        chat_type="group",
        user_id="some-qq-user",
        user_name="Some User",
    )

    assert _runner()._is_user_authorized(source) is True
