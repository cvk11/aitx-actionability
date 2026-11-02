# Independent code review guide (for OpenAI Codex or human reviewers)

Goal: find defects that could produce **incorrect or misleading clinical statements**, untraceable citations, rule violations, or privacy/security problems. Prioritise correctness of evidence handling over style. Read `AGENTS.md` first (no paid API calls, no hardcoding).

## 0b. Re-review of the second remediation (commit after `36d8d3e`)
Read `SCIENTIFIC_REMEDIATION_REPORT.md`. Priorities: (a) can a factual claim reach the report without a supported verdict whose quote is found in a cited source (`verifier.ground_verdict`, `rules.apply_verdicts`, `compose_from_verified_claims`)? (b) can evidence about another variant/gene/intervention be labelled as precedent for this patient? (c) are valid candidates lost anywhere (handoffs, salvage, tier moves)? (d) passage selection (`evidence.select_passages`) for very long sources and tables.

## 0. Targeted re-review of the remediation (commit after `9be0d2c`)
Read `REMEDIATION_REPORT.md`. For each B1–B9 row, check the named functions and the regression test in `tests/test_audit_regressions.py`. Priorities: (a) can any path render an item whose full statement lacks a `supported`/`not_checkable` decision (`pipeline.py` final gate, `_verify`, exception branch)? (b) can synthesis text bypass `verify_synthesis`? (c) is `CostTracker.reserve` used before every model request, including Gemini and retries? (d) is `assemble_submission` the only writer of `outputs/aitx_submission.json`? (e) ClinVar SPDI/VCF normalisation (`_trim`) for indels in repeats, MNVs and dups.

## 1. Biomedical API retrieval — `src/aitx/retrieval/*`, `src/aitx/http.py`
- Are response shapes parsed defensively (missing keys, empty lists, HTTP 4xx/5xx, HTML instead of JSON)?
- `ncbi.clinvar_lookup`: exact-match logic `_clinvar_title_matches` / `_norm_c` — false negatives (other nomenclature, `dupT` vs `dup`, protein-only titles) and false positives (same cDNA on another transcript).
- `clinicaltrials._flatten`/`_age_to_years`: age units, missing min/max age, `sex` values; `OPEN_STATUSES` completeness.
- `variant.uniprot_entry`: `features_at_position` overlap logic; field list validity.
- `variant.gnomad_constraint`: GraphQL errors with HTTP 200; caching of error responses.
- `genereviews`: section extraction (nested sections, tables), retired chapters, anchors used in URLs.
- `web.fetch_url`: allow-list bypass via subdomains/redirects; bot-challenge detection.
- `http.py`: retry policy, rate limits, cache keys exclude credentials, no disk cache in deployment mode.

## 2. Molecular variant reasoning — `src/aitx/variant_model.py`
- HGVS regexes: intronic offsets, UTR (`c.-`, `c.*`), `delins`, inversions, ranges; protein forms (`fs*`, `Ter`, `ext`, `Met1?`, `=`).
- `TranscriptModel.c_to_tx` for negative positions (5'UTR has no c.0), `exon_coding_length`, `exon_c_bounds`.
- Exon-skipping frame flag (only fully coding internal exons), first/last exon handling.
- Cross-check: computed exon vs VEP exon flag in `annotate_variant`.

## 3. NMD prediction — `analyse_variant`
- PTC position from `fsTerN` (start + N − 1) and the indel shift back to reference coordinates.
- Rules: last exon → escape; within ~50 nt of the last exon–exon junction → escape; otherwise NMD; start-proximal caveat. Check boundary off-by-one errors and PTCs beyond the annotated stop.
- Confirm all NMD outputs are labelled as rule-based predictions in ledger text.

## 4. Citation verification — `src/aitx/rules.py`, `src/aitx/agents/verifier.py`
- Every cited key must exist in the ledger; unknown keys stripped from all text fields; retracted sources removed.
- `deterministic_checks`: PMID existence via esummary, abstract loading, **live** NCT status (cache bypass).
- `llm_verify`: batching, mapping of verdicts to `(item_id, claim_index)`, the full-statement pseudo-claim (`claim_index = -1`), unverified claims when the model omits verdicts.
- `apply_verdicts`: when items are dropped vs flagged; `REQUIRED_KINDS` per section.

## 5. Evidence classification
- `_is_human_record` heuristic (publication types + words) used for the off-label rule; risk of animal studies mentioning "patients".
- Targeted-therapy rule requires label/GeneReviews source and approval/guideline evidence level.
- `render.EVIDENCE_LABEL` and calibration wording.

## 6. Clinical-trial processing
- Drop rules for non-recruiting, age- or sex-incompatible trials (`apply_section_rules`); unknown age handling.
- "Eligible" phrasing detection; criteria met / unassessable / exclusions rendering.
- Live status re-check updating ledger metadata before the second rules pass.

## 7. Error handling
- No silent defaults (contrast with the author's earlier study code that coded failed calls as score 0).
- Tool errors returned to the model with `is_error` and logged in the ledger (`retrieval_status`: with_results / empty / failed).
- `BudgetExceeded`, `ModelRefusal`, `StructuredOutputError` propagation; partial reports flagged in `unresolved`.
- Parallel researchers (ThreadPoolExecutor): exceptions, shared ledger thread-safety.

## 8. Reproducibility
- Pinned model ids; `config.py` rejects aliases; served model and fallback usage recorded.
- Prompt SHA-256 manifest, software versions, GeneReviews archive hash in every audit.
- Determinism of rendering and rules given identical items.

## 9. Security
- Secret handling: keys only from environment; `public_dict()` redacts; no keys in cache keys/URLs; `.env` ignored.
- Deployment: no payload logging, no persistence (`deployment/app.py`), Gradio options disabled; input validation (`PatientCase`).
- Prompt injection: retrieved web/paper text enters prompts — check that tools cannot be induced to fetch arbitrary domains or that injected text cannot add citations not in the ledger.
- Suggested secret scan:
```bash
git log -p | grep -nE "sk-ant-|AIza[0-9A-Za-z_-]{20}|ghp_[0-9A-Za-z]{20}|hf_[0-9A-Za-z]{20}|sk-[A-Za-z0-9]{32}" || echo "no secrets found"
```

## 10. Competition compliance
- Headings derived from `data/official/aitxchallenge_report_template.md`; exactly one H1; no placeholder text; no repeated required headings (`qc.check_report_structure`).
- Submission: JSON array, exactly the four ids, only `id`/`report_markdown`, ≤5 MB, mock output rejected (`qc.validate_submission`).
- No manual editing path; no case-specific code (test); endpoint contract (`tests/test_deployment.py`).

## Known open items (do not "fix" by weakening rules)
- Arm C (OpenAI research) is a placeholder by design.
- Verifier accuracy and LLM interpretation quality are unmeasured until authorized real runs.
