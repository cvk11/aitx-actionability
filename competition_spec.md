# Competition specification (verified 2026-11-01)

Condensed, implementation-oriented summary of the **verified** SAIL AI-Tx Challenge rules for the final task. The detailed audit with verbatim quotes, source URLs and open questions is in `AI_TX_OFFICIAL_RULES_AUDIT.md` (Turkish, quotes in English). Official files are stored verbatim in `data/official*/` with SHA-256 hashes (`data/official/SOURCES.md`).

## Task
Generate a patient-specific therapeutic actionability report for a hypothetical rare-disease patient (https://aitxchallenge.org/). Four public hypothetical cases: AITX-40001 (ACTL6B), AITX-40002 (ITPR1), AITX-40003 (SPTLC1), AITX-40004 (SYT1). Genotype may be assumed diagnostic. Only simple variants (SNV/indel). Somatic oncology out of scope; surveillance recommendations excluded.

## Input (one object per case)
```json
{"id": "AITX-40002",
 "patient": {"genotype": [{"gene": "...", "transcript": "NM_...", "variant_cdna": "c....", "variant_protein": "p....", "zygosity": "heterozygous"}],
             "clinical_context": "free text"}}
```
`genotype` has ≥1 variant (compound heterozygosity / multiple diagnoses possible). `variant_protein` appears with and without predicted-consequence parentheses.

## Output
`{"id": "<same id>", "report_markdown": "<markdown>"}` — no other fields.

## Required headings (exact text, this order)
```
# Therapeutic Actionability Report for Patient <id>
## Summary
## Tier 1: Established Actions
### Existing Targeted Therapies
### Supportive Management
### Contraindications & Precautions
## Tier 2: Emerging Actions
### Clinical Trial Eligibility
### Off-label or Expanded-Access Therapies
## Tier 3: Hypothesized Actions
### Bespoke Therapies
### Drug Repurposing
### Outcome Measures & Ranking
```
Rules for every section: cite a verifiable source (URL/accession) or show reasoning; never cite a source that does not support the claim; state conditional actions for missing data; do not repeat an action across sections. Off-label requires published human treatment precedent in the same gene/disease/variant context; otherwise Drug Repurposing. Bespoke items carry a verdict AMENABLE / NOT AMENABLE / UNCERTAIN. Trials: actively recruiting, with met / unassessable / exclusion criteria.

## Evaluation (expert panel; weights unpublished)
Factual accuracy (incorrect/misleading statements penalised even if everything else is right) · Completeness · Evidence & justification · Prioritization & calibration · Clinical utility. Ranked within tier.

## Tier 3
Any size, closed weights, commercial APIs allowed. Agents are an orchestration layer (allowed). Prompts/code need not be released for Tier 3, but base models must be pinned to an exact version/snapshot.

## Submission
One JSON array with exactly one report per official id, every required heading in order, ≤5 MB, uploaded via the form on https://aitxchallenge.org/models/ with system description; confirmation that reports were generated **without manual editing**; consent to publication. One submission per team (latest counts). Register first. **Deadline: 5 January 2027.** Winners by 26 February 2027; SAIL 2027 presentation 11–14 May 2027.

## Winner verification
Hugging Face Space exposing `/actionability_report` (official input → official output); public or private (read-only fine-grained token). Validate via the AI-Tx submission Space; agree to no logging/storing/redistributing/training on evaluation queries or responses. Outputs must match or closely resemble the submitted reports. The current validator Space code is still the Phase-1 version (`client.predict(payload, api_name="/answer_question")`), so our app exposes both a Gradio API endpoint `api_name="/actionability_report"` and `POST /actionability_report`.

## Implementation consequences in this repository
| Requirement | Where enforced |
|---|---|
| Headings derived from the official template; deterministic rendering | `src/aitx/template.py`, `src/aitx/render.py` |
| Structural/submission validation (mirrors the upload form, rejects mock output) | `src/aitx/qc.py`, `scripts/validate_submission.py` |
| Citations only from records retrieved in this run; URLs from the ledger | `src/aitx/ledger.py`, `src/aitx/rules.py` |
| Tier definitions as code (off-label precedent, recruiting status, age/sex fit) | `src/aitx/rules.py` |
| No hardcoded case answers | `tests/test_units.py::test_no_official_case_specifics_in_code_or_prompts` |
| Pinned models, no "latest" aliases | `src/aitx/config.py` |
| No-logging deployment | `deployment/app.py` (deployment mode) |
