# Scientific-readiness remediation before the first paid inference (after `f14fe83`)

Scope: verify the six scientific requirements against the implementation, fix only confirmed defects, and validate for free. **No paid model call was made.** Official case files unchanged. No submission. No deployment.

Baseline inspected: `097a6bc` (= `f14fe83` plus a `.gitignore` rule).

---

## Phase A — safeguards verified against the implementation

Each requirement was checked by reading the code and, where behaviour was in question, by running it.

### Confirmed already met

**R1 — full texts, tables and supplements retrievable and searchable, with explicit access limitations.**
`europepmc.fulltext_passages` parses every `<sec>` paragraph **and** every `<tr>` table row, prefixing each row with its table label, caption and header so a patient row stays attributable. `<supplementary-material>` entries are listed and explicitly marked "not parsed". `load_fulltext` records `fulltext_status` (`loaded` / `not_open_access` / `failed`), and `verifier._evidence_packet` prints `fulltext_status` and `supplementary_files` next to the passages. The verdict `insufficient_source_text` exists precisely so a missing table is recorded as "could not be assessed" rather than as a negative finding, and `rules.apply_verdicts` turns it into the event `claim_not_assessable` with the note "not shown to be false".

**R2 — publication, variant, individual patient, intervention and outcome not conflated.**
`ClaimVerdict` carries `evidence_type`, `applicability`, `intervention_matches` and `overgeneralization` as separate fields. `verifier.ground_verdict` is deterministic and cannot be argued around: a `supported`/`partially_supported` cited claim must carry a verbatim quote found in one of **its own** cited sources, and `same_variant` applicability is downgraded automatically unless a cited source actually contains one of the patient's variant notations. The verifier prompt states the person-level rule explicitly: "A published individual with the same variant but a different age, sex or phenotype is a different person: never treat their response, dose or course as this patient's." For a human precedent it additionally requires that the passage "states that the named intervention was given to identified patient(s) in that context AND reports an outcome. Mentioning patients is not precedent."

**R3 — another patient's or a cohort's response cannot be attributed to the competition patient.**
Three independent layers: the `OVERCLAIM` regex flags "will/would improve/benefit/respond…" and forces re-verification; the human-precedent tier gate requires a `supported` `treatment_effect_human` claim with `evidence_type ∈ {direct_human_same_variant, same_gene, same_disease}`, `applicability ∈ HUMAN_APPLICABLE`, `intervention_matches` **and** a literature-type source; and `variant_mechanism_transfer` removes a `molecular_fact` claim worded about "this variant" when it is supported only for a different variant.

### Confirmed defects

| # | Requirement | Defect | Evidence of the defect |
|---|---|---|---|
| D1 | **R4** — negative, harmful and null findings preserved | `evidence.select_passages` ranked passages **only** by overlap with the claim's own vocabulary. A harm or null result phrased in different words (e.g. "unaffected littermates … displayed reduced rotarod latency") scored zero and was dropped, so the verifier saw only the favourable half of the source and could legitimately return `supported`. | Reproduced on `097a6bc`: with a synthetic record containing a rescue result and a harmful wild-type-control result, `HARM_SHOWN=False` at both a 3,500- and a 1,200-character budget. |
| D2 | **R5** — eligibility judged on the correct study arm | `HealthyVolunteers` was requested in `FIELDS` but never flattened, never stored in the record metadata and never mentioned in any rule or prompt. An eligibility text split into arm blocks ("Healthy Volunteers", "Group B", "For patients with …") was passed to the model as one undifferentiated block, so a healthy-volunteer requirement could be read as a patient-eligibility requirement. | Reproduced on `097a6bc`: `healthy_volunteers` absent from the flattened record, no arm warning in the registered content, no caveat on the trial item. Age, sex, genotype and recruitment-status gates were already correct and were left unchanged. |
| D3 | **R6** — unsupported GoF/LoF assumptions cannot drive therapy | `mechanism_confidence` existed on the interpretation schema but had **no consumer anywhere except the verifier's context text**. Nothing deterministic noticed that proposing a potentiator (or an inhibitor) commits to a direction of effect for this variant. The only protection was the verifier LLM spotting a GoF↔LoF overgeneralization. | Reproduced on `097a6bc`: a repurposing item proposing a "selective channel potentiator" supported only by `same_gene_other_variant_or_mechanism` evidence produced no event and no problem. |

