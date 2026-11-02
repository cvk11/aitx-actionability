"""Offline end-to-end run with the mock LLM (labelled MOCK; never submittable)."""

from __future__ import annotations

from mock_handlers import HANDLERS

from aitx.llm.mock import MockLLM
from aitx.pipeline import MOCK_BANNER, Pipeline
from aitx.qc import check_report_structure, validate_submission


def _run(settings, fake_http, case, reviewer=False):
    llm = MockLLM(HANDLERS)
    p = Pipeline(llm, settings, http=fake_http, reviewer_llm=MockLLM(HANDLERS) if reviewer else None, parallel=False)
    return p.run(case), llm


def test_end_to_end_mock(settings, fake_http, test_case):
    res, llm = _run(settings, fake_http, test_case)
    md = res.output.report_markdown
    final = {i["item_id"]: i for i in res.audit["final_items"]}
    # structure
    assert check_report_structure(md, "TEST-0001").ok
    assert MOCK_BANNER in md and res.is_mock
    # closed trial dropped, open trial kept with live status rendered
    assert "trial-closed" not in final and "trial-open" in final
    assert "NCT00000001: Drug Q in Genetic Epilepsies](https://clinicaltrials.gov/study/NCT00000001)" in md and "recruiting" in md
    # 'existing targeted' without label/guideline support moved to off-label (human case report)
    assert final["tgt-q"]["section"] == "off_label_expanded_access"
    # 'off-label' without human precedent (mouse study) moved to repurposing
    assert final["off-r"]["section"] == "drug_repurposing"
    # fabricated citation removed; item dropped by the revision step
    assert "fake-cite" not in final and "99999999" not in md
    # unsupported claim removed from the supportive item
    assert len(final["sup-seizures"]["claims"]) == 1
    # parser overrides the model's wrong demographics
    assert res.audit["stages"]["A_interpretation"]["patient_age_years"] == 5.0
    assert res.audit["stages"]["A_interpretation"]["patient_sex"] == "female"
    # all links come from the ledger
    assert res.audit["qc"]["links"]["ok"]
    # bespoke verdict rendered, outcome measures nested bullets
    assert "**Verdict:** UNCERTAIN" in md and "  - **Safety metrics:**" in md
    # empty-section statements are deterministic and mention searched sources
    assert "### Existing Targeted Therapies\n\n- No approved or guideline-supported therapy" in md
    # mock output must be rejected by the submission validator
    assert not validate_submission([res.output.model_dump()], ["TEST-0001"]).ok
    # audit provenance
    assert res.audit["prompts_sha256"] and res.audit["settings"]["primary_model"] == "gemini-4-argon"
    assert any(e["rule"] == "trial_not_recruiting" for e in res.audit["rule_events"])


def test_reviewer_path(settings, fake_http, test_case):
    res, _ = _run(settings, fake_http, test_case, reviewer=True)
    assert res.audit["stages"]["E_review"]["adjudication"]["decisions"][0]["decision"] == "reject"
    assert check_report_structure(res.output.report_markdown, "TEST-0001").ok


def test_invalid_input_rejected(settings, fake_http):
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Pipeline(MockLLM(HANDLERS), settings, http=fake_http).run({"id": "X", "patient": {"genotype": []}})
