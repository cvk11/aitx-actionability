#!/usr/bin/env python
"""Offline cost estimate for one case (no API call). Token model assumptions are
explicit and conservative; actual cost is measured per call by CostTracker.

  python scripts/estimate_cost.py                 # current settings
  AITX_MAX_TOOL_ROUNDS=14 python scripts/estimate_cost.py
"""
from __future__ import annotations

import sys

from _common import ROOT  # noqa: F401  (sets sys.path)

from aitx import prompts
from aitx.config import PRICES_PER_MTOK, get_settings

# assumptions (tokens)
NEW_TOKENS_PER_ROUND = 6000     # tool results appended per agent round (<=14k chars per result, ~1-2 calls/round)
OUT_PER_ROUND = 1800            # adaptive thinking + tool call per round
FINAL_OUT = 7000                # final structured JSON + thinking
CLAIMS_PER_CASE = 70            # atomic claims + one statement pseudo-claim per item
VERIFY_BATCH = 18
VERIFY_IN, VERIFY_OUT = 22000, 7000


def agent_cost(p, system_tokens: int, rounds: int) -> float:
    cost, prefix = 0.0, system_tokens + 2000
    for _ in range(rounds):
        cost += prefix * p["cache_read"] / 1e6 + NEW_TOKENS_PER_ROUND * p["cache_write"] / 1e6 + OUT_PER_ROUND * p["output"] / 1e6
        prefix += NEW_TOKENS_PER_ROUND + OUT_PER_ROUND
    return cost + prefix * p["cache_read"] / 1e6 + FINAL_OUT * p["output"] / 1e6


def main() -> int:
    s = get_settings()
    p = PRICES_PER_MTOK[s.primary_model]
    sys_tok = lambda n: len(prompts.raw(n) + prompts.raw("common_rules")) // 4 + 4000  # + context block
    rows = {}
    for label, frac in (("expected (~60% of tool budget used)", 0.6), ("worst case (all rounds used)", 1.0)):
        r = max(1, round(s.max_tool_rounds * frac))
        a = agent_cost(p, sys_tok("interpreter"), max(1, round(min(8, s.max_tool_rounds) * frac)))
        bc = sum(agent_cost(p, sys_tok(n), r) for n in ("research_established", "research_emerging", "research_hypothesized"))
        nb = -(-CLAIMS_PER_CASE // VERIFY_BATCH)
        d = nb * (VERIFY_IN * p["input"] + VERIFY_OUT * p["output"]) / 1e6
        rev = s.max_revisions * ((20000 * p["input"] + 5000 * p["output"]) / 1e6 + (15000 * p["input"] + 4000 * p["output"]) / 1e6)
        f = (30000 * p["input"] + 9000 * p["output"]) / 1e6
        fv = (25000 * p["input"] + 5000 * p["output"]) / 1e6 * (1 + frac)  # synthesis verification (+ possible re-verification)
        rows[label] = {"A_interpret": a, "BC_research_x3": bc, "D_verify": d * (1 + 0.25 * frac), "revision_loop": rev, "F_synthesis": f,
                       "F_synthesis_verify": fv}
    print(f"model={s.primary_model} prices/MTok={p} max_tool_rounds={s.max_tool_rounds} max_revisions={s.max_revisions} "
          f"cap/case={s.max_cost_usd_per_case} reserve={s.call_reserve_usd}")
    for k, v in rows.items():
        print(f"{k}: total ~${sum(v.values()):.2f}  " + ", ".join(f"{a}=${b:.2f}" for a, b in v.items()))
    print("Optional: development judge ~$0.35. Claude Opus is the only model; no other provider is billed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
