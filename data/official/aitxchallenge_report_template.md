# Therapeutic Actionability Report for Patient AITX-XXXXX

<!--
HOW TO USE THIS TEMPLATE

Every section below is required and must appear in this order. Replace the
italicized guidance and the [BRACKETED PLACEHOLDERS] with your own content, and
delete this comment block. Subheadings, bullets, links, and references beyond
this template are encouraged.

The finished report is submitted as the `report_markdown` string of the output
JSON object:

  {
    "id": "AITX-XXXXX",
    "report_markdown": "# Therapeutic Actionability Report for Patient AITX-XXXXX\n\n## Summary..."
  }

Three requirements apply to EVERY section of the report:

1. Cite a verifiable source for every claim, or show your reasoning.
   A source need not be a paper! A database record, drug label, guideline, or
   trial registry entry is fine, given a URL or accession. If no prior report(s)
   exist, cite the underlying facts and lay out the reasoning that gets you to
   the claim. Never cite a reference that doesn't support the claim.

2. State conditional actions.
   If missing patient information would change a recommendation, say so:
   "If seizures occur more often than once every three days, then XXX.
   Otherwise, YYY."

3. Don't repeat across sections.
   Put each action in the single best-fitting section. An existing ASO approved
   for the patient's specific variant and presentation belongs under Existing
   Targeted Therapies, not again under Bespoke Therapies. However, a feasible ASO
   that has not yet been developed or tested belongs under Bespoke Therapies.
-->

## Summary

*The highest-value actions for this patient, ordered by tier, including conditional actions depending on patient-specific clinical variables. If key information that would alter therapeutic options is missing, note it explicitly here.*

- **Tier 1:** [HIGHEST-VALUE ESTABLISHED ACTION, STATED AS SOMETHING THE CARE TEAM CAN DO]
- **Tier 2:** [HIGHEST-VALUE EMERGING ACTION, E.G. A SPECIFIC RECRUITING TRIAL]
- **Tier 3:** [HIGHEST-VALUE HYPOTHESIZED ACTION, WITH ITS UNCERTAINTY STATED]

**Missing information:** [UNKNOWN CLINICAL VARIABLE] would change the recommendations above: if [CONDITION], then [ACTION A]; otherwise [ACTION B].

---

## Tier 1: Established Actions

### Existing Targeted Therapies

*Approved or guideline-supported treatments directed at the underlying molecular defect or disease mechanism.*

- **[THERAPY NAME AND REGIMEN]:** [HOW IT ADDRESSES THIS PATIENT'S MOLECULAR DEFECT, AND WHY THEIR VARIANT AND PRESENTATION QUALIFY]. [ANY CONDITIONAL QUALIFIER]. Source: [DRUG LABEL, GUIDELINE, OR DATABASE RECORD](URL_OR_ACCESSION)

### Supportive Management

*Standard-of-care pharmacologic or procedural management of symptoms and complications rather than the underlying defect. Surveillance is currently excluded.*

- **[SYMPTOM OR COMPLICATION]:** [STANDARD-OF-CARE MANAGEMENT FOR IT IN THIS DISEASE]. Source: [GUIDELINE OR REVIEW](URL_OR_ACCESSION)

### Contraindications & Precautions

*Agents, doses, or procedures to avoid or modify because of the variant or disease, including anesthesia risks and clinically relevant medication interactions.*

- **[AGENT, DOSE, OR PROCEDURE TO AVOID OR MODIFY]:** [WHY THE VARIANT OR DISEASE MAKES IT RISKY, AND WHAT TO DO INSTEAD]. Source: [DRUG LABEL, PHARMGKB/CLINPGX RECORD, OR GUIDELINE](URL_OR_ACCESSION)

---

## Tier 2: Emerging Actions

### Clinical Trial Eligibility

*Actively recruiting clinical trials for which the patient may qualify, with potential eligibility assessed against the available clinical and molecular criteria.*

- **[NCT NUMBER: TRIAL TITLE](https://clinicaltrials.gov/study/NCT00000000)**: [WHICH INCLUSION CRITERIA THE PATIENT MEETS ON THE AVAILABLE CLINICAL AND MOLECULAR DATA, WHICH CRITERIA CANNOT BE ASSESSED FROM THE CASE, AND ANY EXCLUSION THAT WOULD DISQUALIFY THEM].

### Off-label or Expanded-Access Therapies

*Existing therapies that could potentially be accessed outside standard approval for this indication, **and for which human treatment precedent exists**, such as at least one published report of the therapy administered to a patient with the same gene, disease, or variant context.*

- **[THERAPY NAME]:** [MECHANISTIC RATIONALE FOR THIS PATIENT]. Human treatment precedent: [PATIENT(S) WITH THE SAME GENE, DISEASE, OR VARIANT CONTEXT WHO RECEIVED IT, AND THE OUTCOME]. Source: [PUBLISHED REPORT](URL_OR_ACCESSION)

---

## Tier 3: Hypothesized Actions

### Bespoke Therapies

*Assessment of the variant's amenability to specific DNA- and RNA-directed therapeutic strategies (e.g., ASOs, gene therapies) not already identified as established or emerging, with a verdict and rationale for each.*

- **[STRATEGY, E.G. SPLICE-MODULATING ASO]:** **Verdict:** [AMENABLE / NOT AMENABLE / UNCERTAIN]. [RATIONALE TIED TO THE SPECIFIC VARIANT: ITS CONSEQUENCE, POSITION, AND WHAT THE STRATEGY WOULD NEED IN ORDER TO WORK]. Source: [VARIANT RECORD OR SUPPORTING REPORT](URL_OR_ACCESSION)

### Drug Repurposing

*Existing approved or investigational compounds **for which no such human treatment precedent exists**, but with a mechanistic rationale linking the intervention to the molecular defect.*

- **[COMPOUND NAME]:** [MECHANISTIC RATIONALE LINKING THE COMPOUND TO THIS PATIENT'S MOLECULAR DEFECT, AND THE EVIDENCE (IN VITRO, ANIMAL MODEL, OR RELATED-PATHWAY DATA) BEHIND IT]. No human treatment precedent exists in this context. Source: [SUPPORTING EVIDENCE](URL_OR_ACCESSION)

### Outcome Measures & Ranking

*If any hypothesized actions are recommended: required preclinical testing, relevant modality-specific safety considerations, and objective and age-appropriate metrics for evaluating safety and efficacy. If multiple hypothesized actions are recommended, rank them with a brief rationale. A full trial protocol is not expected.*

- **[RANK]. [HYPOTHESIZED ACTION FROM ABOVE]:** [WHY IT RANKS HERE].
  - **Safety metrics:** [OBJECTIVE, AGE-APPROPRIATE MEASURES FOR MONITORING SAFETY, PLUS ANY MODALITY-SPECIFIC SAFETY CONSIDERATIONS].
  - **Efficacy metrics:** [OBJECTIVE, AGE-APPROPRIATE MEASURES FOR ASSESSING EFFICACY].
  - **Preclinical testing:** [RELEVANT PRECLINICAL WORK NEEDED BEFORE TREATMENT]. Source: [SUPPORTING EVIDENCE](URL_OR_ACCESSION)
