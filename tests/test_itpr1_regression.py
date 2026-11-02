"""ITPR1 p.Asp34Asn regression examples from the second independent audit.

These are REGRESSION EXAMPLES for general mechanisms (variant-specific retrieval, inaccessible patient tables,
preclinical-vs-human tiering, neighbouring-variant mechanisms) — no ITPR1-specific logic exists in src/ or prompts/
(enforced by test_units.py). Offline tests use SYNTHETIC records that mimic the structure of the real sources
(PMID 38844245: cohort, gene listed in abstract, patient-level data only in an inaccessible table; PMID 36712939:
cell-based ITPR1 potentiator ARN11391). Live tests (pytest -m live) query free public APIs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aitx.agents.verifier import VerifyContext, _evidence_packet, ground_verdict
from aitx.evidence import select_passages, variant_notations
from aitx.ledger import EvidenceLedger, EvidenceRecord
from aitx.render import precedent_scope, render_item
from aitx.rules import apply_verdicts
from aitx.schemas import ActionItem, ClaimVerdict, Section

ROOT = Path(__file__).resolve().parents[1]
CASE = next(c for c in json.loads((ROOT / "data/official_cases/aitx_final_patients.json").read_text()) if c["id"] == "AITX-40002")
VAR = CASE["patient"]["genotype"][0]
NOTS = variant_notations(VAR["variant_cdna"], VAR["variant_protein"])
VCTX = VerifyContext(patient_block=f"Genotype: {VAR['gene']} {VAR['transcript']}:{VAR['variant_cdna']} {VAR['variant_protein']}",
                     gene=VAR["gene"], notations=NOTS)


def _item(iid, section, claims, statement, title="Intervention X", **kw):
    return ActionItem(item_id=iid, section=section, title=title, statement=statement, conditional_action="", evidence_level=kw.pop("evidence_level", "none"),
                      priority=1, claims=[{"text": t, "citations": c, "kind": k} for t, c, k in claims], missing_information=[], **kw)


def _v(iid, idx, verdict="supported", ev="preclinical", app="same_gene_same_mechanism", im=True, quote="", key=""):
    return ClaimVerdict(item_id=iid, claim_index=idx, verdict=verdict, evidence_type=ev, overgeneralization="", abstract_only=False, supporting_quote=quote,
                        quote_source_key=key, applicability=app, intervention_matches=im, rationale="r", suggested_revision="")


def _ledger():
    L = EvidenceLedger()
    # synthetic stand-in for PMID 38844245 (structure only; not the article text)
    L.add(EvidenceRecord("PMID:38844245", "pubmed", "Cohort study of a progressive myoclonus-ataxia syndrome", "https://pubmed.ncbi.nlm.nih.gov/38844245/",
                         content="A retrospective cohort; therapy efficacy was collected retrospectively. Molecular causes included ITPR1 among other genes.",
                         content_scope="abstract", metadata={"pub_types": ["Journal Article"],
                                                              "fulltext_status": "no_open_access_full_text (abstract/metadata only; tables and supplements not accessible)"}))
    # synthetic stand-in for PMID 36712939 (cell-based potentiator)
    L.add(EvidenceRecord("PMID:36712939", "pubmed", "High-throughput screening for calcium-signalling potentiators", "https://pubmed.ncbi.nlm.nih.gov/36712939/",
                         content="A compound acted as a selective ITPR1 potentiator in cell assays.", content_scope="full_text_excerpt",
                         metadata={"pub_types": ["Journal Article"], "passages": [("Introduction", "Background " * 400),
                                                                                  ("Results", "ARN11391 selectively potentiated ITPR1-mediated calcium release in cultured cells.")]}))
    # synthetic neighbouring-variant source
    L.add(EvidenceRecord("PMID:90000003", "pubmed", "Functional study of an ITPR1 suppressor-domain variant", "u",
                         content="The neighbouring p.Arg36Cys variant increased channel activity (gain-of-function).", content_scope="abstract"))
    return L


def test_notations_cover_the_official_variant_spellings():
    assert {"p.Asp34Asn", "Asp34Asn", "D34N", "p.D34N", "c.100G>A"} <= NOTS


def test_cohort_with_inaccessible_patient_table_is_not_assessable_and_not_absence():
    L = _ledger()
    claim = {"item_id": "o", "claim_index": 0, "intervention": "Intervention X",
             "claim": "A cohort patient with ITPR1 p.Asp34Asn improved on intervention X.", "citations": ["PMID:38844245"], "kind": "treatment_effect_human"}
    packet = _evidence_packet(claim, L, VCTX)
    assert "patient_variant_mentioned_in_source=no" in packet and "tables and supplements not accessible" in packet
    it = _item("o", Section.OFF_LABEL, [(claim["claim"], ["PMID:38844245"], "treatment_effect_human")], "Intervention X [PMID:38844245].")
    res = apply_verdicts([it], [_v("o", 0, "insufficient_source_text", ev="none"), _v("o", -1, "insufficient_source_text", ev="none")], ledger=L)
    assert any(e.rule == "claim_not_assessable" for e in res.events) and res.items == []
    assert any("not shown to be false" in p for p in res.problems["o"])


def test_same_variant_in_a_table_is_precedent_but_not_a_prediction_for_this_patient():
    L = _ledger()
    L.get("PMID:38844245").metadata["passages"] = [("Table 2: Genotype and therapy", "P7 | ITPR1 c.100G>A (p.Asp34Asn) | intervention X | partial improvement")]
    claim = {"item_id": "o", "claim_index": 0, "claim": "One cohort patient with p.Asp34Asn showed partial improvement on intervention X.",
             "citations": ["PMID:38844245"], "kind": "treatment_effect_human"}
    cov = {"quote_failures": 0, "applicability_downgrades": 0, "uncited_supported_to_not_checkable": 0}
    g = ground_verdict(_v("o", 0, ev="direct_human_same_variant", app="same_variant", quote="(p.Asp34Asn) | intervention X | partial improvement",
                          key="PMID:38844245"), claim, L, VCTX, cov)
    assert g.verdict == "supported" and g.applicability == "same_variant"
    it = _item("o", Section.OFF_LABEL, [(claim["claim"], ["PMID:38844245"], "treatment_effect_human")],
               "Intervention X will improve this patient's ataxia [PMID:38844245].")
    res = apply_verdicts([it], [g, _v("o", -1, ev="direct_human_same_variant", app="same_variant")], ledger=L)
    assert res.items[0].section == Section.OFF_LABEL and res.items[0].evidence_level == "human_same_variant"
    assert res.statement_status["o"] == "needs_reverification"  # 'will improve this patient' is not supported by one cohort patient


def test_arn11391_is_a_preclinical_tier3_candidate_not_human_benefit():
    L = _ledger()
    text, _ = select_passages(L.get("PMID:36712939"), "ARN11391 potentiator ITPR1 calcium release", extra_terms=["ARN11391"])
    assert "ARN11391 selectively potentiated" in text  # found beyond the long introduction
    it = _item("arn", Section.OFF_LABEL, [("ARN11391 improved ataxia in patients.", ["PMID:36712939"], "treatment_effect_human"),
                                          ("ARN11391 potentiated ITPR1-mediated calcium release in cultured cells.", ["PMID:36712939"], "treatment_effect_preclinical")],
               "ARN11391 potentiated ITPR1 in cells [PMID:36712939].", title="ARN11391")
    res = apply_verdicts([it], [_v("arn", 0, "unsupported", ev="none"), _v("arn", 1), _v("arn", -1)], ledger=L)
    out = res.items[0]
    assert out.section == Section.REPURPOSING and out.evidence_level == "preclinical_same_gene" and len(out.claims) == 1
    md = render_item(out, L, {})
    assert "No human treatment precedent exists" not in md and "preclinical" in md


def test_neighbouring_variant_mechanism_is_not_assigned_to_the_patient_variant():
    L = _ledger()
    claim = {"item_id": "m", "claim_index": 0, "claim": "This variant is gain-of-function.", "citations": ["PMID:90000003"], "kind": "molecular_fact"}
    cov = {"quote_failures": 0, "applicability_downgrades": 0, "uncited_supported_to_not_checkable": 0}
    g = ground_verdict(_v("m", 0, ev="preclinical", app="same_variant", quote="increased channel activity (gain-of-function)", key="PMID:90000003"),
                       claim, L, VCTX, cov)
    assert g.applicability == "same_gene_other_variant_or_mechanism"
    it = _item("m", Section.BESPOKE, [(claim["claim"], ["PMID:90000003"], "molecular_fact")], "Mechanism [PMID:90000003].", bespoke_verdict="UNCERTAIN")
    assert apply_verdicts([it], [g, _v("m", -1)], ledger=L).items == []


def test_precedent_statement_is_scoped_when_literature_search_ran():
    L = _ledger()
    L.log_query("pubmed_search", {"query": "x"}, ["PMID:38844245"], total=18)
    s = precedent_scope(L)
    assert "searched in this run" in s and "does not establish that none exists" in s


# ------------------------------------------------------------------ live (free public APIs)
@pytest.mark.live
def test_live_itpr1_evidence_retrieval():
    from aitx.config import get_settings
    from aitx.http import HttpClient
    from aitx.retrieval import europepmc, ncbi

    http = HttpClient(get_settings(deployment_mode=True))
    L = EvidenceLedger()
    vl = europepmc.variant_literature_search(http, L, VAR["gene"], sorted(NOTS))
    assert (vl.get("europepmc_hits") or 0) >= 1  # notation found in full text (PubMed abstracts alone return none)
    pm = ncbi.pubmed_search(http, L, "ITPR1[tiab] AND (myoclonus OR ataxia) AND (treatment OR therapy)", 50)
    assert "PMID:38844245" in [r["key"] for r in pm["results"]]
    a = ncbi.pubmed_fetch_abstracts(http, L, ["38844245", "36712939"])
    by = {x["key"]: x for x in a["articles"]}
    assert by["PMID:38844245"]["pmcid"] == "" and by["PMID:36712939"]["pmcid"] == "PMC9830948"
    assert europepmc.load_fulltext(http, L, "PMID:36712939", "PMC9830948") == "loaded"
    text, _ = select_passages(L.get("PMID:36712939"), "ARN11391 selective potentiator ITPR1", extra_terms=["ARN11391"])
    assert "ARN11391" in text


@pytest.mark.live
def test_live_itpr1_mandatory_therapeutic_coverage_finds_both_regression_publications():
    """Generic (gene-parameterised) coverage queries surface the preclinical potentiator study and the treatment
    cohort without any case-specific query (the auditor's targeted query was needed before)."""
    from aitx.config import get_settings
    from aitx.http import HttpClient
    from aitx.retrieval import ncbi

    cov = ncbi.therapeutic_coverage_search(HttpClient(get_settings(deployment_mode=True)), EvidenceLedger(), VAR["gene"])
    assert all(not f["error"] for f in cov.values())
    assert "PMID:36712939" in cov["activating_modulators"]["keys"]
    assert "PMID:38844245" in cov["human_treatment_reports"]["keys"]
