#!/usr/bin/env python
"""Repeated-inference robustness (adapted from the author's radiology stochasticity study).

Analyse existing audits (free):
  python scripts/robustness.py --audits outputs/runs/AITX-40002/run*.audit.json
Run N repeats then analyse (PAID, ~N x per-case cost):
  python scripts/robustness.py --case-id AITX-40002 --runs 3
"""
from __future__ import annotations

import argparse
import glob
import json
import sys

from _common import ROOT, load_cases, setup_logging

from aitx.robustness import analyse_runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audits", nargs="*")
    ap.add_argument("--case-id")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    setup_logging("WARNING")
    paths = [p for pat in (args.audits or []) for p in glob.glob(pat)]
    if args.case_id:
        from aitx.config import get_settings
        from aitx.factory import build_pipeline
        from aitx.pipeline import save_audit

        case = next(c for c in load_cases() if c["id"] == args.case_id)
        out = ROOT / "outputs" / "runs" / args.case_id
        for k in range(args.runs):
            res = build_pipeline("default", get_settings()).run(case)
            p = save_audit(res, out / f"run{k + 1}")
            paths.append(str(p))
            print(f"run {k + 1}: {res.audit['cost']['total_usd']:.2f} USD")
    audits = [json.loads(open(p).read()) for p in paths]
    rep = analyse_runs(audits)
    print(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
