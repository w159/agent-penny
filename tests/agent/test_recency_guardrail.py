"""Verify the recency guardrail rule is present in the assembled system prompt.

Owner report: Penny re-narrated month-old ConnectWise tickets as current in
the Teams group chat, because stale conversation history was treated as
present-tense fact. This rule tells the model to re-query before asserting
ticket status, and to keep replies short -- independent of whether any
learned/approved behavior rules exist yet (unlike LEARNED BEHAVIOR RULES,
this section is hand-authored and always renders).
"""

from agent.system_prompt import _RECENCY_GUARDRAIL_SECTION, _build_learned_context_parts


class TestRecencyGuardrailSection:
    def test_guardrail_text_covers_the_required_rules(self):
        text = _RECENCY_GUARDRAIL_SECTION
        assert "stale" in text.lower()
        assert "ConnectWise" in text
        assert "re-query" in text.lower() or "requery" in text.lower()
        assert "short" in text.lower()

    def test_guardrail_is_always_included_even_with_no_agent_context(self):
        parts = _build_learned_context_parts(agent=None)
        assert any(_RECENCY_GUARDRAIL_SECTION in part for part in parts)

    def test_guardrail_is_the_first_learned_context_part(self):
        parts = _build_learned_context_parts(agent=None)
        assert parts[0] == _RECENCY_GUARDRAIL_SECTION
