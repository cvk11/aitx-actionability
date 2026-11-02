# Retrieval-failure remediation: PubMed / E-utilities (after commit `03f2bde`)

This work was done before the first paid inference. **No paid model call was made.** The official case files are unchanged, and no publication was inserted by hand into any ledger.

## Defect (found while exporting the free AITX-40002 preflight)
The mandatory coverage query `human_treatment_reports` received an **HTTP 200** response whose body was
`esearchresult.ERROR = "Search Backend failed: Pubmed 2.0 search API: HTTP request returned 502 status."`.

`ncbi.pubmed_search` read `int(res.get("count", 0))`, so the response became **0 hits, no error, status "empty"**. The disk cache then kept that response for 7 days.

A fresh search returned 59 hits, including PMID 38844245 and PMID 37964426. Neither appeared in the dossier.

The behaviour was reproduced on a worktree of `03f2bde`: `total_count=0`, ledger error `''`, `retrieval_status = empty 1 / failed 0`, and 1 file written to the cache.

The same pattern existed in four other places:
- ClinVar esearch/esummary: a failure turned into "no ClinVar record found".
- NCBI Gene esearch: a failure turned into `found: False`.
- `pmid_exists`: a failure turned into "all PMIDs missing", which would drop cited items.
- `pubmed_fetch_abstracts`: a failure turned into `not_found`.

Two related gaps were also found:
- `render._negative_statement` hid a failed query whenever another query against the same source had succeeded.
- Europe PMC notation hits such as "D34N" were listed as variant literature without any check of gene context.

## Fixes
| # | Requirement | Change |
|---|---|---|
| 1 | Validate ESearch responses | `ncbi.validate_esearch`, `validate_esummary`, `validate_efetch_xml`; `ncbi.esearch` / `esummary` are used by every E-utilities caller. Reject: an embedded `ERROR` (in `esearchresult` or at top level), non-JSON/HTML, a missing `esearchresult` / `idlist`, a missing or non-numeric `count`, and `count > retstart` with no ids |
| 2 | Never cache failures; invalidation and retry | `http.request(validate=…)`: an invalid 200 body is retried (3 attempts, backoff), **never cached** (memory or disk), then raised as `ResponseValidationError`. `http.embedded_api_error` is a defence-in-depth check for every NCBI and gnomAD response. Cached entries are re-validated on read and evicted if invalid. `HttpClient.purge_invalid_cache` sweeps the disk cache once per process (it removed the poisoned ITPR1 entry in the re-run preflight) |
| 3 | Genuine zero vs failed / truncated / incomplete | `pubmed_search` logs and raises on failure (it never returns 0 hits); returns `status` (`ok`/`empty`) and NCBI `warnings` (e.g. `quotedphrasesnotfound`). `pubmed_search_all` sets status `complete`/`truncated`/`empty`/`failed`/`partial`. `ledger.retrieval_status` adds `partial`. `clinvar_lookup` raises when every term failed and returns `failed_terms` otherwise. `pmid_exists` raises, so existence stays unverified rather than being marked missing |
| 4 | Regression tests | `tests/test_retrieval_failures.py` (29): HTTP 200 + ERROR, transient error then success, no disk caching, eviction of an entry cached by the old version, startup sweep, 6 malformed payloads, malformed ESummary, 502 retry then success, persistent 502, genuine empty search (cacheable, warnings kept), coverage failure, pagination 59/59, cap truncation 100/130, partial (page-2 failure), follow-up request, model view, report wording (2), ClinVar / ESummary / Gene / EFetch / Europe PMC / gnomAD failures, alias anchors, variant context (2), end-to-end pipeline. Live: `test_live_itpr1_coverage_is_complete_or_explicitly_unresolved` |
| 5 | Pagination and limits | Coverage families page through up to **100 hits** (pages of 50), logged as one query whose total and truncation describe the whole search. ITPR1 `inhibiting_modulators` went from 20/21 to 21/21. The model view lists 25 hits per family and states how many are not shown. Follow-up requests state how many unused hits were not listed |
| 6 | Exact gene and variant context | `europepmc.variant_context`: a Europe PMC hit counts as variant literature only if one passage of the accessible text (a paragraph, or a table row with its caption) contains a gene anchor **and** a variant notation. Otherwise it goes to `europepmc_unconfirmed` with the reason (`notation_without_gene_context`, `notation_not_in_accessible_text`, `full_text_unavailable`), and the ledger record is flagged `variant_context`. Gene anchors are the symbol plus NCBI aliases of at least 5 characters that contain a digit and are not a prefix of another alias (`ncbi.gene_aliases`) |
| 7 | Failures visible downstream | The dossier now carries `coverage_failures` and `failed_queries`. `dossier_text` puts a "RETRIEVAL FAILURES AND INCOMPLETE SEARCHES" block first, so the length limit cannot cut it. `coverage_gaps` turns a failed or partial family into an explicit re-run follow-up request. The pipeline issue `coverage_query_failed` now carries the status, and a new `coverage_query_truncated` issue (info) is added. `render._negative_statement` names every source with any failed query ("k of the PubMed queries failed … incomplete"). `render.human_report_coverage_gap` adds an explicit sentence to the off-label, repurposing and bespoke negative statements and to `precedent_scope` when the mandatory human-treatment search failed and was not successfully re-run. Registry error messages say "retrieval FAILED: no conclusion about absence" and no longer log twice |

