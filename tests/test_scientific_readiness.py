"""Scientific-readiness regression tests (post-discovery, from the Phase 3 independent comparison).

These are REGRESSION EXAMPLES for general mechanisms. All records are SYNTHETIC: they mimic the *structure* of the
real sources (a cohort whose per-patient data sits in an inaccessible table; a preclinical study reporting both a
rescue and a harmful control finding; a registry eligibility text split by arm; same-residue and neighbouring-residue
functional studies). No real publication finding is asserted, and no case-specific logic exists in src/ or prompts/
(enforced by test_units.py::test_no_official_case_specifics_in_code_or_prompts).

Covered requirements:
  R1 full texts, tables and supplements retrievable/searchable with explicit access limitations
  R2 publication / variant / individual patient / intervention / outcome not conflated
  R3 another patient's or a cohort's response not attributable to this patient
  R4 negative, harmful and null findings preserved alongside positive ones
  R5 trial eligibility judged on the correct arm, age, diagnosis and exclusions
  R6 unsupported GoF/LoF assumptions cannot determine patient-specific therapy
"""

from __future__ import annotations

import pytest

from aitx.agents.verifier import VerifyContext, _evidence_packet, ground_verdict
from aitx.evidence import contrast_passages, select_passages, variant_notations
from aitx.ledger import EvidenceLedger, EvidenceRecord
from aitx.retrieval.clinicaltrials import _flatten, arm_headings
from aitx.rules import apply_section_rules, apply_verdicts
from aitx.schemas import ActionItem, ClaimVerdict, Section

# A fictional gene/variant used wherever the test does not need the official notation.
GENE, CDNA, PROT = "GENEX", "c.100G>A", "p.Asp34Asn"
NOTS = variant_notations(CDNA, PROT)
VCTX = VerifyContext(patient_block=f"Genotype: {GENE} NM_999999.1:{CDNA} {PROT}", gene=GENE, notations=NOTS)


def _item(iid, section, claims, statement, title="Intervention X", **kw):
    return ActionItem(item_id=iid, section=section, title=title, statement=statement, conditional_action="",
                      evidence_level=kw.pop("evidence_level", "none"), priority=1,
                      claims=[{"text": t, "citations": c, "kind": k} for t, c, k in claims], missing_information=[], **kw)


def _v(iid, idx, verdict="supported", ev="preclinical", app="same_gene_same_mechanism", im=True, quote="", key="", over=""):
    return ClaimVerdict(item_id=iid, claim_index=idx, verdict=verdict, evidence_type=ev, overgeneralization=over,
                        abstract_only=False, supporting_quote=quote, quote_source_key=key, applicability=app,
                        intervention_matches=im, rationale="r", suggested_revision="")


# ---------------------------------------------------------------- R1/R2/R3: cohort patient identity
COHORT_KEY = "PMID:90000101"


def _cohort_ledger(table_accessible: bool) -> EvidenceLedger:
    """A cohort paper that lists the patient's variant among its molecular causes.

    `table_accessible=False` mimics a paywalled article whose per-patient therapy table cannot be read.
    `table_accessible=True` mimics an article whose table row identifies the individual and their genotype but
    reports NO treatment outcome for them."""
    L = EvidenceLedger()
    rec = EvidenceRecord(COHORT_KEY, "pubmed", "Natural history of a progressive myoclonus-ataxia syndrome", "u",
                         content=f"Retrospective cohort of 34 individuals. Molecular causes included {GENE} among other genes. "
                                 "Therapy efficacy was collected retrospectively.",
                         content_scope="abstract",
                         metadata={"pub_types": ["Journal Article"]})
    if table_accessible:
        rec.metadata["passages"] = [
            ("Table 2: Genotype of included individuals",
             f"[Patient | Gene | Variant | Age at onset] PMA-29 | {GENE} | {CDNA} ({PROT}) | 3 years"),
            ("Methods", "Treatment data were recorded at cohort level; individual response data are not reported."),
        ]
        rec.content_scope = "full_text_excerpt"
        rec.metadata["fulltext_status"] = "loaded"
    else:
        rec.metadata["fulltext_status"] = "no_open_access_full_text (abstract/metadata only; tables and supplements not accessible)"
    L.add(rec)
    return L


def test_r1_inaccessible_cohort_table_is_an_access_limitation_not_absence():
    """The access limitation must be visible to the verifier, who then has `insufficient_source_text` available."""
    L = _cohort_ledger(table_accessible=False)
    claim = {"item_id": "x", "claim_index": 0, "section": "off_label", "intervention": "Intervention X",
             "claim": f"A cohort individual with {GENE} {PROT} improved on intervention X.", "kind": "treatment_effect_human",
             "citations": [COHORT_KEY]}
    packet = _evidence_packet(claim, L, VCTX)
    assert "no_open_access_full_text" in packet and "tables and supplements not accessible" in packet
    # and a verdict of "unsupported" is NOT what a missing table warrants - the schema offers the distinct verdict
    assert "insufficient_source_text" in ClaimVerdict.model_fields["verdict"].annotation.__args__


