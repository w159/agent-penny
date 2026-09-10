"""Stage B model resolution for cron/trend_cluster_semantic.py.

Covers default_model_call()'s runtime switch: ANTHROPIC_API_KEY present ->
Anthropic Messages API (model pinned to claude-sonnet-5, or a config
override); absent -> the existing AIAgent/glm-5.2:cloud fallback, logged
once. No network is touched - anthropic.Anthropic is monkeypatched.
"""
from __future__ import annotations

import anthropic
import pytest

from cron import trend_cluster_semantic as tcs


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str | None, stop_reason: str = "end_turn") -> None:
        self.content = [] if text is None else [_FakeTextBlock(text)]
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, response=None, error=None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeAnthropicClient:
    """Records the timeout it was constructed with and exposes .messages."""

    instances: list["_FakeAnthropicClient"] = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.messages = _FakeMessages(response=_FakeResponse("a title"))
        _FakeAnthropicClient.instances.append(self)


@pytest.fixture(autouse=True)
def _reset_fallback_log_flag():
    tcs._logged_fallback_notice = False
    yield
    tcs._logged_fallback_notice = False


@pytest.fixture(autouse=True)
def _clear_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_key_absent_selects_fallback_and_never_constructs_anthropic_client(monkeypatch):
    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **kw: (_ for _ in ()).throw(AssertionError("Anthropic() constructed"))
    )
    called = {}

    def fake_fallback(system, user, *, model=None):
        called["system"] = system
        called["user"] = user
        called["model"] = model
        return "fallback text"

    monkeypatch.setattr(tcs, "_fallback_model_call", fake_fallback)

    result = tcs.default_model_call("sys prompt", "user prompt")

    assert result == "fallback text"
    assert called["system"] == "sys prompt"
    assert called["user"] == "user prompt"


def test_key_present_uses_anthropic_with_default_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    fake = _FakeAnthropicClient()
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    monkeypatch.setattr(tcs, "_stage_b_model_ids", lambda: ("claude-sonnet-5", "glm-5.2:cloud"))

    result = tcs.default_model_call("sys prompt", "user prompt")

    assert result == "a title"
    assert len(fake.messages.calls) == 1
    call = fake.messages.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["system"] == "sys prompt"
    assert call["messages"] == [{"role": "user", "content": "user prompt"}]


def test_anthropic_api_error_raises_loudly_not_empty_string(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    fake = _FakeAnthropicClient()
    fake.messages = _FakeMessages(error=RuntimeError("boom from API"))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)

    with pytest.raises(Exception) as excinfo:
        tcs.default_model_call("sys prompt", "user prompt")

    assert "boom from API" in str(excinfo.value)


def test_returned_text_extracted_from_sdk_response_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    fake = _FakeAnthropicClient()
    fake.messages = _FakeMessages(response=_FakeResponse("narrated title text"))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)

    result = tcs.default_model_call("sys prompt", "user prompt")

    assert result == "narrated title text"


def test_no_text_block_raises_loudly(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    fake = _FakeAnthropicClient()
    fake.messages = _FakeMessages(response=_FakeResponse(None, stop_reason="max_tokens"))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)

    with pytest.raises(Exception) as excinfo:
        tcs.default_model_call("sys prompt", "user prompt")

    assert "no text" in str(excinfo.value).lower()


def test_config_override_of_anthropic_model_id(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    fake = _FakeAnthropicClient()
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"cron": {"trend": {"stage_b_model": "claude-opus-5"}}}
    )

    tcs.default_model_call("sys prompt", "user prompt")

    assert fake.messages.calls[0]["model"] == "claude-opus-5"


def test_config_override_of_fallback_model_id(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"cron": {"trend": {"stage_b_fallback_model": "other-model:cloud"}}},
    )

    captured = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_conversation(self, user):
            return "fallback response"

    monkeypatch.setitem(
        __import__("sys").modules,
        "run_agent",
        type("run_agent", (), {"AIAgent": _FakeAgent}),
    )

    result = tcs.default_model_call("sys prompt", "user prompt")

    assert result == "fallback response"
    assert captured["model"] == "other-model:cloud"


def test_key_absent_logs_once(monkeypatch, caplog):
    monkeypatch.setattr(tcs, "_fallback_model_call", lambda system, user, **kw: "x")

    with caplog.at_level("INFO", logger="cron.trend_cluster_semantic"):
        tcs.default_model_call("s1", "u1")
        tcs.default_model_call("s2", "u2")

    absent_msgs = [r for r in caplog.records if "ANTHROPIC_API_KEY" in r.getMessage()]
    assert len(absent_msgs) == 1
