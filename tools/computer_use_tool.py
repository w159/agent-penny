"""Discovery shim: tools.registry auto-imports ``tools/*.py``, so this top-level
module registers ``computer_use`` for the ``tools/computer_use/`` package."""

from __future__ import annotations

from tools.computer_use.schema import COMPUTER_USE_SCHEMA
from tools.computer_use.tool import (
    check_computer_use_requirements, handle_computer_use, release_computer_use_session, set_approval_callback,
)
from tools.connector_action_gate import require_capability_approval
from tools.registry import registry


registry.register(
    name="computer_use",
    toolset="computer_use",
    schema=COMPUTER_USE_SCHEMA,
    handler=require_capability_approval("computer_use", "computer_use")(lambda args, **kw: handle_computer_use(args, **kw)),
    check_fn=check_computer_use_requirements,
    requires_env=[],
    description=(
        "Universal desktop control via cua-driver (macOS, Windows, Linux). Works with any "
        "tool-capable model (Anthropic, OpenAI, OpenRouter, local vLLM, "
        "etc.). Background computer-use: does NOT steal the user's cursor "
        "or keyboard focus. Mutating actions (clicks, keystrokes) require "
        "express approval via tools.connector_action_gate; screenshot/"
        "cursor_position/wait auto-execute."
    ),
)


__all__ = ["handle_computer_use", "release_computer_use_session", "set_approval_callback", "check_computer_use_requirements"]
