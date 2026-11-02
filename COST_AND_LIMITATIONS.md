# Argon cost and readiness limitations

The active pricing and safeguards are documented in [ARGON_MIGRATION.md](ARGON_MIGRATION.md). Prior Claude cost projections do not apply to this branch.

Run `python scripts/estimate_cost.py` offline. Its token volumes are illustrative assumptions, not observations of ITPR1 inference. Thinking tokens count as output. Cached tokens are billed separately; no cache hit is guaranteed. The November introductory rates automatically switch on 2026-12-01 UTC.

The default case limit is $12, with a $0.25 reservation margin, three explicit attempts per request and at most 32,768 output-plus-thinking tokens per request. SDK automatic retry, tool execution and continuation are disabled. An interrupted or unverified report cannot pass submission validation. Budget accounting is per process and is not a provider financial guarantee. A provider error with uncertain billing consumes the reservation conservatively. Account billing alerts alone do not enforce a spending cap.

`run_all.py` also checks the aggregate $30 setting before starting each case, reserving room for the full case cap. Four completed reports are therefore not guaranteed by either the estimate or that limit. No run is authorized by these documents.

No real Argon output, clinical accuracy rate, comparative agent superiority, actual token volume or service-side compatibility was measured. Full-text retrieval can be incomplete; supplements may remain unavailable; scientific review of the first experimental report remains necessary. Offline tests demonstrate code behavior, not clinical validation.
