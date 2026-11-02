"""Regression tests for the second independent audit of commit 36d8d3e (scientific reliability).

Each test reproduces a failure mode of the audited implementation on synthetic data (fictional gene GENEX unless
stated) and checks the corrected behaviour; see SCIENTIFIC_REMEDIATION_REPORT.md for the mapping (S1-S21)."""
from __future__ import annotations

import json
import logging
import re

import httpx
import pytest
from fake_http import FULLTEXT_XML, handler
from mock_handlers import HANDLERS, quote_from_packet

from aitx.agents import verifier
from aitx.agents.verifier import VerifyContext, ground_verdict, verify_payload
from aitx.evidence import mentions_variant, quote_in_source, select_passages, variant_notations
from aitx.http import HttpClient
from aitx.ledger import EvidenceLedger, EvidenceRecord
from aitx.llm.mock import MockLLM
from aitx.pipeline import Pipeline
from aitx.render import render_item, render_report
from aitx.rules import apply_section_rules, apply_verdicts, compose_from_verified_claims
from aitx.schemas import ActionItem, ClaimVerdict, Section, Synthesis, SummaryLine, VerificationBatch

VCTX = VerifyContext(patient_block="Genotype: GENEX NM_999999.1:c.100G>A p.Asp34Asn (heterozygous)", gene="GENEX",
                     notations=variant_notations("c.100G>A", "p.Asp34Asn"))


def item(iid, section, claims, statement="Statement [PMID:1].", **kw):
    return ActionItem(item_id=iid, section=section, title=kw.pop("title", "Drug Q"), statement=statement, conditional_action=kw.pop("conditional_action", ""),
                      evidence_level=kw.pop("evidence_level", "human_same_gene"), priority=1,
                      claims=[{"text": t, "citations": c, "kind": k} for t, c, k in claims], missing_information=[], **kw)


def v(iid, idx, verdict="supported", ev="direct_human_same_gene", app="same_gene_same_mechanism", im=True, quote="", key="", rev=""):
    return ClaimVerdict(item_id=iid, claim_index=idx, verdict=verdict, evidence_type=ev, overgeneralization="", abstract_only=False,
                        supporting_quote=quote, quote_source_key=key, applicability=app, intervention_matches=im, rationale="r", suggested_revision=rev)


def ledger():
    L = EvidenceLedger()
    L.add(EvidenceRecord("PMID:1", "pubmed", "Drug Q in a child with GENEX disease", "u1", content="A child with the GENEX variant p.Asp34Asn was treated with drug Q and improved.",
                         content_scope="abstract", metadata={"pub_types": ["Journal Article"]}))
    L.add(EvidenceRecord("PMID:2", "pubmed", "Compound R in Genex cells", "u2", content="Compound R potentiated GENEX channel activity in HEK293 cells.",
                         content_scope="abstract", metadata={"pub_types": ["Journal Article"]}))
    L.add(EvidenceRecord("PMID:3", "pubmed", "A neighbouring GENEX variant", "u3", content="The p.Asp34Gly variant showed gain-of-function in vitro.",
                         content_scope="abstract", metadata={"pub_types": ["Journal Article"]}))
    return L


# S1 ------------------------------------------------------------------ patient-specific verification
def test_s1_verifier_receives_patient_context_and_variant_flag():
    L = ledger()
    llm = MockLLM({"D_verify": lambda ctx: VerificationBatch(verdicts=[])})
    claims = [{"item_id": "a", "claim_index": 0, "section": "off_label_expanded_access", "intervention": "Drug Q", "claim": "Drug Q improved a child.",
               "kind": "treatment_effect_human", "citations": ["PMID:1", "PMID:3"]}]
    verify_payload(claims, L, llm, "medium", "D_verify", VCTX)
    sysmsg = llm.seen[0]["system"]
    assert "GENEX NM_999999.1:c.100G>A p.Asp34Asn" in sysmsg
    assert "=== [PMID:1]" in sysmsg and "patient_variant_mentioned_in_source=yes" in sysmsg
    assert re.search(r"=== \[PMID:3\].*?patient_variant_mentioned_in_source=no", sysmsg, re.S)


