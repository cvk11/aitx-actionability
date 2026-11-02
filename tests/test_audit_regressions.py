"""Regression tests for the independent audit of commit 9be0d2c (findings B1-B9 + medium issues).

Every test reproduces the audited failure on synthetic data and must fail on the
audited implementation; see REMEDIATION_REPORT.md for the mapping."""
from __future__ import annotations

import json
import re
import threading

import httpx
import pytest
from fake_http import handler
from mock_handlers import HANDLERS

from aitx.http import HttpClient
from aitx.ledger import EvidenceLedger, EvidenceRecord
from aitx.llm.base import BudgetExceeded, CostTracker
from aitx.llm.mock import MockLLM
from aitx.pipeline import Pipeline
from aitx.render import render_item
from aitx.rules import apply_section_rules, apply_verdicts
from aitx.schemas import ActionItem, ClaimVerdict, Section, VerificationBatch


def _item(iid, section, claims, statement="Statement [PMID:1].", **kw):
    return ActionItem(item_id=iid, section=section, title=kw.pop("title", "Drug Q"), statement=statement, conditional_action="",
                      evidence_level=kw.pop("evidence_level", "human_same_gene"), priority=1,
                      claims=[{"text": t, "citations": c, "kind": k} for t, c, k in claims], missing_information=[], **kw)


def _v(iid, idx, verdict="supported", ev="direct_human_same_gene", rev="", quote="A child was treated with drug Q", key="PMID:1",
       app="same_gene_same_mechanism", im=True):
    return ClaimVerdict(item_id=iid, claim_index=idx, verdict=verdict, evidence_type=ev, overgeneralization="", abstract_only=False,
                        supporting_quote=quote, quote_source_key=key, applicability=app, intervention_matches=im,
                        rationale="r", suggested_revision=rev)


def _ledger_human_and_mouse():
    L = EvidenceLedger()
    L.add(EvidenceRecord("PMID:1", "pubmed", "Drug Q in a child with GENEX disease", "u1", content="A child was treated with drug Q and improved.",
                         content_scope="abstract", metadata={"pub_types": ["Case Reports"]}))
    L.add(EvidenceRecord("PMID:2", "pubmed", "Drug Q in Genex mice", "u2", content="Mice were treated. No patients received the drug.",
                         content_scope="abstract", metadata={"pub_types": ["Journal Article"]}))
    return L


# ---------------------------------------------------------------------- B1
def test_b1_missing_verdicts_fail_closed():
    it = _item("off-1", Section.OFF_LABEL, [("Drug Q improved seizures in a child.", ["PMID:1"], "treatment_effect_human")])
    res = apply_verdicts([it], [])  # verifier returned nothing for this item
    assert res.items == [], "an item without verification decisions must not pass"


def test_b1_llm_verify_requires_complete_unique_verdicts():
    from aitx.agents import verifier

    it = _item("off-1", Section.OFF_LABEL, [("c0", ["PMID:1"], "treatment_effect_human"), ("c1", ["PMID:1"], "disease_fact")])
    calls = []

    def verify(ctx):
        calls.append(ctx)
        if len(calls) == 1:  # first answer: one claim missing, statement verdict duplicated with conflict
            return VerificationBatch(verdicts=[_v("off-1", 0), _v("off-1", -1), _v("off-1", -1, "contradicted")])
        return VerificationBatch(verdicts=[_v("off-1", 1)])  # bounded re-verification of the missing claim

    out = verifier.llm_verify([it], _ledger_human_and_mouse(), MockLLM({"D_verify": verify}), "medium")
    got = {(v.item_id, v.claim_index): v.verdict for v in out.verdicts}
    assert got == {("off-1", 0): "supported", ("off-1", 1): "supported", ("off-1", -1): "contradicted"}  # most conservative kept
    assert out.coverage["missing_after_reverification"] == [] and out.coverage["conflicting_duplicates"] == [["off-1", -1]]
    assert len(calls) == 2


# ---------------------------------------------------------------------- B2
def test_b2_unverified_synthesis_and_foreign_links_are_rejected(settings, fake_http, test_case):
    from mock_handlers import synthesize as base_synth

    def synth(ctx):
        s = base_synth(ctx)
        s.summary_lines[0].text = "Give unproven drug X [evil](https://unlisted.example/x)"
        s.missing_information.append("Weight would change the recommendations: if low, then give unproven drug X; otherwise nothing.")
        return s

    def verify(ctx):
        claims = json.loads(re.search(r"Claims to verify \(JSON\):\n(\[.*?\])\n\nSource texts:", ctx["system"], re.S).group(1))
        return VerificationBatch(verdicts=[_v(c["item_id"], c["claim_index"], "unsupported" if "unproven" in c["claim"] else "supported")
                                           for c in claims])

    res = Pipeline(MockLLM(HANDLERS | {"F_synthesize": synth, "D_verify": verify, "D_verify_synth": verify}), settings,
                   http=fake_http, parallel=False).run(test_case)
    md = res.output.report_markdown
    assert "unlisted.example" not in md and "unproven" not in md
    assert res.audit["qc"]["links"]["ok"]