## Re-run of the free ITPR1 preflight (`scripts/bootstrap_evidence.py --out-dir outputs/internal/preflight_fixed`)
The original dossier is preserved read-only (`outputs/internal/preflight_03f2bde/`, SHA-256 `261a6236…fdec`), and the exported package is unchanged. The comparison was made with `scripts/compare_dossiers.py` (read-only).

| Family | 03f2bde | Fixed |
|---|---|---|
| activating_modulators | 17/17 | complete 17/17 |
| inhibiting_modulators | 20/21 (truncated) | complete 21/21 |
| **human_treatment_reports** | **0/0 (failure recorded as empty)** | **complete 59/59 (2 pages)**: PMID 37964426 at rank 1, PMID 38844245 at rank 6 |
| nucleic_acid_therapies | 15/15 | complete 15/15 |

- Ledger: 73 → 124 records (+51 PubMed metadata records; none lost).
- Failed queries: 0. The startup sweep evicted 1 cache entry.
- Variant literature: 18 Europe PMC hits were listed before. Now 0 are confirmed and 18 are unconfirmed:
  - 11 `notation_not_in_accessible_text`
  - 6 `full_text_unavailable`
  - 1 `notation_without_gene_context` (a congress-abstract compendium where c.100G>A belongs to another abstract)
- PubMed variant search: still a genuine 0.
- ClinVar: unchanged (VCV000809404, exact transcript-version match).

## Remaining limitations
1. The coverage cap is 100 hits per family. Larger result sets are marked `truncated`, not reviewed in full.
2. Coverage queries are broad keyword queries. Most of the 59 human-treatment hits are unrelated (oncology, autoimmunity); relevance is judged downstream by the LLM and verifier.
3. Variant-context confirmation depends on open-access full text. Paywalled articles, figures and supplements cannot be checked, so they appear as `full_text_unavailable` or `notation_not_in_accessible_text`, which is not the same as being irrelevant. Gene-alias anchoring is heuristic.
4. Validation is endpoint-specific for NCBI E-utilities and Europe PMC search, and generic (embedded-error detection) for NCBI and gnomAD. Other sources (ClinicalTrials.gov, FDA, UniProt, ClinGen, VEP, VariantValidator) still rely on their existing HTTP-status checks.
5. The bounded retries (3 attempts plus HTTP-level retries) can still end in failure during a long NCBI outage. That failure is now recorded and reported, not resolved.
6. Variant-context checks download open-access full texts for up to 25 Europe PMC hits per variant search (free; slower preflight and live tests).

## Results
`pytest`: 145 passed, offline. `pytest -m live`: 7 passed, free public APIs only. No paid inference.