# S2 ------------------------------------------------------------------ deterministic quote grounding
def test_s2_supported_without_verbatim_quote_is_not_accepted():
    L = ledger()
    claim = {"item_id": "a", "claim_index": 0, "claim": "Drug Q improved a child.", "citations": ["PMID:1"], "kind": "treatment_effect_human"}
    cov = {"quote_failures": 0, "applicability_downgrades": 0, "uncited_supported_to_not_checkable": 0}
    bad = ground_verdict(v("a", 0, quote="Drug Q cured all patients", key="PMID:1"), claim, L, VCTX, cov)
    good = ground_verdict(v("a", 0, quote="was treated with drug Q and improved", key="PMID:1"), claim, L, VCTX, cov)
    wrong_src = ground_verdict(v("a", 0, quote="was treated with drug Q and improved", key="PMID:2"), claim, L, VCTX, cov)
    assert bad.verdict == "insufficient_source_text" and good.verdict == "supported" and wrong_src.verdict == "insufficient_source_text"


# S3 ------------------------------------------------------------------ not_checkable loophole + pubtype exclusion
def test_s3_not_checkable_factual_efficacy_claim_cannot_pass():
    it = item("a", Section.SUPPORTIVE, [("Drug Q reduces seizures in GENEX disease.", ["PMID:1"], "treatment_effect_human")])
    res = apply_verdicts([it], [v("a", 0, "not_checkable"), v("a", -1, "not_checkable")], ledger=ledger())
    assert res.items == [] and any(e.rule == "factual_claim_not_checkable" for e in res.events)


def test_s3_tier3_hypothesis_needs_verified_premise_and_is_labelled():
    L = ledger()
    it = item("h", Section.REPURPOSING, [("Compound R potentiated GENEX activity in cells.", ["PMID:2"], "treatment_effect_preclinical"),
                                         ("Potentiation could compensate reduced channel function.", [], "mechanistic_reasoning")],
              statement="Compound R potentiated GENEX in cells [PMID:2]; potentiation might compensate reduced function.", title="Compound R",
              evidence_level="preclinical_same_gene")
    res = apply_verdicts([it], [v("h", 0, ev="preclinical"), v("h", 1, "not_checkable", ev="mechanistic_inference"), v("h", -1, "not_checkable")], ledger=L)
    assert [i.item_id for i in res.items] == ["h"] and res.statement_status["h"] == "hypothesis"
    assert "Hypothesis: mechanistic inference" in render_item(res.items[0], L, {}, "hypothesis")
    only_reasoning = item("r", Section.REPURPOSING, [("Potentiation could help.", [], "mechanistic_reasoning")], statement="Might help.")
    assert apply_verdicts([only_reasoning], [v("r", 0, "not_checkable"), v("r", -1, "not_checkable")], ledger=L).items == []


def test_s3_journal_article_human_report_is_not_excluded_by_publication_type():
    L = ledger()
    it = item("o", Section.OFF_LABEL, [("A child with p.Asp34Asn improved on drug Q.", ["PMID:1"], "treatment_effect_human")],
              statement="Drug Q was given to a child with GENEX disease, who improved [PMID:1].", human_precedent="one child improved [PMID:1]")
    pre = apply_section_rules([it], L, 5.0, "female")
    assert pre.items[0].section == Section.OFF_LABEL  # no publication-type pre-filter any more
    res = apply_verdicts(pre.items, [v("o", 0, app="same_variant"), v("o", -1)], ledger=L)
    assert res.items[0].section == Section.OFF_LABEL and res.items[0].evidence_level == "human_same_variant"


# S4/S5 --------------------------------------------------------------- applicability, intervention identity, neighbour variants
def test_s4_precedent_requires_applicable_context_and_same_intervention():
    L = ledger()
    it = item("o", Section.OFF_LABEL, [("Drug Q improved a patient.", ["PMID:1"], "treatment_effect_human")])
    other_mech = apply_verdicts([it], [v("o", 0, app="same_gene_other_variant_or_mechanism"), v("o", -1)], ledger=L)
    other_drug = apply_verdicts([it], [v("o", 0, app="same_variant", im=False), v("o", -1)], ledger=L)
    assert all(r.items == [] or r.items[0].section == Section.REPURPOSING for r in (other_mech, other_drug))