# ---------------------------------------------------------------------- B3
def test_b3_verifier_revision_is_not_auto_accepted():
    it = _item("sup-1", Section.SUPPORTIVE, [("Seizures are treated with ASMs.", ["PMID:1"], "disease_fact")], statement="Original [PMID:1].")
    res = apply_verdicts([it], [_v("sup-1", 0), _v("sup-1", -1, "partially_supported", rev="New unchecked clinical assertion.")])
    kept = res.items[0] if res.items else None
    assert kept is None or kept.statement != "New unchecked clinical assertion."
    assert res.problems.get("sup-1"), "partially supported statement must require revision + re-verification"
    assert res.statement_status["sup-1"] == "partially_supported"


# ---------------------------------------------------------------------- B4
def test_b4_clinvar_transcript_version_needs_genomic_equivalence(settings):
    from aitx.retrieval import ncbi

    def no_vv(req):  # genomic coordinates of the patient's variant unavailable
        return httpx.Response(404) if req.url.host == "rest.variantvalidator.org" else handler(req)

    http = HttpClient(settings, transport=httpx.MockTransport(no_vv))
    L = EvidenceLedger()
    r = ncbi.clinvar_lookup(http, L, "GENEX", "c.100G>A", "NM_999999.2")  # fake ClinVar title is on NM_999999.1
    assert r["records"] == [] and "CLINVAR:VCV000000123" not in L
    assert r["version_mismatch_candidates"]
    # same c. on another version is accepted only when GRCh38 coordinates/alleles match the patient's variant
    r2 = ncbi.clinvar_lookup(http, L, "GENEX", "c.100G>A", "NM_999999.2", genomic_vcf="NC_000001.11-1000-G-A")
    assert [x["key"] for x in r2["records"]] == ["CLINVAR:VCV000000123"] and r2["records"][0]["match"] == "genomic_equivalent_other_transcript_version"
    r3 = ncbi.clinvar_lookup(http, EvidenceLedger(), "GENEX", "c.100G>A", "NM_999999.2", genomic_vcf="NC_000001.11-1000-G-T")
    assert r3["records"] == []


def test_b4_vep_never_falls_back_to_another_transcript(settings):
    from aitx.retrieval import variant

    def h(req):
        return httpx.Response(200, json=[{"most_severe_consequence": "missense_variant", "seq_region_name": "1", "start": 1, "end": 1,
                                          "transcript_consequences": [{"transcript_id": "NM_888888.1", "consequence_terms": ["stop_gained"], "exon": "9/9"}]}])

    r = variant.ensembl_vep(HttpClient(settings, transport=httpx.MockTransport(h)), "NM_999999.1", "c.100G>A")
    assert r["ok"] is False and "NM_999999.1" in r["error"]


# ---------------------------------------------------------------------- B5
def test_b5_word_patient_is_not_human_treatment_precedent():
    """A mouse study that says 'no patients received the drug' is not human precedent. (Placement is now decided
    after verification from verified evidence; the former publication-type pre-filter was removed — see test_s3.)"""
    L = _ledger_human_and_mouse()
    it = _item("off-m", Section.OFF_LABEL, [("Drug Q improved mice.", ["PMID:2"], "treatment_effect_preclinical")], statement="Drug Q [PMID:2].")
    res = apply_verdicts([it], [_v("off-m", 0, ev="preclinical", quote="Mice were treated", key="PMID:2"), _v("off-m", -1)], ledger=L)
    assert res.items[0].section != Section.OFF_LABEL


def test_b5_targeted_requires_verified_indication_claim():
    L = _ledger_human_and_mouse()
    L.add(EvidenceRecord("FDALABEL:x", "fda_label", "FDA label: drug Q", "u3", content="Indicated for hypertension."))
    it = _item("tgt", Section.EXISTING_TARGETED, [("Drug Q is approved for GENEX disease.", ["FDALABEL:x"], "regulatory_status")],
               statement="Drug Q is approved [FDALABEL:x].", evidence_level="regulatory_approval")
    res = apply_verdicts(apply_section_rules([it], L, 5.0, "female").items,
                         [_v("tgt", 0, "unsupported", "none"), _v("tgt", -1, "supported")], ledger=L)
    assert all(i.section != Section.EXISTING_TARGETED for i in res.items)


