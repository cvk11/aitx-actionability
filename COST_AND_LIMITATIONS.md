# Cost, budget controls and limitations

## 1. Prices used (first-party Claude API, per million tokens; cached reference 2026-10-06)
`claude-opus-5-5`: input $4.00 · output $20.00 · cache read $0.20 · cache write $5.00. Claude Opus is the only billed model; Gemini is not used by any configuration (project decision 2026-11-02).

## 2. Estimated cost per patient (offline model: `python scripts/estimate_cost.py`)

| Setting | Expected | Worst case (all tool rounds used) |
|---|---|---|
| `AITX_MAX_TOOL_ROUNDS=24` (code default) | ≈ $6.6 | ≈ $9.9 |
| `AITX_MAX_TOOL_ROUNDS=16` (`.env.example`, recommended first run) | ≈ $5.5 | ≈ $7.5 |
| `AITX_MAX_TOOL_ROUNDS=14` | ≈ $5.0 | ≈ $7.0 |

Breakdown (16 rounds, expected): interpreter $0.50 · three researchers $2.71 · claim verification (+ bounded re-verification) $1.05 · revision loop $0.64 · synthesis $0.30 · synthesis verification $0.32. Optional judge ≈ $0.35. **These are model-based estimates, not measurements**; adaptive thinking length is the main uncertainty, so plan for **$5–12 per case**. Actual cost is measured per call (`audit.cost`).

## 3. Spending controls (application-level guard — NOT a provider-enforced cap)
**Set an independent monthly spend limit for the API key's workspace in the Anthropic Console before any paid run.** The application guard below cannot see other usage of the same key and cannot stop a request already in flight.

How the guard works (`src/aitx/llm/base.py::CostTracker`, `src/aitx/llm/anthropic_client.py::_send_with_budget`):
* Before **every attempt** of every request, a conservative upper bound of that request's cost is **reserved atomically** (thread-safe, so the three parallel researchers cannot jointly over-commit). Bound = all input at the most expensive cache-write rate of any model a refusal fallback could reach (≈3 characters/token) + `max_tokens` at the most expensive output rate (≈ $0.8 for 32,000 output tokens + input). If committed spend + reservations + this bound exceed `AITX_MAX_COST_USD_PER_CASE`, the request is **not sent**.
* SDK auto-retries are disabled; retries are our own (`AITX_MAX_ATTEMPTS`, default 3) and each attempt is reserved separately. 429/529 responses are recorded as not billed; 5xx, timeouts and connection errors are charged at the full reserved bound (`uncertain_billing`).
* Unknown/fallback model prices use the most expensive known rate.
* **Residual risk:** actual spend can exceed the cap only if a request's real token use exceeds its estimated bound (e.g., unusual tokenisation), or through usage outside this process.

| Variable | Default (code = `.env.example`) | Effect |
|---|---|---|
| `AITX_MAX_COST_USD_PER_CASE` | 12 | Per-case guard; when reached, the case stops and is marked `stage_incomplete` (critical → never submitted) |
| `AITX_CALL_RESERVE_USD` | 0.25 | Extra margin added to every reservation |
| `AITX_MAX_ATTEMPTS` | 3 | Bounded attempts per request |
| `AITX_MAX_COST_USD_TOTAL` | 30 | `run_all.py` stops before a case whose cap could push the run total over this value |
| `AITX_MAX_TOOL_ROUNDS`, `AITX_MAX_REVISIONS`, `AITX_MAX_TOKENS` | 16 (`.env.example`; code 24), 2, 32000 | Loop and output limits |
| `AITX_EFFORT`, `AITX_VERIFIER_EFFORT` | high, medium | Reasoning depth |

## 4. Prepared first real inference — AITX-40002 (ITPR1) — NOT RUN
Owner budget ≈ $32. Recommended command (after explicit authorization and setting the key in `.env`, never in chat):
```bash
AITX_MAX_TOOL_ROUNDS=16 AITX_MAX_COST_USD_PER_CASE=12 AITX_CALL_RESERVE_USD=1.5 python scripts/run_one.py --case-id AITX-40002 --config B_full
```
Model-based estimate ≈ $5.5–7.5 (not a measurement; includes the fail-closed re-verification and synthesis verification); application guard $12 plus an independent provider-side limit. This would leave ≈ $20 — enough for the arm-A baseline on the same case (≈ $2–4) and one more full case, **not** for all four cases plus A/B/C plus repeated-run robustness (≈ $30–50 for four cases alone). A larger budget is needed before the final submission run.

## 5. Planned A/B/C comparison (not started)
| Arm | Config | Status |
|---|---|---|
| A — simple Claude baseline with scientific retrieval | `A_baseline` (one agent, same tools, no molecular bootstrap/rules/verification) | implemented, mock-tested |
| B — full pipeline (molecular analysis + evidence verification) | `B_full` | implemented, mock-tested |
| C — full pipeline + independent OpenAI research | `C_full_openai` | **placeholder** — raises `NotImplementedError`; no OpenAI code or key |
Ablation available: `B_no_verification`. (A Gemini reviewer arm is no longer offered.) Run with `scripts/compare_configs.py` only after code review and cost authorization. Judge scores from `--judge` are a development heuristic, not the official score; expert review of a sample is required to claim improvement.

## 6. Known limitations and failure modes
**Scientific**
- No real LLM inference has been run: mechanism interpretation, recommendation quality and verifier accuracy are unmeasured.
- NMD is rule-based (last exon / ~50–55 nt / start-proximal caveat) on the given transcript only; no isoform, tissue or experimental data (`docs/SCIENTIFIC_CHECKS.md` §3).
- "Not found" in a database is reported as such and never as biological absence; failed retrieval is reported separately; but the LLM may still overstate absence — the verifier and prompt rule 10 mitigate, not eliminate, this.
- Human-precedent detection for the off-label rule is heuristic (publication type + human-subject words); the verifier's evidence-type labels add a second check.
- Paywalled full texts are inaccessible; claims verified from abstracts only are flagged `abstract_only`.
- GeneReviews chapters can be years old (e.g., one official-case chapter last revised 2021); revision dates are passed to the models.
- ClinVar matching relies on record titles (exact cDNA on the same transcript accession); variants described differently in ClinVar may be missed (reported as "searched, not found").
- Ensembl (ENST) transcript inputs: no exon model (VEP/VV only).
- OMIM not used (licensed API); PharmGKB legacy host unreachable (ClinPGx used).
**Technical**
- LLM nondeterminism (temperature cannot be set on current models) and changing live records mean endpoint outputs will resemble, not equal, the submitted reports.
- One report takes minutes (many tool rounds); validator timeouts are unknown.
- Free public APIs can be slow/unavailable (a ClinGen TLS timeout was observed once); failures are recorded but reduce completeness.
- Budget limits can truncate a run; such reports carry a critical `stage_incomplete` issue and `assemble_submission` refuses them.
- Fail-closed verification can remove valid content when the verifier omits decisions or judges a correct statement as partial; this trades completeness for accuracy and must be measured in the A/B comparison.
- Refusal fallback may serve a call with a different model; this is recorded and must be disclosed.
