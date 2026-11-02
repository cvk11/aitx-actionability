"""Regression tests for the residual findings of the second audit report (re-checked against commit 9e1084d).

Each test reproduces a gap that was still open after the S1-S10 remediation:
R1 model text / input values in parse errors and logs, R2 billed fallback iterations not charged,
R3 same verdict with conflicting evidence type / applicability, R4 submission gate trusting the issue list,
R5 free-text negative findings rendered unverified, R6 gene-level searches used as candidate-specific absence,
R7 not-yet-recruiting trials presented as enrolment options, R8 no mandatory therapeutic coverage check,
R9 ClinVar alleles named on another transcript / repeat-region indels. See SCIENTIFIC_REMEDIATION_REPORT.md."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from mock_handlers import HANDLERS
from test_audit_regressions import _item, _ledger_human_and_mouse, _v

from aitx.ledger import EvidenceLedger, EvidenceRecord
from aitx.llm.mock import MockLLM
from aitx.pipeline import Pipeline
from aitx.render import render_item, render_report
from aitx.rules import apply_section_rules
from aitx.schemas import Section, SectionResearchOutput, VerificationBatch


# ---------------------------------------------------------------- R1
def test_r1_parse_error_contains_no_model_text_or_input_values():
    from aitx.llm.anthropic_client import ClaudeLLM
    from aitx.llm.base import StructuredOutputError

    llm = object.__new__(ClaudeLLM)
    secret = "PATIENT-SECRET-7y-male"
    msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"verdicts": [{"item_id": "%s"}]}' % secret)])
    with pytest.raises(StructuredOutputError) as e:
        llm._parse("D_verify", msg, VerificationBatch)
    assert secret not in str(e.value) and e.value.__cause__ is None
    assert "verdicts.0" in str(e.value)  # field locations are kept for debugging
    bad = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json " + secret)])
    with pytest.raises(StructuredOutputError) as e2:
        llm._parse("D_verify", bad, VerificationBatch)
    assert secret not in str(e2.value)


# ---------------------------------------------------------------- R2
def test_r2_every_billed_iteration_is_charged():
    from aitx.llm.anthropic_client import usage_cost
    from aitx.llm.base import CostTracker

    top = dict(input_tokens=1000, output_tokens=1000, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    plain = SimpleNamespace(**top)
    c_plain, _ = usage_cost(plain, "claude-opus-5-5")
    assert c_plain == pytest.approx(CostTracker.price("claude-opus-5-5", 1000, 1000, 0, 0))
    # a refused first attempt on the requested model + a fallback attempt on another model
    with_iters = SimpleNamespace(**top, iterations=[dict(top, model="claude-opus-5-5"), dict(top, model="some-unlisted-model")])
    c_it, tok = usage_cost(with_iters, "some-unlisted-model")
    assert c_it > c_plain and tok[0] == 2000
    assert c_it == pytest.approx(CostTracker.price("claude-opus-5-5", 1000, 1000, 0, 0) + CostTracker.price("unknown", 1000, 1000, 0, 0))


# ---------------------------------------------------------------- R3
def test_r3_same_verdict_conflicting_evidence_type_keeps_the_weakest():
    from aitx.agents.verifier import _collect

    a = _v("off-1", 0, ev="direct_human_same_variant", app="same_variant")
    b = _v("off-1", 0, ev="preclinical", app="same_gene_other_variant_or_mechanism", im=False)
    cov = {"conflicting_duplicates": [], "identical_duplicates": 0, "malformed_or_unexpected": 0}
    got: dict = {}
    _collect(VerificationBatch(verdicts=[a, b]), {("off-1", 0)}, got, cov)
    v = got[("off-1", 0)]
    assert (v.evidence_type, v.applicability, v.intervention_matches) == ("preclinical", "same_gene_other_variant_or_mechanism", False)
    assert cov["conflicting_duplicates"] == [["off-1", 0]]
    got2: dict = {}
    cov2 = {"conflicting_duplicates": [], "identical_duplicates": 0, "malformed_or_unexpected": 0}
    _collect(VerificationBatch(verdicts=[a, a]), {("off-1", 0)}, got2, cov2)
    assert cov2["identical_duplicates"] == 1 and got2[("off-1", 0)].evidence_type == "direct_human_same_variant"


# ---------------------------------------------------------------- R4
def test_r4_submission_gate_checks_completion_history_not_only_issue_list(settings, fake_http, test_case):
    from aitx.submission import SubmissionBlocked, assemble_submission

    res = Pipeline(MockLLM(HANDLERS), settings, http=fake_http, parallel=False).run(test_case)
    assert assemble_submission([res], ["TEST-0001"], allow_mock=True)  # complete run passes
    for mutate, why in ((lambda a: a.pop("stages_completed"), "stages"),
                        (lambda a: a["stages"].pop("D_verification_rounds"), "claim-verification"),
                        (lambda a: a["stages"].pop("F_synthesis_verification"), "synthesis verification"),
                        (lambda a: a["stages"]["D_verification_rounds"][0]["coverage"].update(missing_after_reverification=[["x", 0]]), "incomplete")):
        import copy

        bad = copy.deepcopy(res)
        mutate(bad.audit)
        bad.audit["issues"] = [i for i in bad.audit.get("issues", []) if i["severity"] != "critical" or i["code"] == "mock_output"]
        with pytest.raises(SubmissionBlocked) as e:
            assemble_submission([bad], ["TEST-0001"], allow_mock=True)
        assert why in str(e.value)


# ---------------------------------------------------------------- R5
def test_r5_free_text_negative_findings_are_not_rendered():
    L = EvidenceLedger()
    L.log_query("search_trials", {"term": "GENEX"}, [])
    md = render_report("TEST-0001", [], _empty_synthesis(), L, {Section.TRIALS: ["No trial exists anywhere for this disease and none ever will."]})
    assert "none ever will" not in md
    assert "No actively recruiting clinical trial relevant to this patient was identified" in md


def _empty_synthesis():
    from mock_handlers import synthesize

    s = synthesize({"system": "", "user": ""})
    return s.model_copy(update={"summary_lines": [], "outcome_measures": []})


# ---------------------------------------------------------------- R6
def test_r6_gene_level_search_is_not_candidate_specific_absence():
    L = _ledger_human_and_mouse()
    L.log_query("pubmed_search", {"query": "GENEX[tiab] AND (treat*[tiab])"}, ["PMID:2"])
    it = _item("rep", Section.REPURPOSING, [("mice", ["PMID:2"], "treatment_effect_preclinical")], statement="Compoundine rescued mice [PMID:2].",
               evidence_level="preclinical_same_gene", title="Compoundine")
    txt = render_item(it, L, {})
    assert "not specifically searched" in txt and "No human treatment precedent in this gene" not in txt
    L.log_query("pubmed_search", {"query": "compoundine AND (patient OR case)"}, [])
    assert "No human treatment precedent in this gene/disease/variant context was identified" in render_item(it, L, {})


# ---------------------------------------------------------------- R7
def test_r7_not_yet_recruiting_trial_is_labelled_as_future_option_only():
    L = EvidenceLedger()
    L.add(EvidenceRecord("NCT00000009", "clinicaltrials", "Study of drug Q in GENEX disease", "https://clinicaltrials.gov/study/NCT00000009",
                         content="Inclusion: GENEX disease.", content_scope="structured_record",
                         metadata={"overall_status": "NOT_YET_RECRUITING", "status_verification": "live_ok"}))
    it = _item("trial-n", Section.TRIALS, [("Trial of drug Q", ["NCT00000009"], "trial_status_or_eligibility")], statement="Trial [NCT00000009].",
               nct_id="NCT00000009", evidence_level="trial_registry_record")
    res = apply_section_rules([it], L, 7.0, "male", ["GENEX"])
    kept = res.items[0]
    assert kept.priority >= 50 and any("future option only" in c for c in kept.criteria_unassessable)


# ---------------------------------------------------------------- R8
def test_r8_coverage_gaps_become_follow_up_requests():
    from aitx.agents.researcher import coverage_gaps

    dossier = {"therapeutic_coverage": {"GENEX": {
        "activating_modulators": {"query": "q1", "total": 3, "keys": ["PMID:1", "PMID:7"], "titles": {"PMID:7": "A potentiator of GENEX"},
                                  "truncated": False, "error": ""},
        "nucleic_acid_therapies": {"query": "q2", "total": None, "keys": [], "titles": {}, "truncated": False, "error": "HTTPFetchError"}}}}
    it = _item("off-1", Section.OFF_LABEL, [("c", ["PMID:1"], "treatment_effect_human")])
    reqs, rows = coverage_gaps(dossier, [it])
    unused = [r for r in reqs if "that no candidate used" in r]
    assert len(unused) == 1 and "PMID:7" in unused[0] and "PMID:1" not in unused[0].split("used:")[1]
    assert {r["family"]: r["error"] for r in rows}["nucleic_acid_therapies"] == "HTTPFetchError"
    # a failed mandatory family is re-run, never treated as "no hits" (retrieval-failure remediation)
    rerun = [r for r in reqs if "FAILED" in r]
    assert len(rerun) == 1 and "'q2'" in rerun[0] and "not treat missing hits as evidence of absence" in rerun[0]


def test_r8_pipeline_runs_mandatory_coverage_and_researches_unused_hits(settings, fake_http, test_case):
    seen = []

    def no_tier3(ctx):  # the Tier-3 researcher returns nothing, so coverage hits stay unused
        return SectionResearchOutput(items=[], candidate_handoffs=[], searched_but_not_found=[], open_questions=[])

    def followup(ctx):
        seen.append(ctx["system"])
        return SectionResearchOutput(items=[], candidate_handoffs=[], searched_but_not_found=["reviewed"], open_questions=[])

    res = Pipeline(MockLLM(HANDLERS | {"C_hypothesized": no_tier3, "E_followup": followup}), settings, http=fake_http, parallel=False).run(test_case)
    rows = res.audit["stages"]["therapeutic_coverage"]
    assert {r["family"] for r in rows} == {"activating_modulators", "inhibiting_modulators", "human_treatment_reports", "nucleic_acid_therapies"}
    unused = [r for r in rows if r["unused"]]
    if unused:
        assert seen and all(f"[coverage:{r['gene']}:{r['family']}]" in seen[0] for r in unused)


# ---------------------------------------------------------------- R9
class _SeqHTTP:
    def __init__(self, seq: str | None):
        self.seq, self.calls = seq, 0

    def request(self, method, url, params=None, **kw):
        self.calls += 1
        if self.seq is None:
            return SimpleNamespace(status=503, text="")
        lo, hi = params["seq_start"] - 1, params["seq_stop"]
        return SimpleNamespace(status=200, text=">ref\n" + self.seq[lo:hi])


def test_r9_repeat_region_deletions_are_equivalent_only_after_reference_left_alignment():
    from aitx.retrieval.ncbi import alleles_equivalent

    seq = "G" * 104 + "C" + "ATATAT" + "G" * 60
    a, b = ("NC_000001.11", 105, "AT", ""), ("NC_000001.11", 107, "AT", "")
    assert alleles_equivalent(a, b, _SeqHTTP(seq))
    assert not alleles_equivalent(a, b, _SeqHTTP(None))  # reference unavailable -> not assumed equivalent
    assert not alleles_equivalent(a, ("NC_000001.10", 105, "AT", ""), _SeqHTTP(seq))  # other assembly
    assert not alleles_equivalent(a, ("NC_000001.11", 104, "CA", ""), _SeqHTTP(seq))  # different deleted bases


def test_r9_clinvar_record_named_on_another_transcript_matches_by_genomic_allele():
    from aitx.retrieval.ncbi import clinvar_lookup

    rec = {"accession": "VCV000000777", "title": "NM_888888.2(GENEX):c.900G>A (p.Asp300Asn)", "germline_classification": {},
           "variation_set": [{"canonical_spdi": "NC_000003.12:4000:G:A"}]}
    other = dict(rec, accession="VCV000000778", title="NM_888888.2(GENEX):c.903G>A (p.Glu301Lys)",
                 variation_set=[{"canonical_spdi": "NC_000003.12:4003:G:A"}])

    class H:  # minimal client: E-utilities responses go through the same validators as real ones
        def request(self, method, url, params=None, validate=None, **kw):
            import json as _json
            from urllib.parse import urlencode

            from aitx.http import HTTPResult

            body = ({"esearchresult": {"count": "2", "idlist": ["777", "778"]}} if url.endswith("esearch.fcgi")
                    else {"result": {"uids": ["777", "778"], "777": rec, "778": other}})
            r = HTTPResult(url + "?" + urlencode(params or {}), 200, _json.dumps(body), "2026-01-01T00:00:00Z", False)
            if validate:
                validate(r)
            return r

    L = EvidenceLedger()
    out = clinvar_lookup(H(), L, "GENEX", "c.100G>A", "NM_999999.1", genomic_vcf="NC_000003.12-4001-G-A")
    assert [r["key"] for r in out["records"]] == ["CLINVAR:VCV000000777"]
    assert out["records"][0]["match"] == "genomic_equivalent_other_transcript"
    assert L.get("CLINVAR:VCV000000778") is None
