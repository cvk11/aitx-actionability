"""ClaudeLLM tool loop against a fake SDK client (no network, no cost)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from aitx.ledger import EvidenceLedger
from aitx.llm.anthropic_client import FALLBACK_BETA, ClaudeLLM
from aitx.llm.base import BudgetExceeded, CostTracker, ModelRefusal
from aitx.retrieval.registry import ToolBox
from aitx.schemas import JudgeReport

FINAL = json.dumps({"scores": [{"dimension": "factual_accuracy", "score_1_to_5": 3, "main_problems": []}], "specific_fixable_issues": []})


def msg(content, stop, model="claude-opus-5-5"):
    return NS(content=content, stop_reason=stop, model=model, usage=NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                                                                         cache_read_input_tokens=500), _request_id="req_x", stop_details=None)


class FakeStream:
    def __init__(self, m):
        self.m = m

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.m


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = NS(messages=NS(stream=self._stream))
        self.messages = NS(stream=self._stream)

    def _stream(self, **kw):
        self.calls.append(kw)
        return FakeStream(self.script.pop(0))


def test_agent_loop_executes_tools_and_parses(settings, fake_http):
    script = [
        msg([NS(type="thinking", thinking=""), NS(type="tool_use", id="t1", name="pubmed_search", input={"query": "GENEX", "max_results": 5, "page": 1})], "tool_use"),
        msg([NS(type="text", text=FINAL)], "end_turn"),
    ]
    fc = FakeClient(script)
    cost = CostTracker()
    from dataclasses import replace

    llm = ClaudeLLM(replace(settings, primary_model="claude-opus-5-5", refusal_fallback=True), cost, client=fc)
    L = EvidenceLedger()
    out = llm.agent(stage="x", system="sys", user="u", output_model=JudgeReport, toolbox=ToolBox(fake_http, L, settings), tool_names=None, max_rounds=3)
    assert out.scores[0].score_1_to_5 == 3
    first = fc.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["thinking"] == {"type": "adaptive"} and "temperature" not in first
    assert first["output_config"]["effort"] == settings.effort and first["output_config"]["format"]["type"] == "json_schema"
    assert first["betas"] == [FALLBACK_BETA] and first["fallbacks"] == "default"
    assert all(t.get("strict") for t in first["tools"]) and "tool_choice" not in first
    tr = fc.calls[1]["messages"][-1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1" and "PMID:11111111" in tr["content"]
    assert "PMID:11111111" in L and len(cost.calls) == 2 and cost.total_usd > 0


def test_tool_budget_forces_final_answer(settings, fake_http):
    use = lambda i: msg([NS(type="tool_use", id=f"t{i}", name="pubmed_search", input={"query": "q", "max_results": 1, "page": 1})], "tool_use")
    fc = FakeClient([use(0), use(1), msg([NS(type="text", text=FINAL)], "end_turn")])
    llm = ClaudeLLM(settings, CostTracker(), client=fc)
    llm.agent(stage="x", system="s", user="u", output_model=JudgeReport, toolbox=ToolBox(fake_http, EvidenceLedger(), settings), tool_names=None, max_rounds=2)
    assert fc.calls[-1]["tool_choice"] == {"type": "none"}
    assert "Tool budget reached" in json.dumps(fc.calls[-1]["messages"][-1]["content"], default=str)


def test_refusal_and_budget(settings):
    fc = FakeClient([msg([], "refusal")])
    with pytest.raises(ModelRefusal):
        ClaudeLLM(settings, CostTracker(), client=fc).structured(stage="s", system="s", user="u", output_model=JudgeReport)
    c = CostTracker(budget_usd=0.0)
    c.calls.append(__import__("aitx.llm.base", fromlist=["CallRecord"]).CallRecord("s", "m", "m", cost_usd=1.0))
    with pytest.raises(BudgetExceeded):
        ClaudeLLM(settings, c, client=FakeClient([])).structured(stage="s", system="s", user="u", output_model=JudgeReport)


def test_fallback_served_model_recorded(settings):
    fc = FakeClient([msg([NS(type="fallback"), NS(type="text", text=FINAL)], "end_turn", model="claude-opus-5")])
    cost = CostTracker()
    ClaudeLLM(settings, cost, client=fc).structured(stage="s", system="s", user="u", output_model=JudgeReport)
    assert cost.calls[0].fallback_used and cost.calls[0].served_model == "claude-opus-5"


def test_fallback_disabled_by_default(settings):
    """First controlled run: no automatic model fallback unless explicitly enabled."""
    assert settings.refusal_fallback is False
    fc = FakeClient([msg([NS(type="text", text=FINAL)], "end_turn")])
    ClaudeLLM(settings, CostTracker(), client=fc).structured(stage="s", system="s", user="u", output_model=JudgeReport)
    assert "fallbacks" not in fc.calls[0] and "betas" not in fc.calls[0]
