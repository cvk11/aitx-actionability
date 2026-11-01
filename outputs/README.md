# outputs/

* `aitx_submission.json` — created **only** by `scripts/run_all.py` after real (non-mock) inference on all four official cases, structural QC and submission validation. It does not exist yet: no paid inference has been run.
* `run_manifest.json` — models served, costs, prompt hashes, software versions, unresolved issues (created with the submission).
* `internal/` (git-ignored) — per-case audit trails (`*.audit.json`), rendered reports, deterministic dossiers and `scientific_checks.json`.
