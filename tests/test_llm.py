from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.llm import BudgetExceeded, ClaudeLLM, LLMError, MockLLM, start_meter
from app.models import ChatReply, IntentResult


class FakeClient:
    """Mimics anthropic.Anthropic().messages.create with scripted tool_use responses."""

    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        block = NS(type="tool_use", id=f"tu_{len(self.calls)}", name="respond", input=out)
        return NS(content=[block], usage=NS(input_tokens=120, output_tokens=30))


def llm(outputs, **kw):
    return ClaudeLLM("k", "claude-test", client=FakeClient(outputs), **kw)


def test_forced_tool_call_and_pydantic_validation():
    m = llm([{"intent": "dinner", "reason": "x"}])
    out = m.structured("intent", IntentResult, "plan dinner", {"a": 1})
    assert out.intent == "dinner"
    call = m.client.calls[0]
    assert call["tool_choice"] == {"type": "auto"}  # newer models reject a forced tool; the prompt asks for it instead
    assert call["tools"][0]["input_schema"]["properties"]["intent"]
    assert "UNTRUSTED" in call["system"]            # injection guard is in the system prompt
    assert '"a": 1' in call["messages"][0]["content"]


def test_invalid_output_is_retried_with_the_validation_error():
    m = llm([{"intent": "nonsense"}, {"intent": "briefing"}])
    assert m.structured("intent", IntentResult, "x", {}).intent == "briefing"
    retry = m.client.calls[1]["messages"]
    assert retry[-1]["content"][0]["is_error"] is True and "Validation failed" in retry[-1]["content"][0]["content"]


def test_gives_up_after_max_attempts():
    m = llm([{"intent": "bad"}] * 3)
    with pytest.raises(LLMError):
        m.structured("intent", IntentResult, "x", {})
    assert len(m.client.calls) == 3


def test_sdk_failure_becomes_llmerror():
    with pytest.raises(LLMError):
        llm([RuntimeError("boom")]).structured("chat", ChatReply, "x", {})


def test_daily_cap_blocks_further_calls():
    m = llm([{"text": "a"}, {"text": "b"}], daily_cap=1)
    m.structured("chat", ChatReply, "x", {})
    with pytest.raises(BudgetExceeded):
        m.structured("chat", ChatReply, "x", {})


def test_usage_meter_counts_tokens_and_prices_only_when_configured():
    meter = start_meter()
    llm([{"text": "a"}]).structured("chat", ChatReply, "x", {})
    assert (meter.calls, meter.input_tokens, meter.output_tokens) == (1, 120, 30)
    assert meter.cost_usd(None, None) is None
    assert meter.cost_usd(3.0, 15.0) == round(120 / 1e6 * 3.0 + 30 / 1e6 * 15.0, 6)


def test_mock_is_deterministic_and_rule_based():
    m = MockLLM()
    assert m.structured("intent", IntentResult, "Give me my morning briefing", {}).intent == "briefing"
    assert m.structured("intent", IntentResult, "book a table for dinner", {}).intent == "dinner"
    assert m.structured("intent", IntentResult, "what is 2+2", {}).intent == "chat"
