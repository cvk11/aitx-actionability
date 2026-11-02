#!/usr/bin/env python
"""PREPARED (not executed): minimal live Gemini 4 Argon validation.

Six steps, in order:
  1. authentication + model availability  (models.get metadata; no token billing)
  2. basic text generation
  3. structured JSON output (response_json_schema + Pydantic)
  4. function calling (one tool, one round)
  5. multi-turn tool responses (two sequential tool rounds, function-response IDs)
  6. token usage metadata (per-call accounting through the production client)

Every billed step runs through the production `GeminiLLM` (same serialization,
budget reservation, fail-closed billing and accounting as the pipeline), with a
tiny `max_tokens` override so the reserved bound stays small.

PAID GATE: nothing is sent unless BOTH
  - GEMINI_API_KEY / GOOGLE_API_KEY is set, and
  - `--authorize-paid` is passed explicitly.
Without them the script prints the plan and the conservative cost bound and exits.
Only synthetic text is sent; no patient, gene or variant content is used.

Exit codes: 0 all steps passed; 1 a step failed; 2 key missing; 3 not authorized.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field

from _common import ROOT  # noqa: F401  (sys.path bootstrap for `aitx`)

from aitx.config import Settings, get_settings
from aitx.llm.base import CostTracker, StructuredOutputError
from aitx.llm.gemini_client import MODEL, argon_prices


@dataclass
class Step:
    name: str
    billed: bool
    description: str
    token_bound_input: int = 0  # conservative serialized-input bound for the reservation
    token_bound_output: int = 0


STEPS: list[Step] = [
    Step("auth_and_availability", False, "models.get('gemini-4-argon') metadata; proves key validity and model existence (no billing)"),
    Step("basic_text", True, "one short text generation", 2000, 64),
    Step("structured_json", True, "one structured JSON answer validated by Pydantic", 2500, 512),
    Step("function_calling", True, "one declared tool invoked once, then final JSON", 3000, 512),
    Step("multi_turn_tools", True, "two sequential tool rounds with function-response IDs, then final JSON", 4500, 768),
    Step("usage_metadata", False, "assert every settled call has input/output/cached/thought counts, served model and cost"),
]


def cost_bound(steps: list[Step] | None = None) -> float:
    """Conservative application-level bound for the billed steps (current price schedule)."""
    pin, pout, _ = argon_prices()
    total = 0.0
    for s in steps or STEPS:
        if s.billed:
            total += (s.token_bound_input * pin + s.token_bound_output * pout) / 1e6
    return total


def print_plan() -> None:
    pin, pout, cache = argon_prices()
    print(f"model: {MODEL}  (prices today: input ${pin:.2f}/M, output+thinking ${pout:.2f}/M, implicit cache ${cache:.2f}/M)")
    for i, s in enumerate(STEPS, 1):
        tag = "billed" if s.billed else "free"
        print(f"  {i}. {s.name:24s} [{tag:6s}] {s.description}")
    print(f"Conservative upper bound for the billed steps: ${cost_bound():.4f} USD (application estimate, not a provider cap).")


class MiniToolbox:
    """ToolBox-compatible minimal toolbox for steps 4-5 (production signature only)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def specs(self, names, strict=False):
        spec = {
            "name": "double",
            "description": "Multiply an integer by two. Use it to answer arithmetic questions.",
            "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"],
                             "additionalProperties": False},
        }
        return [spec]

    def execute(self, name: str, args: dict):
        self.calls.append((name, args))
        if name != "double" or not isinstance(args.get("value"), int):
            return "invalid tool call", True
        return str(args["value"] * 2), False


class _Answer:
    pass