def test_r2_cohort_individual_is_a_separate_person_from_the_patient():
    """A table row identifying ANOTHER individual with the same variant must not become a claim about this patient,
    and must not be upgraded to same_variant applicability for an outcome the source never reports."""
    L = _cohort_ledger(table_accessible=True)
    # the variant IS mentioned in the source, so same_variant applicability survives grounding ...
    claim_id = {"item_id": "id1", "claim_index": 0, "section": "off_label", "intervention": "Intervention X",
                "claim": f"A separate individual (PMA-29) in the cohort carries {CDNA}.", "kind": "molecular_fact",
                "citations": [COHORT_KEY]}
    cov = _cov()
    v = ground_verdict(_v("id1", 0, ev="direct_human_same_variant", app="same_variant",
                          quote=f"PMA-29 | {GENE} | {CDNA}", key=COHORT_KEY), claim_id, L, VCTX, cov)
    assert v.applicability == "same_variant" and v.verdict == "supported"
    # ... but a claim that invents a treatment RESPONSE for that individual has no quote in the source
    claim_tx = {"item_id": "id2", "claim_index": 0, "section": "off_label", "intervention": "Intervention X",
                "claim": "PMA-29 responded to intervention X.", "kind": "treatment_effect_human", "citations": [COHORT_KEY]}
    cov2 = _cov()
    v2 = ground_verdict(_v("id2", 0, ev="direct_human_same_variant", app="same_variant",
                           quote="PMA-29 responded to intervention X", key=COHORT_KEY), claim_tx, L, VCTX, cov2)
    assert v2.verdict == "insufficient_source_text" and cov2["quote_failures"] == 1


def test_r3_cohort_response_cannot_be_rendered_as_this_patients_outcome():
    """Even a correctly cited response in another individual may not be stated as this patient's expected benefit."""
    it = _item("o1", Section.OFF_LABEL,
               [("A cohort individual with the same variant improved on intervention X.", [COHORT_KEY], "treatment_effect_human")],
               f"Intervention X will improve this patient's myoclonus [{COHORT_KEY}].")
    res = apply_verdicts([it], [_v("o1", 0, ev="direct_human_same_variant", app="same_variant", quote="q", key=COHORT_KEY),
                                _v("o1", -1, ev="direct_human_same_variant", app="same_variant", quote="q", key=COHORT_KEY)],
                         ledger=_cohort_ledger(table_accessible=True))
    assert _has(res, "predicts benefit for this patient")
    assert res.statement_status.get("o1") == "needs_reverification"


def _has(res, text: str) -> bool:
    """True if any item carries a revision problem containing `text` (RuleResult.problems is item_id -> [problem])."""
    return any(text in t for ts in res.problems.values() for t in ts)


def _cov() -> dict:
    return {"quote_failures": 0, "applicability_downgrades": 0, "uncited_supported_to_not_checkable": 0}


# ---------------------------------------------------------------- R4: harmful / null findings preserved
def _preclinical_record() -> EvidenceRecord:
    """A preclinical study reporting a rescue in mutants AND a harmful effect in wild-type controls. The harmful
    passage deliberately shares no vocabulary with the claim, which is how such findings get lost."""
    rec = EvidenceRecord("PMID:90000102", "pubmed", "Suppression of calcium release in cerebellar neurons", "u",
                         content="Abstract: chronic suppression was evaluated in a mouse model.", content_scope="full_text_excerpt")
    rec.metadata["passages"] = [
        ("Results", "Chronic enzyme overexpression alleviated age-dependent firing dysfunction and rescued motor incoordination in mutant mice."),
        ("Results", "Unaffected littermates receiving the identical construct displayed reduced rotarod latency and dendritic atrophy."),
    ] + [("Methods", f"Methods paragraph {i} on stereotaxic enzyme overexpression, rescued motor incoordination scoring and firing analysis. " * 5)
         for i in range(20)]
    return rec


@pytest.mark.parametrize("budget", [3500, 1200])
def test_r4_harmful_control_finding_reaches_the_verifier(budget):
    rec = _preclinical_record()
    text, info = select_passages(rec, "Enzyme overexpression rescued motor incoordination in mutant mice",
                                 extra_terms=["overexpression"], max_chars=budget)
    assert "rescued motor incoordination in mutant mice" in text          # the positive result
    assert "reduced rotarod latency and dendritic atrophy" in text        # the harmful control finding
    assert info["contrast_passages"] >= 1 and info["contrast_shown"] >= 1


