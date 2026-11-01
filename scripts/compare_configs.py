#!/usr/bin/env python
"""A/B/C comparison (PAID; run only after code review and explicit authorization):
A_baseline vs B_full (vs C_full_openai once implemented) on one case. Reports per-config cost, heuristic scorecard, development-judge scores
(the SAME Claude judge for all configs), and which claims verification removed.
The judge is a development heuristic, not the official score."""
from __future__ import annotations

import argparse
import json
import sys

from _common import ROOT, load_cases, setup_logging

from aitx.config import get_settings
from aitx.factory import CONFIGS, build_pipeline
from aitx.pipeline import save_audit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-id", default="AITX-40002")
    ap.add_argument("--configs", nargs="*", default=["A_baseline", "B_full"], choices=list(CONFIGS))
    args = ap.parse_args()
    setup_logging("WARNING")
    case = next(c for c in load_cases() if c["id"] == args.case_id)
    rows = {}
    for cfg in args.configs:
        res = build_pipeline(cfg, get_settings(), run_judge=True).run(case)
        save_audit(res, ROOT / "outputs" / "compare" / cfg)
        j = {s["dimension"]: s["score_1_to_5"] for s in res.audit.get("judge", {}).get("scores", [])}
        rows[cfg] = {"cost_usd": res.audit["cost"]["total_usd"], "report_chars": len(res.output.report_markdown), "judge": j,
                     "scorecard": res.audit["heuristic_scorecard"], "unresolved": len(res.unresolved),
                     "rule_events": len(res.audit["rule_events"])}
        print(cfg, json.dumps(rows[cfg]), flush=True)
    (ROOT / "outputs" / "compare" / f"{args.case_id}_comparison.json").write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