def test_b5_preclinical_support_preserved_in_tier3():
    """Phase 3: rejecting the human-treatment claim must not discard supported preclinical evidence."""
    L = _ledger_human_and_mouse()
    it = _item("off-x", Section.OFF_LABEL, [("Drug Q improved a patient.", ["PMID:1"], "treatment_effect_human"),
                                            ("Drug Q rescued Genex mice.", ["PMID:2"], "treatment_effect_preclinical")])
    res = apply_verdicts([it], [_v("off-x", 0, "unsupported", "none"), _v("off-x", 1, "supported", "preclinical"), _v("off-x", -1, "supported")],
                         ledger=L)
    assert [i.section for i in res.items] == [Section.REPURPOSING]
    assert res.statement_status["off-x"] != "supported"  # statement must be rewritten for Tier 3 and re-verified


# ---------------------------------------------------------------------- B6
def test_b6_live_recheck_refreshes_eligibility_and_status_classes(settings):
    from aitx.agents import verifier
    from aitx.retrieval import clinicaltrials

    def h(req):
        if req.url.path.endswith("/NCT00000001"):  # live record: now adults only
            from fake_http import STUDY_OPEN
            s = json.loads(json.dumps(STUDY_OPEN))
            s["protocolSection"]["eligibilityModule"]["minimumAge"] = "18 Years"
            return httpx.Response(200, json=s)
        return handler(req)

    http = HttpClient(settings, transport=httpx.MockTransport(h))
    L = EvidenceLedger()
    clinicaltrials.search_trials(http, L, term="GENEX")  # stale ages 2-17
    it = _item("t1", Section.TRIALS, [("recruiting", ["NCT00000001"], "trial_status_or_eligibility")], statement="May qualify [NCT00000001].",
               nct_id="NCT00000001", evidence_level="trial_registry_record")
    verifier.deterministic_checks([it], L, http)
    assert apply_section_rules([it], L, 5.0, "female").items == []  # 5-year-old no longer age-eligible
    assert clinicaltrials.status_class("NOT_YET_RECRUITING") == "not_yet_recruiting"
    assert clinicaltrials.status_class("RECRUITING") == "actively_recruiting"


def test_b6_failed_live_check_is_unverified_not_absent(settings):
    from aitx.retrieval import clinicaltrials

    st = clinicaltrials.trial_status(HttpClient(settings, transport=httpx.MockTransport(lambda r: httpx.Response(403))), "NCT00000001")
    assert st["exists"] is None and st["status"] is None


def test_b6_not_yet_recruiting_rendered_distinctly():
    L = EvidenceLedger()
    L.add(EvidenceRecord("NCT00000003", "clinicaltrials", "Future trial", "https://clinicaltrials.gov/study/NCT00000003",
                         metadata={"overall_status": "NOT_YET_RECRUITING"}))
    it = _item("t3", Section.TRIALS, [("x", ["NCT00000003"], "trial_status_or_eligibility")], statement="May qualify [NCT00000003].",
               nct_id="NCT00000003", evidence_level="trial_registry_record")
    txt = render_item(it, L, {})
    assert "not yet recruiting" in txt.lower() and "not currently enrolling" in txt.lower()


# ---------------------------------------------------------------------- B7
def test_b7_parallel_reservations_are_atomic():
    c = CostTracker(budget_usd=1.0)
    ok, barrier = [], threading.Barrier(3)

    def worker():
        barrier.wait()
        try:
            c.reserve("s", 0.5)
            ok.append(1)
        except BudgetExceeded:
            pass

    ts = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 2


def test_b7_call_refused_when_upper_bound_exceeds_budget(settings):
    from aitx.llm.anthropic_client import ClaudeLLM
    from aitx.schemas import JudgeReport

    class NoCall:
        def __getattr__(self, n):
            raise AssertionError("request must not be sent")

    c = CostTracker(budget_usd=0.10)  # max_tokens alone can cost more than this
    with pytest.raises(BudgetExceeded):
        ClaudeLLM(settings, c, client=NoCall()).structured(stage="s", system="s", user="u", output_model=JudgeReport)


def test_b7_unknown_served_model_priced_conservatively():
    assert CostTracker.price("unknown-model", 1_000_000, 0, 0, 0) >= 4.0


def test_b7_code_default_matches_documented_cap(monkeypatch):
    from aitx.config import Settings

    monkeypatch.delenv("AITX_MAX_COST_USD_PER_CASE", raising=False)
    assert Settings().max_cost_usd_per_case == 12.0


