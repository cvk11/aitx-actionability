"""Baseline arm, A/B/C config slots, budget reserve, and the
unavailable-vs-failed-vs-negative distinction in rendered reports."""
from __future__ import annotations

import httpx
import pytest
from fake_http import handler
from mock_handlers import HANDLERS, HANDLERS_BASELINE

from aitx.http import HttpClient
from aitx.ledger import EvidenceLedger
from aitx.llm.base import BudgetExceeded, CallRecord, CostTracker
from aitx.llm.mock import MockLLM
from aitx.pipeline import Pipeline
from aitx.qc import check_report_structure


def test_baseline_arm(settings, fake_http, test_case):
    res = Pipeline(MockLLM(HANDLERS_BASELINE), settings, http=fake_http, mode="baseline").run(test_case)
    assert res.audit["mode"] == "baseline" and check_report_structure(res.output.report_markdown, "TEST-0001").ok
    final = {i["item_id"]: i for i in res.audit["final_items"]}
    # baseline applies no section rules: the closed trial and the mis-tiered item survive (this is what arm B should fix)
    assert "trial-closed" in final and final["tgt-q"]["section"] == "existing_targeted_therapies"
    assert "A_interpretation" not in res.audit["stages"]


def test_config_c_is_blocked(monkeypatch):
    from aitx.factory import build_pipeline

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(NotImplementedError):
        build_pipeline("C_full_openai")


def test_budget_reserve():
    c = CostTracker(budget_usd=5.0, reserve_usd=1.0)
    c.record(CallRecord("s", "m", "m", cost_usd=3.9))
    c.check("ok")
    c.record(CallRecord("s", "m", "m", cost_usd=0.2))
    with pytest.raises(BudgetExceeded):
        c.check("next")


def test_failed_retrieval_is_not_reported_as_absence(settings, test_case):
    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.host == "clinicaltrials.gov":
            raise httpx.ConnectTimeout("simulated outage")
        return handler(request)

    http = HttpClient(settings, transport=httpx.MockTransport(flaky))
    res = Pipeline(MockLLM(HANDLERS), settings, http=http, parallel=False).run(test_case)
    md = res.output.report_markdown
    trials = md.split("### Clinical Trial Eligibility", 1)[1].split("###", 1)[0]
    assert "Retrieval from ClinicalTrials.gov failed during this run" in trials
    assert res.audit["retrieval_status"]["search_trials"]["failed"] >= 1


def test_retrieval_status_categories():
    L = EvidenceLedger()
    L.log_query("clinvar_lookup", {}, [])
    L.log_query("clinvar_lookup", {}, ["CLINVAR:VCV1"])
    L.log_query("clinvar_lookup", {}, [], error="timeout")
    assert L.retrieval_status()["clinvar_lookup"] == {"with_results": 1, "empty": 1, "failed": 1, "truncated": 0, "partial": 0}


def test_claude_opus_is_the_only_model(monkeypatch):
    """Project decision 2026-11-02: no configuration builds a Gemini (or other non-Claude) client."""
    import sys

    from aitx import factory

    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-placeholder")  # client construction only; no request is sent
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert all(o["reviewer"] in (None, "openai") for o in factory.CONFIGS.values())
    for name, o in factory.CONFIGS.items():
        if o["reviewer"] == "openai":
            continue  # arm C placeholder raises NotImplementedError
        p = factory.build_pipeline(name)
        assert p.reviewer_llm is None and p.llm.model.startswith("claude-opus") and p.verifier_llm.model.startswith("claude-opus")
    p = factory.build_pipeline("default")
    assert p.reviewer_llm is None
    assert "aitx.llm.gemini_client" not in sys.modules
    assert "gemini" not in factory.__loader__.get_source("aitx.factory").split("# Project decision")[0].lower()
