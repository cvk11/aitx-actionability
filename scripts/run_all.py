#!/usr/bin/env python
"""Run all four official cases through the SAME pipeline and build the submission
(PAID). outputs/aitx_submission.json is written only if every case ran with real
inference (no mock), passed structural QC, and the combined file validates."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from _common import INTERNAL, ROOT, load_cases, setup_logging

from aitx import prompts
from aitx.config import get_settings
from aitx.factory import CONFIGS, build_pipeline
from aitx.pipeline import save_audit, software_versions
from aitx.qc import validate_submission


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="default", choices=["default", *CONFIGS])
    ap.add_argument("--out", default=str(ROOT / "outputs" / "aitx_submission.json"))
    args = ap.parse_args()
    s = get_settings()
    setup_logging(s.log_level)
    cases = load_cases()
    outputs, manifest = [], {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "config": args.config,
                             "settings": s.public_dict(), "software": software_versions(), "prompts_sha256": prompts.manifest(), "cases": {}}
    spent = 0.0
    for c in cases:
        if spent + s.max_cost_usd_per_case > s.max_cost_usd_total:
            print(f"stopping before {c['id']}: spent {spent:.2f} + per-case cap {s.max_cost_usd_per_case:.2f} would exceed "
                  f"AITX_MAX_COST_USD_TOTAL={s.max_cost_usd_total:.2f}", file=sys.stderr)
            return 5
        pipe = build_pipeline(args.config, s)
        res = pipe.run(c)
        if res.is_mock:
            print("refusing to build a submission from mock output", file=sys.stderr)
            return 3
        spent += res.audit["cost"]["total_usd"]
        save_audit(res, INTERNAL)
        outputs.append(res.output.model_dump())
        manifest["cases"][c["id"]] = {"cost_usd": res.audit["cost"]["total_usd"], "served_models": res.audit["cost"]["served_models"],
                                      "fallback_calls": res.audit["cost"]["fallback_calls"], "seconds": res.audit["seconds"],
                                      "unresolved": res.unresolved, "qc": res.audit["qc"]}
        print(f"{c['id']}: {res.audit['cost']['total_usd']:.2f} USD, {res.audit['seconds']} s, unresolved={len(res.unresolved)}", flush=True)
    v = validate_submission(outputs, [c["id"] for c in cases])
    manifest["submission_validation"] = v.__dict__
    manifest["total_cost_usd"] = round(sum(x["cost_usd"] for x in manifest["cases"].values()), 2)
    (ROOT / "outputs" / "run_manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
    if not v.ok:
        print("submission validation FAILED:", v.errors, file=sys.stderr)
        return 4
    Path(args.out).write_text(json.dumps(outputs, ensure_ascii=False, indent=1))
    print(f"wrote {args.out} ({len(outputs)} reports); manifest: outputs/run_manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