def test_s5_neighbouring_variant_mechanism_is_not_transferred():
    L = ledger()
    claim = {"item_id": "m", "claim_index": 0, "claim": "This variant is gain-of-function.", "citations": ["PMID:3"], "kind": "molecular_fact"}
    cov = {"quote_failures": 0, "applicability_downgrades": 0, "uncited_supported_to_not_checkable": 0}
    g = ground_verdict(v("m", 0, app="same_variant", quote="showed gain-of-function in vitro", key="PMID:3"), claim, L, VCTX, cov)
    assert g.applicability == "same_gene_other_variant_or_mechanism" and cov["applicability_downgrades"] == 1
    it = item("m", Section.BESPOKE, [("This variant is gain-of-function.", ["PMID:3"], "molecular_fact")], statement="x [PMID:3].", bespoke_verdict="UNCERTAIN")
    res = apply_verdicts([it], [g, v("m", -1)], ledger=L)
    assert res.items == [] and any(e.rule == "variant_mechanism_transfer" for e in res.events)
    assert mentions_variant("The p.Asp34Gly variant", VCTX.notations) == []


# S6 ------------------------------------------------------------------ inaccessible evidence is "not assessable", not false
def test_s6_inaccessible_table_is_not_treated_as_false_and_valid_claims_survive():
    L = ledger()
    it = item("o", Section.OFF_LABEL, [("A patient with this variant responded to drug Q (patient table).", ["PMID:1"], "treatment_effect_human"),
                                       ("Compound R potentiated GENEX in cells.", ["PMID:2"], "treatment_effect_preclinical")])
    res = apply_verdicts([it], [v("o", 0, "insufficient_source_text"), v("o", 1, ev="preclinical"), v("o", -1, "partially_supported")], ledger=L)
    assert any(e.rule == "claim_not_assessable" for e in res.events) and not any(e.rule == "claim_unsupported" for e in res.events)
    assert [i.section for i in res.items] == [Section.REPURPOSING]  # supported preclinical evidence kept, correct tier
    comp = compose_from_verified_claims(res.items[0], res.reindexed)
    assert comp is not None and "potentiated" in comp.statement and "patient table" not in comp.statement


# S7 ------------------------------------------------------------------ evidence passages beyond the first 3,500 characters
def test_s7_table_row_deep_in_full_text_reaches_the_verifier(settings):
    http = HttpClient(settings, transport=httpx.MockTransport(handler))
    L = EvidenceLedger()
    L.add(EvidenceRecord("PMID:11111111", "pubmed", "Drug Q case", "https://pubmed.ncbi.nlm.nih.gov/11111111/", content="Abstract only.",
                         content_scope="abstract", metadata={"pmcid": "PMC1111111"}))
    it = item("o", Section.OFF_LABEL, [("Patient 2 with p.Asp34Asn had a 50% seizure reduction on drug Q.", ["PMID:11111111"], "treatment_effect_human")])
    rep = verifier.deterministic_checks([it], L, http)
    assert rep["fulltext"]["PMID:11111111"] == "loaded"
    rec = L.get("PMID:11111111")
    assert "supplementary files listed but not parsed" in rec.metadata["fulltext_status"]
    text, info = select_passages(rec, "Patient 2 p.Asp34Asn 50% seizure reduction drug Q", extra_terms=VCTX.notations)
    assert "P2 | c.100G>A (p.Asp34Asn) | drug Q | 50% seizure reduction" in text and len(FULLTEXT_XML) > 3500
    assert quote_in_source("P2 | c.100G>A (p.Asp34Asn) | drug Q", rec)


