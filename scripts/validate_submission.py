#!/usr/bin/env python
"""Validate a submission file like the organizers' upload form: valid JSON array,
exactly one report per official patient id, all required headings in order."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import ROOT, load_cases

from aitx.qc import validate_submission


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=str(ROOT / "outputs" / "aitx_submission.json"))
    args = ap.parse_args()
    p = Path(args.path)
    if not p.exists():
        print(f"{p} not found (it is only created after real inference by scripts/run_all.py)")
        return 2
    r = validate_submission(p.read_text(encoding="utf-8"), [c["id"] for c in load_cases()])
    for e in r.errors:
        print("ERROR:", e)
    for w in r.warnings:
        print("warning:", w)
    print("VALID" if r.ok else "INVALID")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
