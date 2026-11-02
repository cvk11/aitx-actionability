# aitx-actionability

Evidence-grounded agentic system that turns a confirmed rare-disease genotype + brief clinical context into a **therapeutic actionability report** in the official format of the SAIL AI-Tx Challenge (final task, Tier 3).

> **Status (2026-11-02, milestone 1 + audit remediation):** implementation complete and tested offline; live biomedical retrieval verified against public APIs; **no paid LLM inference has been run yet**, so no genuine reports exist and `outputs/aitx_submission.json` has not been created. Hypothetical patients only — not for clinical use.

## What is verified vs. not

| Category | Meaning | Result |
|---|---|---|
| Offline tests (`pytest`) | Mock LLM + fake HTTP on a fictional gene; rules, rendering, QC, endpoint contract, 24 audit-regression tests | 77 passed |
| Live retrieval tests (`pytest -m live`) | Free public APIs (PubMed, CT.gov, ClinVar, FDA labels, Europe PMC, VV/VEP) on the public CFTR example | 4 passed |
| Deterministic scientific checks (`scripts/scientific_checks.py`) | HGVS/transcript/exon/NMD/ClinVar/ClinGen/GeneReviews/trials on the 4 official cases + CFTR control | see `docs/SCIENTIFIC_CHECKS.md` |
| Genuine LLM clinical inference | Claude research, interpretation, verification | **not run** (requires authorization + `ANTHROPIC_API_KEY`) |

Passing software tests does not establish scientific or clinical validity.

## Documents
- `competition_spec.md` — verified rules and required schema (English)
- `AI_TX_OFFICIAL_RULES_AUDIT.md` — rule-by-rule audit with sources and open questions (Turkish)
- `GITHUB_REPOSITORY_AUDIT.md` — audit of the author's previous study repository and reuse strategy (Turkish)
- `docs/ARCHITECTURE_DECISIONS.md` — comparison with the organizers' tutorials and architecture reassessment (Turkish)
- `docs/RETRIEVAL_ARCHITECTURE.md` — biomedical sources, evidence ledger, provenance, licensing
- `docs/SCIENTIFIC_CHECKS.md` — deterministic scientific checks and open uncertainties (Turkish)
- `SYSTEM_DESCRIPTION.md` — draft for the submission form
- `COST_AND_LIMITATIONS.md` — cost model, spending limits, prepared first run, limitations
- `deployment/README.md` — Hugging Face Space
- `AGENTS.md`, `CODEX_REVIEW_GUIDE.md` — instructions for independent code review
- `REMEDIATION_REPORT.md` — fixes for the independent audit of commit `9be0d2c` (B1–B9 + medium findings)

## Installation
Python ≥ 3.11 (developed on 3.13).
```bash
python -m venv .venv
```
```bash
source .venv/bin/activate
```
```bash
pip install -r requirements.txt && pip install -e .
```
Build the GeneReviews snapshot (official NCBI archive, ~640 MB streamed, ~1 min; only chapter XML is kept, not committed):
```bash
python scripts/build_genereviews_index.py
```
Configuration: copy `.env.example` to `.env` and fill keys locally (never commit `.env`). Optional free `NCBI_API_KEY`/`NCBI_EMAIL` raise NCBI rate limits.

## Usage
Offline tests (no network, no cost):
```bash
pytest
```
Live free-API smoke tests:
```bash
pytest -m live
```
Free deterministic evidence pre-scan of the official cases:
```bash
python scripts/bootstrap_evidence.py
```
Offline cost estimate:
```bash
python scripts/estimate_cost.py
```
**Paid** (only after authorization): one patient, all patients, validation:
```bash
python scripts/run_one.py --case-id AITX-40002 --config B_full
```
```bash
python scripts/run_all.py --config B_full
```
```bash
python scripts/validate_submission.py
```
Any other valid input file: `python scripts/run_one.py --input my_case.json`.

## Pipeline (one generalizable path for every patient)
```
validate input → deterministic bootstrap (RefSeq exon/NMD model, VariantValidator, VEP, ClinVar exact match,
ClinGen, gnomAD, UniProt, GeneReviews, trials) → A interpretation (gene- vs variant-level mechanism)
→ B/C three parallel agentic researchers (Tier 1/2/3) → deterministic tier & citation rules
→ D verification (PMID existence, live trial status, LLM claim/statement entailment) → [E optional review]
→ bounded revision → F synthesis → deterministic Markdown render → structural QC → {id, report_markdown}
```
Key properties: models cite only evidence-ledger keys retrieved in the same run; links come from the ledger (model-written links stripped; non-ledger links reject the report); verification fails closed (only fully verified statements are rendered); partial or unverified reports cannot enter the submission (`src/aitx/submission.py`); "searched, not found" vs "retrieval failed" vs confirmed negative are kept distinct; no hardcoded case answers (tested); full audit trail per run.

## Layout
```
src/aitx/            config, schemas, template, http, ledger, variant_model, clinical, rules, render, qc, pipeline, factory, robustness
src/aitx/retrieval/  ncbi, clinicaltrials, variant, drugs, europepmc, genereviews, web, registry (tool specs)
src/aitx/llm/        anthropic_client (Claude), gemini_client (optional), mock, base (cost/budget)
src/aitx/agents/     interpreter (A), researcher (B/C), verifier (D), reviewer (E), synthesizer (F), context
prompts/             version-controlled templates (SHA-256 recorded per run)
data/official*/      verbatim official template and cases + SHA-256 provenance
scripts/             run_one, run_all, validate_submission, bootstrap_evidence, scientific_checks, estimate_cost,
                     robustness, compare_configs, build_genereviews_index, prepare_space
deployment/          Hugging Face Space app (/actionability_report), requirements, Space card
tests/               offline unit/integration/endpoint tests + live smoke tests
```

## Models
Primary and verifier: `claude-opus-5-5` (exact identifier; aliases such as "latest" are rejected by config). Optional reviewer: `gemini-4-argon` (stable code per Google's model page, 2026-11-01). Arm C (independent OpenAI research) is a placeholder only.

## Privacy and security
No API keys in code or history (see `CODEX_REVIEW_GUIDE.md` for the scan procedure). In deployment mode (forced on a Space), no disk cache, no artifacts, no input/output logging; Gradio analytics, run history, monitoring and flagging are disabled.

## License / data
Code: to be decided by the owner (no license file yet). GeneReviews content © University of Washington — not redistributed; rebuilt from NCBI's official archive. Official challenge files © the organizers, included verbatim for reproducibility.