# S8 ------------------------------------------------------------------ candidate handoffs are researched, not lost
def test_s8_candidate_handoff_is_followed_up_with_provenance(settings, fake_http, test_case):
    from mock_handlers import hypothesized

    def hyp_with_handoff(ctx):
        out = hypothesized(ctx)
        from aitx.schemas import CandidateHandoff

        out.candidate_handoffs = [CandidateHandoff(intervention="Drug Q", proposed_section="off_label_expanded_access",
                                                   reason="case report of a treated patient", citations=["PMID:11111111"])]
        out.open_questions = ["Is there a natural-history registry?"]
        return out

    def followup(ctx):
        from aitx.schemas import SectionResearchOutput
        assert "[C_hypothesized-h1] Drug Q" in ctx["system"] and "[open question]" in ctx["system"]
        return SectionResearchOutput(items=[], candidate_handoffs=[], searched_but_not_found=["Drug Q already covered."], open_questions=[])

    res = Pipeline(MockLLM(HANDLERS | {"C_hypothesized": hyp_with_handoff, "E_followup": followup}), settings, http=fake_http, parallel=False).run(test_case)
    st = res.audit["stages"]
    assert st["candidate_handoffs"][0]["handoff_id"] == "C_hypothesized-h1" and "handoff_followup" in st


def test_s8_item_ids_unique():
    from aitx.agents.researcher import merge_items
    from aitx.schemas import SectionResearchOutput

    out = SectionResearchOutput(items=[item("x", Section.SUPPORTIVE, [("c", ["PMID:1"], "disease_fact")])] * 3, candidate_handoffs=[],
                                searched_but_not_found=[], open_questions=[])
    items, _, _ = merge_items({"B_established": out, "B_emerging": out})
    ids = [i.item_id for i in items]
    assert len(ids) == len(set(ids)) == 6


# S9 ------------------------------------------------------------------ synthesis identity and consistency
def test_s9_summary_cannot_swap_the_intervention():
    from aitx.agents.synthesizer import verify_synthesis

    it = item("o", Section.OFF_LABEL, [("c", ["PMID:1"], "treatment_effect_human")], title="Drug Q")
    s = Synthesis(summary_lines=[SummaryLine(tier=2, text="Start drug W [PMID:1]", item_ids=["o"])], missing_information=[],
                  duplicate_item_ids_to_drop=[], outcome_measures=[], calibration_notes=[])
    out, _, events = verify_synthesis(s, [it], ledger(), MockLLM({"D_verify": lambda c: VerificationBatch(verdicts=[])}), "medium")
    assert "drug W" not in out.summary_lines[0].text and "Drug Q" in out.summary_lines[0].text
    assert any("intervention identity" in e["reason"] for e in events)


def test_s9_tier3_items_never_contradicted_by_outcome_section():
    L = ledger()
    it = item("h", Section.REPURPOSING, [("c", ["PMID:2"], "treatment_effect_preclinical")], title="Compound R", evidence_level="preclinical_same_gene")
    md = render_report("X-1", [it], Synthesis(summary_lines=[], missing_information=[], duplicate_item_ids_to_drop=[], outcome_measures=[],
                                              calibration_notes=[]), L)
    om = md.split("### Outcome Measures & Ranking", 1)[1]
    assert "No hypothesized action is recommended" not in om and "1. Compound R" in om and "Not verified in this run" in om


def test_s9_all_rendered_fields_are_in_the_verified_statement():
    it = item("t", Section.TRIALS, [("c", ["NCT00000001"], "trial_status_or_eligibility")], conditional_action="If seizures >4/month, screen.",
              criteria_met=["age 2-17"], criteria_unassessable=["seizure frequency"], potential_exclusions=["prior drug Q"], nct_id="NCT00000001")
    txt = verifier._statement_text(it)
    for part in ("If seizures >4/month", "age 2-17", "seizure frequency", "prior drug Q"):
        assert part in txt
    b = item("b", Section.BESPOKE, [("c", ["PMID:1"], "molecular_fact")], bespoke_verdict="NOT AMENABLE")
    assert verifier._statement_text(b).startswith("Verdict: NOT AMENABLE.")


def test_s9_evidence_label_is_derived_from_verified_evidence():
    it = item("r", Section.REPURPOSING, [("Compound R potentiated GENEX in cells.", ["PMID:2"], "treatment_effect_preclinical")],
              evidence_level="regulatory_approval", title="Compound R")
    res = apply_verdicts([it], [v("r", 0, ev="preclinical"), v("r", -1)], ledger=ledger())
    assert res.items[0].evidence_level == "preclinical_same_gene"