# ---------------------------------------------------------------------- B8
def test_b8_partial_report_is_not_submittable(settings, fake_http, test_case):
    from aitx.submission import SubmissionBlocked, assemble_submission

    def boom(ctx):
        raise BudgetExceeded("simulated")

    res = Pipeline(MockLLM(HANDLERS | {"C_hypothesized": boom}), settings, http=fake_http, parallel=False).run(test_case)
    assert not res.submittable and any(b["code"] == "stage_incomplete" for b in res.blockers)
    with pytest.raises(SubmissionBlocked):
        assemble_submission([res], ["TEST-0001"], allow_mock=True)


# ---------------------------------------------------------------------- B9
def test_b9_no_unscoped_absence_claim():
    L = _ledger_human_and_mouse()
    L.log_query("pubmed_search", {"query": "GENEX"}, ["PMID:2"], total=230)
    it = _item("rep", Section.REPURPOSING, [("mice", ["PMID:2"], "treatment_effect_preclinical")], statement="Drug Q rescued mice [PMID:2].",
               evidence_level="preclinical_same_gene")
    txt = render_item(it, L, {})
    assert "No human treatment precedent exists" not in txt
    assert "searched in this run" in txt and "first" in txt  # scope + truncation are explicit
    L2 = EvidenceLedger()
    L2.log_query("pubmed_search", {"query": "x"}, [], error="timeout")
    assert "could not be assessed" in render_item(it, L2, {})


# ---------------------------------------------------------------------- medium
def test_m_redirect_outside_allowlist_is_blocked(settings):
    from aitx.retrieval.web import fetch_url

    def h(req):
        if req.url.host == "www.fda.gov":
            return httpx.Response(302, headers={"location": "https://unlisted.example/page"})
        return httpx.Response(200, text="<html><title>x</title>secret</html>")

    L = EvidenceLedger()
    r = fetch_url(HttpClient(settings, transport=httpx.MockTransport(h)), L, "https://www.fda.gov/x")
    assert "error" in r and len(L) == 0


def test_m_search_truncation_is_recorded(fake_http):
    from aitx.retrieval import ncbi

    L = EvidenceLedger()
    ncbi.pubmed_search(fake_http, L, "GENEX", max_results=1)
    st = L.retrieval_status()["pubmed_search"]
    assert st["truncated"] == 1


def test_m_multiple_diagnoses_representable():
    from aitx.schemas import Interpretation

    assert "diagnoses" in Interpretation.model_fields


def test_gates_do_not_over_block_a_complete_verified_run(settings, fake_http, test_case):
    from aitx.submission import assemble_submission

    res = Pipeline(MockLLM(HANDLERS), settings, http=fake_http, parallel=False).run(test_case)
    assert [b["code"] for b in res.blockers] == ["mock_output"]
    assert assemble_submission([res], ["TEST-0001"], allow_mock=True)[0]["id"] == "TEST-0001"
    final = {i["item_id"]: i["section"] for i in res.audit["final_items"]}
    assert final["off-r"] == "drug_repurposing"  # supported preclinical candidate preserved in Tier 3


def test_baseline_and_ablation_arms_never_submittable(settings, fake_http, test_case):
    from mock_handlers import HANDLERS_BASELINE

    a = Pipeline(MockLLM(HANDLERS_BASELINE), settings, http=fake_http, mode="baseline").run(test_case)
    assert "comparison_arm_only" in [b["code"] for b in a.blockers]
    b = Pipeline(MockLLM(HANDLERS), settings, http=fake_http, parallel=False, verify_claims=False).run(test_case)
    assert "verification_disabled" in [x["code"] for x in b.blockers]


def test_b4_indel_equivalence_uses_identical_normalisation():
    """Real CFTR F508del: VariantValidator VCF (left-aligned) and ClinVar canonical SPDI (fully justified)
    describe the same deletion; raw coordinates differ (HGVS 3'-shifted g.117559592_117559594del)."""
    from aitx.retrieval.ncbi import _norm_spdi, _norm_vcf

    assert _norm_vcf("NC_000007.14-117559590-ATCT-A") == _norm_spdi("NC_000007.14:117559590:TCTT:T")
    assert _norm_vcf("NC_000007.14-117559590-ATCTT-A") != _norm_spdi("NC_000007.14:117559590:TCTT:T")  # 4-bp vs 3-bp deletion
    assert _norm_vcf("NC_000003.12-4521031-G-A") != _norm_spdi("NC_000003.12:4521030:G:T")
