"""Scripted stage outputs for the offline end-to-end test (fictional GENEX case).

The handlers call real tools through the ToolBox (backed by the fake HTTP
transport), so ledger population, rules, verification and rendering are all
exercised exactly as in a real run.
"""

from __future__ import annotations

from aitx.schemas import (Adjudication, AdjudicationDecision, ClaimVerdict, Interpretation, OutcomeMeasure, ReviewIssue, ReviewReport, RevisionPlan, ItemRevision,
                          SectionResearchOutput, SummaryLine, Synthesis, VerificationBatch, VariantInterpretation)

GR_TX = "GENEREVIEWS:NBK9999#Treatment_of_Manifestations"
GR_AVOID = "GENEREVIEWS:NBK9999#AgentsCircumstances_to_Avoid"


def item(item_id, section, title, statement, claims, level="guideline_or_expert_review", **kw):
    base = dict(item_id=item_id, section=section, title=title, statement=statement, conditional_action="", evidence_level=level, priority=1,
                claims=[{"text": t, "citations": c, "kind": k} for t, c, k in claims], missing_information=[])
    base.update(kw)
    return base


def interpret(ctx):
    return Interpretation(
        diagnoses=[{"disease_name": "GENEX-related epilepsy", "inheritance": "autosomal dominant", "variant_hgvs": ["NM_999999.1:c.100G>A"],
                    "genotype_consistency": "consistent"}],
        disease_name="GENEX-related epilepsy", disease_identifiers=[], inheritance="autosomal dominant", genotype_consistency="consistent",
        variants=[VariantInterpretation(gene="GENEX", hgvs_c="c.100G>A", hgvs_p="p.Asp34Asn", consequence="missense", variant_specific_evidence="ClinVar pathogenic [CLINVAR:VCV000000123]",
                                        gene_level_mechanism="loss of function", variant_mechanism_assessment="uncertain", mechanism_label="uncertain",
                                        mechanism_confidence="uncertain", citations=["CLINVAR:VCV000000123"])],
        phenotype_summary="seizures", patient_age_years=99.0, patient_sex="male",  # deliberately wrong: parser must override
        key_missing_clinical_information=["seizure frequency"], research_plan=["x"], citations=[])


def established(ctx):
    tb = ctx["toolbox"]
    tb.execute("genereviews_read", {"chapter": "genex", "sections": []})
    tb.execute("fda_label_search", {"drug": "drug Q", "indication_text": "", "sections": []})
    return SectionResearchOutput(candidate_handoffs=[], items=[
        item("sup-seizures", "supportive_management", "Seizures", f"Treat seizures with standard anti-seizure medications [{GR_TX}].",
             [("Seizures are treated with standard ASMs.", [GR_TX], "disease_fact"), ("ASM X is first line.", [GR_TX], "treatment_effect_human")]),
        item("ci-drugz", "contraindications_precautions", "Drug Z", f"Avoid drug Z [{GR_AVOID}].", [("Drug Z worsened seizures.", [GR_AVOID], "safety_or_contraindication")]),
        item("tgt-q", "existing_targeted_therapies", "Drug Q", "Drug Q reduced seizures in a GENEX patient [PMID:11111111].",
             [("Drug Q reduced seizures by 50% in one patient.", ["PMID:11111111"], "treatment_effect_human")], level="human_same_gene"),
    ], searched_but_not_found=["No FDA label indicates a GENEX-directed therapy."], open_questions=[])


def emerging(ctx):
    tb = ctx["toolbox"]
    tb.execute("pubmed_fetch_abstracts", {"pmids": ["11111111", "22222222"]})
    return SectionResearchOutput(candidate_handoffs=[], items=[
        item("trial-open", "clinical_trial_eligibility", "Drug Q trial", "The patient may qualify (genetic DEE, age 5) [NCT00000001].",
             [("NCT00000001 is recruiting children 2-17 years.", ["NCT00000001"], "trial_status_or_eligibility")], level="trial_registry_record",
             nct_id="NCT00000001", criteria_met=["age 2-17"], criteria_unassessable=["seizure frequency"]),
        item("trial-closed", "clinical_trial_eligibility", "Old study", "Old study [NCT00000002].",
             [("Old study.", ["NCT00000002"], "trial_status_or_eligibility")], level="trial_registry_record", nct_id="NCT00000002"),
        item("off-r", "off_label_expanded_access", "Compound R", "Compound R rescued Genex knockout mice [PMID:22222222].",
             [("Compound R restored release in mice.", ["PMID:22222222"], "treatment_effect_preclinical")], level="preclinical_same_gene"),
    ], searched_but_not_found=[], open_questions=[])