def test_s9_overclaiming_patient_response_is_flagged():
    it = item("o", Section.OFF_LABEL, [("A child improved on drug Q.", ["PMID:1"], "treatment_effect_human")],
              statement="Drug Q will improve this patient's seizures [PMID:1].")
    res = apply_verdicts([it], [v("o", 0, app="same_variant"), v("o", -1)], ledger=ledger())
    assert res.statement_status["o"] == "needs_reverification" and any("will respond" in p or "predicts benefit" in p for p in res.problems["o"])


# S10 ----------------------------------------------------------------- deterministic safeguards
def test_s10_trial_genotype_eligibility():
    L = EvidenceLedger()
    L.add(EvidenceRecord("NCT00000011", "clinicaltrials", "Drug for SCN2A-related epilepsy", "https://clinicaltrials.gov/study/NCT00000011",
                         content="Inclusion: pathogenic variant in SCN2A.", metadata={"overall_status": "RECRUITING", "status_verification": "live_ok"}))
    L.add(EvidenceRecord("NCT00000012", "clinicaltrials", "Drug for GENEX-related epilepsy", "https://clinicaltrials.gov/study/NCT00000012",
                         content="Inclusion: pathogenic variant in GENEX; loss-of-function variants only.", metadata={"overall_status": "RECRUITING"}))
    a = item("a", Section.TRIALS, [("c", ["NCT00000011"], "trial_status_or_eligibility")], nct_id="NCT00000011")
    b = item("b", Section.TRIALS, [("c", ["NCT00000012"], "trial_status_or_eligibility")], nct_id="NCT00000012")
    res = apply_section_rules([a, b], L, 5.0, "female", genes=["GENEX"])
    assert [i.item_id for i in res.items] == ["b"] and any(e.rule == "trial_genotype_ineligible" for e in res.events)
    assert any("GENEX" in c for c in res.items[0].criteria_met) and any("functional class" in c for c in res.items[0].criteria_unassessable)


def test_s10_genomic_accession_version_is_preserved():
    from aitx.retrieval.ncbi import _norm_spdi, _norm_vcf

    assert _norm_vcf("NC_000001.11-1000-G-A") == _norm_spdi("NC_000001.11:999:G:A")
    assert _norm_vcf("NC_000001.11-1000-G-A") != _norm_spdi("NC_000001.10:999:G:A")  # GRCh37 accession must not match GRCh38


def test_s10_deployment_refuses_incomplete_reports_and_logs_no_patient_data(settings, fake_http, test_case, monkeypatch, caplog):
    import importlib.util
    from pathlib import Path

    from aitx.llm.base import BudgetExceeded

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("space_app2", root / "deployment" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._gr_ready.set()

    def boom(ctx):
        raise BudgetExceeded(f"simulated during {test_case['patient']['clinical_context']}")

    monkeypatch.setattr(mod, "_pipeline_factory", lambda: Pipeline(MockLLM(HANDLERS | {"C_hypothesized": boom}), settings, http=fake_http, parallel=False))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(mod.ReportUnavailable) as e:
            mod.actionability_report(test_case)
        bad = dict(test_case, patient=dict(test_case["patient"], genotype=[{"gene": "GENEX-SECRET-VALUE"}]))
        with pytest.raises(Exception) as e2:
            mod.actionability_report(bad)
    assert "stage_incomplete" in str(e.value)
    secret = test_case["patient"]["clinical_context"]
    assert secret not in str(e.value) and "GENEX-SECRET-VALUE" not in mod._safe_validation_message(e2.value)


def test_s10_mock_verifier_quotes_are_real_passages():
    """Guard for the test harness itself: the scripted verifier copies passages from the packet it was shown."""
    L = ledger()
    llm = MockLLM({"D_verify": HANDLERS["D_verify"]})
    claims = [{"item_id": "a", "claim_index": 0, "section": "x", "intervention": "Drug Q", "claim": "Drug Q improved a child.",
               "kind": "treatment_effect_human", "citations": ["PMID:1"]}]
    out = verify_payload(claims, L, llm, "medium", "D_verify", VCTX)
    assert out.verdicts[0].verdict == "supported" and out.coverage["quote_failures"] == 0