---

## Phase C — minimal remediation

Three targeted changes. No pipeline redesign, no new stage, no new agent, no speculative treatment content, no change to any official input, and nothing from any external evaluation injected into a prompt.

### D1 — contrast-passage retention (`src/aitx/evidence.py`, `src/aitx/agents/verifier.py`)
`CONTRAST` matches negative, null, harmful and limiting language (no significant / failed to / worsened / impaired / adverse / toxic / mortality / wild-type / unaffected littermates / control group / however / in contrast / limitation …). `contrast_passages()` returns their indices, and `select_passages` now reserves a **separate budget** (`max(600, max_chars // 3)`) for them, so a qualifying passage is never squeezed out by passages that merely echo the claim's wording. The returned `info` gained `contrast_passages` and `contrast_shown`, and the verifier's evidence packet prints `negative_or_limiting_passages_shown=x of y` so the model knows whether it saw the whole picture.

### D2 — study-arm awareness (`src/aitx/retrieval/clinicaltrials.py`, `src/aitx/rules.py`)
`arm_headings()` detects arm/cohort headings in an eligibility text. `_flatten` now carries `healthy_volunteers` and `eligibility_arm_headings`, both added to `META_FIELDS` so they survive the live status re-check. The registered record states "accepts healthy volunteers: …" and, when headings exist, prefixes the eligibility text with an explicit instruction that criteria under one heading apply only to that arm and that healthy-volunteer requirements are not patient-eligibility requirements. `apply_section_rules` adds the same point to the item's `criteria_unassessable`, so it reaches the rendered report deterministically rather than depending on the model.

### D3 — direction-of-effect dependency gate (`src/aitx/rules.py`)
`DIRECTIONAL` matches interventions whose rationale depends on the direction of the variant's functional effect (potentiator / activator / agonist / positive modulator / enhancer / inhibitor / antagonist / blocker / negative modulator / suppressor / knockdown / silencing / allele-specific). For a Tier-3 item naming one of these, `apply_verdicts` now requires at least one `supported` claim with applicability `same_variant` or `same_gene_same_mechanism`. Otherwise it records the event `mechanism_direction_unestablished` and a revision problem requiring the dependency and the uncertainty to be stated explicitly, or the candidate dropped.

**Honest limit of D3:** this is a *surfacing* safeguard, not a hard block. It routes the item into the bounded revision loop and into `unresolved` in the audit; it does not by itself remove the item from the report. A hard block was not implemented because dropping legitimate, correctly hedged hypothesised actions would cost recall in the one tier where hypotheses belong.

### D4 — case-notation leak in production code (found during validation)
`src/aitx/retrieval/europepmc.py` carried the official case's short protein notation (`D34N`) in two docstrings, as an illustrative example added by the previous commit. AGENTS.md forbids case variants in `src/`, `prompts/` and `deployment/`, but `test_no_official_case_specifics_in_code_or_prompts` only banned the case id, gene symbol, transcript and cDNA notation — not the protein notation or its short forms, so the leak passed. The docstrings now use a neutral example, and the test bans **every** spelling produced by `variant_notations()` for each official case (`p.Asp34Asn`, `Asp34Asn`, `D34N`, …). Nothing in the logic depended on the notation; this was documentation text, not case-specific tuning.

### Prompt rules (`prompts/common_rules.md`)
Two rules added, numbered 14 and 15: arm-specific eligibility attribution, and the requirement to report a source's negative/null/harmful finding alongside its positive one.

---

## Phase B — regression tests

