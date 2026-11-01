{{COMMON_RULES}}

## Your role: Prioritisation, Summary and Outcome Measures (stage F)

The items below have passed claim-level verification (unsupported claims were removed or qualified). Produce:

1. `summary_lines`: exactly one line per tier (1, 2, 3) stating the single highest-value action as something the care team can do, with its uncertainty for Tier 3. Use inline ledger citations already used by the referenced items. If a tier has no qualifying item, say so honestly (e.g., "No actively recruiting interventional trial was identified; consider registry enrolment ...") rather than inventing one.
2. `missing_information`: 1–4 key unknown clinical variables that would change the recommendations, each written as "<variable> would change the recommendations: if <condition>, then <action A>; otherwise <action B>."
3. `duplicate_item_ids_to_drop`: items that repeat an action already placed in a better-fitting section.
4. `outcome_measures`: for each recommended Tier-3 action (bespoke items with verdict AMENABLE or UNCERTAIN, and repurposing items), in rank order: why it ranks there (evidence strength, feasibility, safety, timeline), objective age-appropriate safety metrics with modality-specific considerations (e.g., intrathecal ASO: CSF cell count/protein, platelets, renal function, injection-procedure risks; AAV: anti-capsid/transgene immunity, liver enzymes, dorsal-root-ganglion toxicity; small molecules: label-specified monitoring), objective age-appropriate efficacy metrics (validated scales or biomarkers appropriate for the disease and the patient's age/development), and the preclinical testing needed first (e.g., patient iPSC-derived cells, allele-specificity assays, animal models). Cite sources where available; reasoning is acceptable where labelled.
5. `calibration_notes`: any overall caveats (e.g., mechanism uncertain; guideline predates newer data).

Patient and interpretation context:
{{CONTEXT}}

Verified items (JSON):
{{ITEMS}}

Evidence digest:
{{EVIDENCE}}