def test_r4_null_result_is_retained_and_counted():
    rec = _preclinical_record()
    rec.metadata["passages"].append(("Results", "Survival at 12 months showed no significant change between groups."))
    text, info = select_passages(rec, "Enzyme overexpression rescued motor incoordination", max_chars=1200)
    assert "no significant change" in text and info["contrast_shown"] >= 1


def test_r4_verifier_packet_states_how_many_limiting_passages_were_shown():
    L = EvidenceLedger()
    L.add(_preclinical_record())
    claim = {"item_id": "p", "claim_index": 0, "section": "drug_repurposing", "intervention": "Enzyme overexpression",
             "claim": "Enzyme overexpression rescued motor incoordination.", "kind": "treatment_effect_preclinical",
             "citations": ["PMID:90000102"]}
    packet = _evidence_packet(claim, L, VCTX)
    assert "negative_or_limiting_passages_shown=" in packet
    assert "reduced rotarod latency" in packet


def test_r4_contrast_detector_does_not_fire_on_ordinary_positive_text():
    ps = [("Results", "The compound increased calcium release in transfected cells.")]
    assert contrast_passages(ps) == []


# ---------------------------------------------------------------- R5: study-arm eligibility
ARM_ELIGIBILITY = (
    "Inclusion Criteria:\n"
    "* For people with cerebellar ataxia, diagnoses of cerebellar damage from stroke, tumor or degeneration will be included.\n"
    "Healthy Volunteers\n"
    "* Able to stand for 30 seconds without upper extremity support\n"
    "* Ambulatory (including use of a cane or a walker)\n"
    "* Able to walk for 2 minutes\n"
    "Exclusion Criteria:\n"
    "* Uncontrolled hypertension\n"
)


def _arm_study(nct="NCT00000011") -> dict:
    return {"protocolSection": {
        "identificationModule": {"nctId": nct, "briefTitle": "Adapted exercise in neurodegenerative disease"},
        "statusModule": {"overallStatus": "RECRUITING", "statusVerifiedDate": "2026-09"},
        "designModule": {"studyType": "INTERVENTIONAL"},
        "eligibilityModule": {"minimumAge": "5 Years", "maximumAge": "75 Years", "sex": "ALL",
                              "healthyVolunteers": True, "eligibilityCriteria": ARM_ELIGIBILITY}}}


def test_r5_arm_headings_are_detected_and_recorded():
    f = _flatten(_arm_study())
    assert f["healthy_volunteers"] is True
    assert any("healthy" in h.lower() for h in f["eligibility_arm_headings"])


def test_r5_record_content_warns_against_transferring_arm_criteria():
    from aitx.retrieval.clinicaltrials import _content

    content = _content(_flatten(_arm_study()))
    assert "ARM/COHORT HEADINGS" in content
    assert "healthy-volunteer requirements are not patient-eligibility requirements" in content
    assert "accepts healthy volunteers: True" in content


def test_r5_trial_item_carries_an_arm_attribution_caveat():
    from aitx.retrieval.clinicaltrials import _content

    f = _flatten(_arm_study())
    L = EvidenceLedger()
    L.add(EvidenceRecord(f["nct_id"], "clinicaltrials", f["title"], "u", content=_content(f), content_scope="structured_record",
                         metadata={"overall_status": "RECRUITING", "min_age_years": 5.0, "max_age_years": 75.0, "sex": "ALL",
                                   "eligibility_arm_headings": f["eligibility_arm_headings"], "healthy_volunteers": True}))
    it = _item("t1", Section.TRIALS, [("The study is recruiting.", [f["nct_id"]], "trial_status_or_eligibility")],
               f"The patient may qualify [{f['nct_id']}].", nct_id=f["nct_id"])
    res = apply_section_rules([it], L, age_years=7.0, sex="male")
    kept = res.items[0]
    assert any("applies only to its own" in c for c in kept.criteria_unassessable)
    assert any("Healthy Volunteers" in c for c in kept.criteria_unassessable)


def test_r5_arm_heading_detector_ignores_plain_criteria_text():
    assert arm_headings("Inclusion Criteria:\n* Age 5-75 years\nExclusion Criteria:\n* Pregnancy\n") == []


def test_r5_existing_age_and_sex_gates_still_drop_ineligible_trials():
    L = EvidenceLedger()
    L.add(EvidenceRecord("NCT00000012", "clinicaltrials", "Adult study", "u", content="c", content_scope="structured_record",
                         metadata={"overall_status": "RECRUITING", "min_age_years": 18.0, "max_age_years": None, "sex": "ALL"}))
    it = _item("t2", Section.TRIALS, [("Recruiting.", ["NCT00000012"], "trial_status_or_eligibility")],
               "May qualify [NCT00000012].", nct_id="NCT00000012")
    res = apply_section_rules([it], L, age_years=7.0, sex="male")
    assert res.items == [] and any(e.rule == "trial_age_ineligible" for e in res.events)