`tests/test_scientific_readiness.py`, 18 tests. All records are **synthetic**; they reproduce the *structure* of the discovery examples without asserting any real publication's content, and no case-specific logic exists in `src/`, `prompts/` or `deployment/` (enforced by `test_units.py::test_no_official_case_specifics_in_code_or_prompts`).

| Discovery example | What the test pins down |
|---|---|
| A cohort paper whose per-patient therapy table is inaccessible | The access limitation reaches the verifier packet verbatim, and the distinct `insufficient_source_text` verdict is available instead of `unsupported` |
| The same cohort with an accessible genotype row for a **different** individual | The row grounds a *molecular* claim about that individual at `same_variant` applicability, but a claim inventing a *treatment response* for them fails quote grounding (`quote_failures == 1`) and becomes `insufficient_source_text` |
| A correctly cited response in another individual | Cannot be rendered as this patient's expected benefit: `OVERCLAIM` fires and the statement status becomes `needs_reverification` |
| A preclinical study reporting a rescue **and** a harmful wild-type-control result | Both reach the verifier at a wide and a narrow budget; a null result ("no significant change") is also retained; the packet reports how many limiting passages were shown; the detector does not fire on ordinary positive text |
| A registry eligibility text split by arm | Headings detected; `healthy_volunteers` flattened; the record content warns against transferring arm criteria; the trial item carries the caveat; plain eligibility text produces no false headings; the existing age gate still drops an adults-only trial |
| Other substitutions at the same residue, and a neighbouring-residue variant, with measured functional directions | Their direction is not assigned to the patient's variant (`variant_mechanism_transfer`); a directional candidate without variant-specific mechanism evidence is flagged; one **with** such evidence is not flagged; a non-directional supportive item is not flagged |

All three defects were reproduced on `097a6bc` before the fixes, by running the pre-fix source directly (the new test module does not even import there, since `contrast_passages` and `arm_headings` do not exist).

---

## Phase D — free validation

| Check | Result |
|---|---|
| Complete offline suite (`pytest`) | **163 passed** (145 before + 18 new), 7 deselected |
| Free live biomedical API tests (`pytest -m live`) | **7 passed** |
| Deterministic ITPR1 preflight | 124 ledger records, **0 coverage gaps, 0 failed queries**, 20 HTTP requests; coverage identical to the `f14fe83` preflight (59/59, 21/21, 17/17, 15/15); ClinVar unchanged |
| Report structure QC | `ok=True`, no errors; all 12 official template headings present |
| Output schema | Exactly `{id, report_markdown}` |
| Submission gate | Refuses a mock run (`SubmissionBlocked: mock output must never be submitted`) |
| Secret scan (tracked files + full history) | No secrets. The one pattern hit is the scan command documented in `CODEX_REVIEW_GUIDE.md` §9, not a credential |
| Git cleanliness | Working tree clean after commit; `.env` absent; preflight outputs git-ignored |

Original reports and evidence exports were preserved: the new preflight was written to a separate directory and compared read-only against the existing one.

---

## Remaining known limitations

1. **D3 surfaces, it does not block.** A directional Tier-3 candidate with an unestablished mechanism is flagged into the revision loop and the audit, not removed. Whether the revision loop actually fixes the wording is unmeasured until a real run.
2. **Contrast detection is lexical.** `CONTRAST` is a regex over English result language. A harm reported only in a figure, a supplement, or in wording it does not match can still be missed, and a non-open-access article remains unreadable in the first place.
3. **Arm detection is heading-based.** An eligibility text that mixes arms *within* a block, or uses an unusual heading, will not be split. The caveat then does not fire, and arm attribution falls back to the model and the verifier.
4. **The verifier is still an LLM.** Quote grounding proves a passage exists, not that it was interpreted correctly.
5. **Paywalled full texts, patient tables and supplements remain inaccessible**; such evidence is reported as "could not be assessed", which lowers completeness.
6. **No measured superiority over a plain Claude agent** (arm A versus B has not been run), and verifier accuracy remains unmeasured until an authorised real run.
7. **These fixes were written by the same model family as the pipeline.** They are not an independent audit of it.
