# System description (draft for the submission form — Tier 3)

> Draft text. Final numbers (models served, cost, run date) must be filled from `outputs/run_manifest.json` after the real run. No reports have been generated yet.

**Name:** aitx-actionability — evidence-ledger agentic pipeline for therapeutic actionability reports.

**Tier:** 3 (Unrestricted). **Base model:** Anthropic Claude Opus 5.5, exact model identifier `claude-opus-5-5` (adaptive thinking, effort `high`; verifier effort `medium`). Server-side refusal fallback (`fallbacks: "default"`) is enabled; the model that actually served every call is recorded, and any fallback is disclosed in the run manifest. Optional independent reviewer: Google Gemini `gemini-4-argon` (off by default; not used unless stated).

**Architecture.** One generalizable pipeline for every patient; no case-specific code, prompts or answers (enforced by an automated test).
1. *Deterministic molecular bootstrap (no LLM):* RefSeq transcript model from the NCBI feature table (exon/intron position, exon coding length and reading frame, PTC position, rule-based NMD prediction), VariantValidator nomenclature check, Ensembl VEP cross-check, ClinVar exact-HGVS match, ClinGen validity/dosage, gnomAD constraint, UniProt features at the residue, GeneReviews chapters, open trials.
2. *Interpretation agent:* disease, inheritance/zygosity fit, gene-level vs variant-specific mechanism with an explicit confidence label, missing clinical information, research plan.
3. *Three parallel agentic researchers* (Tier 1 / Tier 2 / Tier 3 sections) iterating over live tools: PubMed, Europe PMC (open-access full text), ClinVar, ClinicalTrials.gov API v2, openFDA/DailyMed labels, GeneReviews (official NCBI archive snapshot, dated), ClinGen, gnomAD, UniProt, ClinPGx, allow-listed authoritative pages. Every retrieved record enters an evidence ledger; models may cite only ledger keys, and all links are rendered from the ledger.
4. *Deterministic rules* implementing the template's tier definitions: citations must exist in the ledger; retracted sources removed; trials must be live-recruiting and compatible with age/sex; off-label requires published human precedent (else moved to Drug Repurposing); "existing targeted" requires label or guideline support.
5. *Independent verification:* PMID existence checks, live re-check of trial status, and a separate Claude invocation that judges every atomic claim and every full item statement against the retrieved source text (supported / partially / unsupported / contradicted; evidence type; overgeneralisation). Unsupported content is removed or qualified; a bounded revision loop fixes specific verified problems; statements still unsupported are dropped.
6. *Synthesis:* tier summary, conditional actions for missing data, Tier-3 outcome measures and ranking. A deterministic renderer writes the official template; structural QC mirrors the upload checks.

**Reproducibility.** Pinned model identifier and dependency versions; SHA-256 of every prompt, the official template/cases and the GeneReviews archive recorded per run; full audit trail (queries with timestamps, ledger, verdicts, rule events, cost). The Hugging Face Space runs the identical code at `/actionability_report` with no logging or storage of evaluation inputs/outputs. LLM outputs are not bit-for-bit deterministic and live records (e.g., trial status) can change between runs.

**Human editing:** none. Reports are produced end-to-end by the pipeline.
