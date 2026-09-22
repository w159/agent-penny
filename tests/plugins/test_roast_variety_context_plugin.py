"""Tests for plugins/roast_variety_context/ - the structural anti-repetition backstop
for roast material in the internal IT Teams chat.

Root-caused against a real production incident (2026-09-21): "roast an end user for
me" got the exact same Jarvis Williams "Waiting Client Response pile is aging like
forgotten yogurt" material Penny had used before. SOUL.md's "Vary it" rule alone
proved insufficient (same lesson as agent/turn_finalizer.py's
_strip_behavior_state_aside); this plugin grounds the model in real recent-message
fact instead of trusting the instruction alone.
"""
from __future__ import annotations

import pytest

import plugins.roast_variety_context as plugin

_IT_CHAT_SESSION_ID = "agent:main:teams:group:19:d72b9e0d737b4dda960814e674c260b7@thread.v2"
_OTHER_TEAMS_SESSION_ID = "agent:main:teams:dm:19:someone-else@thread.v2"


def _assistant_msg(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _user_msg(text: str) -> dict:
    return {"role": "user", "content": text}


class TestChannelGating:
    def test_non_teams_platform_returns_none(self, monkeypatch):
        monkeypatch.setattr(plugin, "_roster_names", lambda: ["Jarvis Williams"])
        history = [_assistant_msg("Jarvis Williams strikes again.")]
        result = plugin._on_pre_llm_call(
            session_id=_IT_CHAT_SESSION_ID, platform="telegram", conversation_history=history,
        )
        assert result is None

    def test_teams_dm_outside_it_chat_returns_none(self, monkeypatch):
        monkeypatch.setattr(plugin, "_roster_names", lambda: ["Jarvis Williams"])
        history = [_assistant_msg("Jarvis Williams strikes again.")]
        result = plugin._on_pre_llm_call(
            session_id=_OTHER_TEAMS_SESSION_ID, platform="teams", conversation_history=history,
        )
        assert result is None

    def test_it_teams_chat_with_no_recent_names_returns_none(self, monkeypatch):
        monkeypatch.setattr(plugin, "_roster_names", lambda: ["Jarvis Williams"])
        history = [_assistant_msg("The board is quiet today.")]
        result = plugin._on_pre_llm_call(
            session_id=_IT_CHAT_SESSION_ID, platform="teams", conversation_history=history,
        )
        assert result is None


class TestRecentlyUsedNames:
    def test_name_in_last_message_flagged_as_last_message(self, monkeypatch):
        monkeypatch.setattr(plugin, "_roster_names", lambda: ["Jarvis Williams", "Nicole McFarland"])
        history = [
            _user_msg("roast an end user for me"),
            _assistant_msg("Jarvis Williams' Waiting Client Response pile is aging again."),
        ]
        result = plugin._on_pre_llm_call(
            session_id=_IT_CHAT_SESSION_ID, platform="teams", conversation_history=history,
        )
        assert result is not None
        context = result["context"]
        assert "Jarvis Williams" in context
        assert "your last message" in context
        assert "Nicole McFarland" not in context

    def test_name_further_back_reports_messages_ago(self, monkeypatch):
        monkeypatch.setattr(plugin, "_roster_names", lambda: ["Jarvis Williams"])
        history = [
            _assistant_msg("Jarvis Williams again, same pile."),
            _user_msg("ok noted"),
            _assistant_msg("Board's quiet since then."),
            _user_msg("roast an end user for me"),
        ]
        result = plugin._on_pre_llm_call(
            session_id=_IT_CHAT_SESSION_ID, platform="teams", conversation_history=history,
        )
        assert result is not None
        assert "2 messages ago" in result["context"]

    def test_multiple_recent_names_ordered_nearest_first(self, monkeypatch):
        monkeypatch.setattr(plugin, "_roster_names", lambda: ["Jarvis Williams", "Ernesto Velarde"])
        history = [
            _assistant_msg("Ernesto Velarde's On-Hold graveyard grows."),
            _assistant_msg("Jarvis Williams strikes again."),
        ]
        result = plugin._on_pre_llm_call(
            session_id=_IT_CHAT_SESSION_ID, platform="teams", conversation_history=history,
        )
        assert result is not None
        context = result["context"]
        jarvis_idx = context.index("Jarvis Williams")
        ernesto_idx = context.index("Ernesto Velarde")
        assert jarvis_idx < ernesto_idx  # more recent mention listed first

    def test_only_scans_assistant_messages_not_user_messages(self, monkeypatch):
        monkeypatch.setattr(plugin, "_roster_names", lambda: ["Jarvis Williams"])
        history = [_user_msg("what's up with Jarvis Williams lately?")]
        result = plugin._on_pre_llm_call(
            session_id=_IT_CHAT_SESSION_ID, platform="teams", conversation_history=history,
        )
        assert result is None

    def test_lookback_window_ignores_names_beyond_it(self, monkeypatch):
        monkeypatch.setattr(plugin, "_roster_names", lambda: ["Jarvis Williams"])
        # 7 assistant messages: the name only appears in the oldest, beyond the
        # 6-message lookback window.
        history = [_assistant_msg("Jarvis Williams, way back when.")]
        history += [_assistant_msg(f"filler message {i}") for i in range(6)]
        result = plugin._on_pre_llm_call(
            session_id=_IT_CHAT_SESSION_ID, platform="teams", conversation_history=history,
        )
        assert result is None


class TestFailsOpen:
    def test_roster_read_failure_returns_none_not_raise(self, monkeypatch):
        def _raise():
            raise RuntimeError("disk unavailable")

        monkeypatch.setattr(plugin, "_roster_names", _raise)
        history = [_assistant_msg("Jarvis Williams strikes again.")]
        result = plugin._on_pre_llm_call(
            session_id=_IT_CHAT_SESSION_ID, platform="teams", conversation_history=history,
        )
        assert result is None

    def test_missing_roster_file_returns_empty_list_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        assert plugin._roster_names() == []


class TestRosterNameParsing:
    def test_parses_level_two_headings_as_names(self, tmp_path, monkeypatch):
        ops_dir = tmp_path / "memories" / "ops"
        ops_dir.mkdir(parents=True)
        (ops_dir / "roster.md").write_text(
            "# Henssler Financial \u2014 Ops Roster\n\n"
            "Compliance boundary text.\n\n"
            "## Jerry Morgan\n- Director of IT.\n\n"
            "## Jarvis Williams\n- Help-desk tech.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        assert plugin._roster_names() == ["Jerry Morgan", "Jarvis Williams"]


class TestPluginRegistration:
    def test_register_hooks_pre_llm_call(self):
        registered = {}

        class _Ctx:
            def register_hook(self, name, fn):
                registered[name] = fn

        plugin.register(_Ctx())
        assert registered == {"pre_llm_call": plugin._on_pre_llm_call}
