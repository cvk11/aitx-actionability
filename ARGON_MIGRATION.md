# Argon migration — implementation and verification boundary

Base: `b2704f602d211e79f12ff2c31fdba6526994c2f5`, PR #2. The five-fix base had a residual inline trial-arm bug; this branch includes its correction and six regression cases. It must not be described as an unconditionally approved baseline.

The active factory now constructs only `GeminiLLM`, with `gemini-4-argon` for research, interpretation, claim verification and synthesis. No Anthropic key, SDK import or fallback is required by active inference. The old Anthropic module and its offline regression tests are historical compatibility artifacts, not runtime entry points. No paid API call was made.

## API contract

Pinned SDK: `google-genai==2.34.0`. Uses documented `models.generate_content`, explicit local `Content` history, `FunctionDeclaration.parameters_json_schema`, matching function-response IDs, and intact model parts/thought signatures. Finalization uses a separate tool-free request with `response_json_schema`; Pydantic validates the returned JSON. `low`, `medium`, `high` are the only thinking levels. Default maximum 32,768 output-plus-thinking tokens, synchronous maximum 65,536; conservative text context check against 1,048,576.

SDK retries are one attempt. Automatic continuation and automatic tool execution are disabled. Each explicit request reserves budget atomically. Application retries are bounded for 429/5xx; connection/unknown failures stop with uncertain billing conservatively charged. Missing usage, unknown served model, incomplete/blocked/truncated outputs stop processing. Error strings omit provider response bodies and model text. `store=False` disables request logging through the API option; provider retention and account policy still require owner review.

Streaming and long-decode continuation are intentionally not enabled. Their partial results therefore cannot be consumed by this implementation. Background/server-stored interactions and paid Google Search grounding are not used. Biomedical retrieval continues through the project's public-source tools.

## Pricing and bounds

Official pricing checked 2026-11-02: through Nov 30 input/output $2/$10 per million; from Dec 1 $4/$20. Thinking is charged as output. Implicit cached input is 5% of input price. The code switches by UTC date; no explicit cache creation. Additional tool-use prompt tokens are conservatively charged as input. Actual model usage, not local character counts, settles successful requests.

The request reservation uses serialized UTF-8 bytes plus overhead and the configured output limit. It is a conservative application estimate, not a guaranteed provider financial cap. Unknown billing keeps the reservation charged. Separate processes do not share a budget. Google billing alerts are not a hard cap. Configure account/quota controls separately and inspect them before any paid run.

`python scripts/estimate_cost.py` gives illustrative workloads, not measured patient costs. At introductory rates, 1M input + 150k output/thinking costs $3.50; 3M + 500k costs $11. Four such runs cost $14–44; 20% additional billed tokens gives $4.20–13.20 for one run. A $12 application limit may interrupt the larger retry scenario. These token assumptions are not experimentally established for ITPR1.

## Verification and remaining prerequisites

Tests exercise actual SDK request serialization through `httpx.MockTransport`, multiple calls and IDs, signatures, schema validation, accounting, expiry, rate/auth failures, missing usage, blocked/truncated output, and full synthetic scientific pipeline execution through Gemini response objects. Passing them does not establish live model availability, semantic accuracy, response latency, or actual API schema acceptance by the service. No real patient report was generated.

Before paid inference: inspect the final test report; use this branch's exact commit, valid Gemini billing/access, the reviewed $12 limit, and deployment privacy settings. Review the first experimental output scientifically. Do not claim superiority over a simple agent without comparative experiments. Model migration does not eliminate verifier mistakes or guarantee literature completeness.

Official references:

- https://ai.google.dev/gemini-api/docs/models/gemini-4-argon
- https://ai.google.dev/gemini-api/docs/latest-model
- https://ai.google.dev/api/generate-content
- https://ai.google.dev/gemini-api/docs/pricing
