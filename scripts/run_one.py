#!/usr/bin/env python
"""Process one patient through the full pipeline (PAID: real Gemini Argon API calls).

  python scripts/run_one.py --case-id AITX-40002            # official case
  python scripts/run_one.py --input my_hypothetical.json     # any valid input object
Outputs (development mode only): outputs/internal/<id>.md, <id>.audit.json, <id>.output.json
"""
from __future__ import annotations

import argparse
import json
import sys

from _common import INTERNAL, load_cases, setup_logging

from aitx.config import get_settings
from aitx.factory import CONFIGS, build_pipeline
from aitx.pipeline import save_audit


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--case-id")
    g.add_argument("--input")
    ap.add_argument("--config", default="default", choices=["default", *CONFIGS])
    ap.add_argument("--judge", action="store_true", help="also run the development heuristic judge (extra cost)")
    ap.add_argument("--out", default=str(INTERNAL))
    args = ap.parse_args()
    s = get_settings()
    setup_logging(s.log_level)
    if args.case_id:
        cases = [c for c in load_cases() if c["id"] == args.case_id]
        if not cases:
            print(f"unknown case id {args.case_id}", file=sys.stderr)
            return 2
        case = cases[0]
    else:
        case = load_cases(args.input)[0]
    pipe = build_pipeline(args.config, s, run_judge=args.judge)
    res = pipe.run(case)
    from pathlib import Path

    out = Path(args.out)
    if s.persist_artifacts:
        p = save_audit(res, out)
        (out / f"{res.output.id}.output.json").write_text(json.dumps(res.output.model_dump(), indent=1, ensure_ascii=False))
        print(f"audit: {p}")
    print(json.dumps({"id": res.output.id, "cost": res.audit["cost"]["total_usd"], "served_models": res.audit["cost"]["served_models"],
                      "submittable": res.submittable, "blockers": res.blockers, "unresolved": res.unresolved[:10],
                      "scorecard": res.audit["heuristic_scorecard"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