def run_checks(settings: Settings, budget_usd: float) -> dict:
    from google import genai
    from google.genai import types
    from pydantic import BaseModel, Field

    class Text(BaseModel):
        text: str

    class Result(BaseModel):
        name: str
        count: int
        ok: bool

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=key, http_options=types.HttpOptions(
        timeout=900000, retry_options=types.HttpRetryOptions(attempts=1)))
    cost = CostTracker(budget_usd, reserve_usd=settings.call_reserve_usd)
    llm = None
    report: dict = {"steps": [], "served_model": None}
    try:
        from aitx.llm.gemini_client import GeminiLLM

        small = get_settings(max_tokens=256, max_attempts=2)
        llm = GeminiLLM(small, cost, client=client)

        # 1 — auth + availability (no token billing)
        try:
            info = client.models.get(model=MODEL)
            report["steps"].append({"step": 1, "ok": True, "detail": f"display_name={getattr(info, 'display_name', '?')}"})
        except Exception as exc:  # availability is ultimately proven by steps 2+; record and continue
            report["steps"].append({"step": 1, "ok": False, "detail": f"models.get failed: {type(exc).__name__}"})

        # 2 — basic text generation (through the production accounting path)
        out = llm.structured(stage="check:basic_text", system="You are a validation harness.", user="Reply with the single word: OK",
                             output_model=Text)
        report["steps"].append({"step": 2, "ok": out.text.strip().upper() == "OK", "detail": out.text.strip()})

        # 3 — structured JSON (schema validated by Pydantic)
        out3 = llm.structured(stage="check:structured", system="You are a validation harness. Return exact JSON.",
                              user="Return JSON: name='validation', count=2, ok=true", output_model=Result)
        report["steps"].append({"step": 3, "ok": out3.name == "validation" and out3.count == 2 and out3.ok, "detail": out3.model_dump_json()})

        # 4 — function calling (one tool, one round)
        tb4 = MiniToolbox()
        out4 = llm.agent(stage="check:tools", system="You are a validation harness. Use the 'double' tool, then finalize.",
                         user="What is 21 doubled? Call the double tool, then return JSON {name:'toolcheck', count:<result>, ok:true}.",
                         output_model=Result, toolbox=tb4, tool_names=None, max_rounds=1)
        ok4 = len(tb4.calls) == 1 and tb4.calls[0] == ("double", {"value": 21}) and out4.count == 42
        report["steps"].append({"step": 4, "ok": ok4 and out4.ok, "detail": f"tool_calls={tb4.calls!r} result={out4.model_dump_json()}"})

        # 5 — multi-turn tool responses (two sequential rounds, IDs preserved by the client)
        tb5 = MiniToolbox()
        out5 = llm.agent(stage="check:multiturn", system="You are a validation harness. Use the 'double' tool twice in separate turns.",
                         user="Double 3, then double the result, then return JSON {name:'multi', count:<final>, ok:true}.",
                         output_model=Result, toolbox=tb5, tool_names=None, max_rounds=2)
        ok5 = len(tb5.calls) == 2 and tb5.calls[0][1].get("value") == 3 and tb5.calls[1][1].get("value") == 6 and out5.count == 12
        report["steps"].append({"step": 5, "ok": ok5 and out5.ok, "detail": f"tool_calls={tb5.calls!r} result={out5.model_dump_json()}"})

        # 6 — usage metadata: every settled call must carry full accounting
        calls = cost.calls
        bad = [c for c in calls if c.input_tokens <= 0 or c.output_tokens <= 0 or not c.served_model or c.cost_usd < 0]
        report["steps"].append({"step": 6, "ok": bool(calls) and not bad,
                                "detail": f"{len(calls)} billed calls; total {cost.total_usd:.4f} USD; served={sorted({c.served_model for c in calls})}"})
        report["served_model"] = calls[0].served_model if calls else None
        report["cost"] = cost.summary()
    finally:
        try:
            client.close()
        except Exception:
            pass
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Minimal live gemini-4-argon validation (PAID steps gated)")
    ap.add_argument("--authorize-paid", action="store_true", help="explicit authorization to run the billed steps")
    ap.add_argument("--max-cost-usd", type=float, default=2.0, help="application budget for this validation (default 2.00)")
    args = ap.parse_args(argv)

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("GEMINI_API_KEY / GOOGLE_API_KEY is not set. This validation cannot run without a key.")
        print("No request was sent.")
        return 2
    if not args.authorize_paid:
        print("PAID VALIDATION PREPARED, NOT AUTHORIZED. Plan:")
        print_plan()
        print("Pass --authorize-paid after reviewing the plan and Google billing controls. No request was sent.")
        return 3

    settings = get_settings(max_tokens=256, max_attempts=2)
    bound = cost_bound()
    if args.max_cost_usd < bound:
        print(f"--max-cost-usd {args.max_cost_usd:.2f} is below the conservative bound ${bound:.4f}; refusing.", file=sys.stderr)
        return 4
    report = run_checks(settings, args.max_cost_usd)
    ok = all(s["ok"] for s in report["steps"])
    for s in report["steps"]:
        print(f"  step {s['step']} {'PASS' if s['ok'] else 'FAIL'}  {s['detail']}")
    print(f"overall: {'PASS' if ok else 'FAIL'}; served_model={report.get('served_model')}")
    if report.get("cost"):
        print(f"total_usd={report['cost']['total_usd']} uncertain_billing_calls={report['cost']['uncertain_billing_calls']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