# ---------------------------------------------------------------- R6: GoF/LoF direction not assumed
NEIGHBOUR_KEY = "PMID:90000103"
SAME_RESIDUE_KEY = "PMID:90000104"


def _mechanism_ledger() -> EvidenceLedger:
    L = EvidenceLedger()
    L.add(EvidenceRecord(NEIGHBOUR_KEY, "pubmed", "Functional study of a neighbouring suppressor-domain variant", "u",
                         content="The neighbouring p.Arg36Cys variant increased IP3 binding affinity (gain of function).",
                         content_scope="abstract"))
    L.add(EvidenceRecord(SAME_RESIDUE_KEY, "pubmed", "Functional study of other substitutions at the same residue", "u",
                         content="p.Asp34Val and p.Asp34Arg reduced agonist-induced calcium release in transfected cells.",
                         content_scope="abstract"))
    return L


@pytest.mark.parametrize("key,text", [
    (NEIGHBOUR_KEY, "This variant is a gain-of-function change that increases channel activity."),
    (SAME_RESIDUE_KEY, "This variant reduces agonist-induced calcium release."),
])
def test_r6_functional_direction_of_other_variants_is_not_assigned_to_this_variant(key, text):
    """A measured direction for a neighbouring residue (R36C) or for other substitutions at the SAME residue
    (D34V, D34R) is evidence about those variants, not about this one."""
    it = _item("m1", Section.REPURPOSING, [(text, [key], "molecular_fact")], f"{text} [{key}]")
    res = apply_verdicts([it], [_v("m1", 0, app="same_gene_other_variant_or_mechanism", quote="q", key=key),
                                _v("m1", -1, app="same_gene_other_variant_or_mechanism", quote="q", key=key)],
                         ledger=_mechanism_ledger())
    assert any(e.rule == "variant_mechanism_transfer" for e in res.events)
    assert _has(res, "supported only for a different variant")


def test_r6_directional_candidate_without_variant_specific_mechanism_is_flagged():
    """Proposing a potentiator (or an inhibitor) commits to a functional class for this variant."""
    it = _item("m2", Section.REPURPOSING,
               [("A selective potentiator increased channel activity in cultured cells.", [NEIGHBOUR_KEY], "treatment_effect_preclinical")],
               f"A selective potentiator could compensate for reduced channel function [{NEIGHBOUR_KEY}].",
               title="Selective channel potentiator")
    res = apply_verdicts([it], [_v("m2", 0, app="same_gene_other_variant_or_mechanism", quote="q", key=NEIGHBOUR_KEY),
                                _v("m2", -1, app="same_gene_other_variant_or_mechanism", quote="q", key=NEIGHBOUR_KEY)],
                         ledger=_mechanism_ledger())
    assert any(e.rule == "mechanism_direction_unestablished" for e in res.events)
    assert _has(res, "depends on the direction of the variant's functional effect")


def test_r6_directional_candidate_with_variant_specific_evidence_is_not_flagged():
    """The gate must not fire once the direction IS established for this variant - it is a dependency check,
    not a ban on mechanism-directed candidates."""
    it = _item("m3", Section.REPURPOSING,
               [("The patient's variant reduces agonist-induced calcium release.", [SAME_RESIDUE_KEY], "molecular_fact")],
               f"A selective potentiator could compensate for the reduced channel function [{SAME_RESIDUE_KEY}].",
               title="Selective channel potentiator")
    res = apply_verdicts([it], [_v("m3", 0, app="same_variant", quote="q", key=SAME_RESIDUE_KEY),
                                _v("m3", -1, app="same_variant", quote="q", key=SAME_RESIDUE_KEY)],
                         ledger=_mechanism_ledger())
    assert not any(e.rule == "mechanism_direction_unestablished" for e in res.events)


def test_r6_non_directional_supportive_item_is_not_flagged():
    it = _item("m4", Section.SUPPORTIVE, [("Physiotherapy improves mobility.", [NEIGHBOUR_KEY], "disease_fact")],
               f"Physiotherapy is standard supportive care [{NEIGHBOUR_KEY}].", title="Physiotherapy")
    res = apply_verdicts([it], [_v("m4", 0, app="other_disease_or_population", quote="q", key=NEIGHBOUR_KEY),
                                _v("m4", -1, app="other_disease_or_population", quote="q", key=NEIGHBOUR_KEY)],
                         ledger=_mechanism_ledger())
    assert not any(e.rule == "mechanism_direction_unestablished" for e in res.events)