def hypothesized(ctx):
    return SectionResearchOutput(candidate_handoffs=[], items=[
        item("aso-allele", "bespoke_therapies", "Allele-selective ASO", "Allele-selective knockdown is uncertain [COMPUTED:GENEX:c.100G>A].",
             [("The variant is missense in exon 2.", ["COMPUTED:GENEX:c.100G>A"], "molecular_fact")], level="mechanistic_inference", bespoke_verdict="UNCERTAIN"),
        item("fake-cite", "drug_repurposing", "Compound F", "Compound F works [PMID:99999999].",
             [("Compound F works.", ["PMID:99999999"], "treatment_effect_preclinical")], level="preclinical_related"),
    ], searched_but_not_found=[], open_questions=[])


def verify(ctx):
    import json, re
    # parse the claims embedded in the system prompt
    m = re.search(r"Claims to verify \(JSON\):\n(\[.*?\])\n\nSource texts:", ctx["system"], re.S)
    claims = json.loads(m.group(1))
    out = []
    for c in claims:
        verdict = "supported"
        if c["item_id"] == "sup-seizures" and c["claim_index"] == 1:
            verdict = "unsupported"
        key, quote = quote_from_packet(ctx["system"], c)
        if not c["citations"] and verdict == "supported":
            verdict = "not_checkable"
        out.append(ClaimVerdict(item_id=c["item_id"], claim_index=c["claim_index"], verdict=verdict, evidence_type="direct_human_same_gene",
                                overgeneralization="", abstract_only=False, supporting_quote=quote, quote_source_key=key,
                                applicability="same_gene_same_mechanism", intervention_matches=True, rationale="mock", suggested_revision=""))
    return VerificationBatch(verdicts=out)


def quote_from_packet(system: str, claim: dict) -> tuple[str, str]:
    """Copy a real passage fragment from the evidence packet shown for this claim (like a faithful verifier)."""
    import re

    m = re.search(rf"##### Evidence for item_id={re.escape(claim['item_id'])} claim_index={claim['claim_index']}\n(.*?)(?=\n##### Evidence for|\Z)",
                  system, re.S)
    if not m:
        return "", ""
    for block in re.split(r"\n(?==== \[)", m.group(1)):
        km = re.match(r"=== \[([^\]]+)\]", block)
        pm = re.search(r"\n\[[^\]]+\] (.{20,})", block)
        if km and pm:
            return km.group(1), pm.group(1)[:60]
    return "", ""


def revise(ctx):
    return RevisionPlan(revisions=[ItemRevision(item_id="fake-cite", action="drop", reason="no valid citation"),
                                   ItemRevision(item_id="sup-seizures", action="revise", revised_statement=f"Treat seizures with standard anti-seizure medications [{GR_TX}].",
                                                reason="removed unsupported first-line claim")])


def synthesize(ctx):
    return Synthesis(
        summary_lines=[SummaryLine(tier=1, text=f"Standard anti-seizure medications [{GR_TX}]", item_ids=["sup-seizures"]),
                       SummaryLine(tier=2, text="Assess eligibility for NCT00000001 [NCT00000001]", item_ids=["trial-open"]),
                       SummaryLine(tier=3, text="Allele-selective ASO (uncertain) [PMID:99999999]", item_ids=["aso-allele"])],
        missing_information=["Seizure frequency would change the recommendations: if >=4/month, then trial screening; otherwise ASM optimisation."],
        duplicate_item_ids_to_drop=[],
        outcome_measures=[OutcomeMeasure(rank=1, item_id="aso-allele", action_name="Allele-selective ASO", rank_rationale="only option",
                                         safety_metrics="CSF cell count", efficacy_metrics="seizure diary", preclinical_testing="iPSC neurons", citations=["PMID:99999999"])],
        calibration_notes=[])


def review_issues(ctx):
    return ReviewReport(issues=[ReviewIssue(issue_type="missed_contraindication", target_item_id="", description="Consider drug W", suggested_fix="add",
                                            cited_keys=[], severity="low")])


def adjudicate(ctx):
    return Adjudication(decisions=[AdjudicationDecision(issue_index=0, decision="reject", rationale="no evidence", research_query="")])


HANDLERS = {
    "A_interpret": interpret, "B_established": established, "B_emerging": emerging, "C_hypothesized": hypothesized,
    "D_verify": verify, "F_revise": revise, "F_synthesize": synthesize, "E_review": review_issues, "E_adjudicate": adjudicate,
}


def baseline(ctx):
    a, b = established(ctx), emerging(ctx)
    return SectionResearchOutput(candidate_handoffs=[], items=a.items + b.items, searched_but_not_found=[], open_questions=[])


HANDLERS_BASELINE = {"A_baseline": baseline, "F_synthesize": synthesize}
