"""Build configured pipelines (single place where real clients are created)."""

from __future__ import annotations

import os

from .config import Settings, get_settings
from .llm.base import CostTracker
from .pipeline import Pipeline

# Planned A/B/C scientific comparison (run only after code review + explicit cost authorization)
CONFIGS = {
    "A_baseline": dict(mode="baseline", verify_claims=False, reviewer=None),     # simple Claude agent + same retrieval tools
    "B_full": dict(mode="full", verify_claims=True, reviewer=None),              # full pipeline: molecular analysis + verification
    "C_full_openai": dict(mode="full", verify_claims=True, reviewer="openai"),   # + independent OpenAI research (NOT YET INTEGRATED)
    "B_no_verification": dict(mode="full", verify_claims=False, reviewer=None),  # ablation
}


def require_anthropic_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set. Real inference is paid; set the key only after approving the cost estimate "
                         "(see COST_AND_LIMITATIONS.md). Use --mock for an offline dry run.")


def build_pipeline(config: str = "default", settings: Settings | None = None, run_judge: bool = False) -> Pipeline:
    from .llm.anthropic_client import ClaudeLLM

    s = settings or get_settings()
    # Project decision (2026-11-02): Claude Opus is the only active model. No configuration builds a Gemini client;
    # the archived module src/aitx/llm/gemini_client.py is not imported anywhere in the pipeline.
    opts = CONFIGS.get(config) or dict(mode="full", verify_claims=True, reviewer=None)
    if opts["reviewer"] == "openai":
        raise NotImplementedError("Arm C (independent OpenAI research) is a planned slot; the OpenAI integration has not been "
                                  "implemented or authorized yet.")
    require_anthropic_key()
    cost = CostTracker(s.max_cost_usd_per_case, reserve_usd=s.call_reserve_usd)
    llm = ClaudeLLM(s, cost)
    verifier_llm = ClaudeLLM(s, cost, client=llm.client, model=s.verifier_model)
    reviewer = None  # optional independent-review stage E stays available for a future, separately authorised reviewer
    return Pipeline(llm, s, verifier_llm=verifier_llm, reviewer_llm=reviewer, cost=cost, run_judge=run_judge,
                    verify_claims=opts["verify_claims"], mode=opts["mode"])
