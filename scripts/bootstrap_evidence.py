#!/usr/bin/env python
"""FREE deterministic evidence pre-scan (no LLM, no cost): variant model, ClinVar,
ClinGen, gnomAD, UniProt, GeneReviews pointers, open trials by gene term.
Writes outputs/internal/<id>.dossier.json. Useful to check data availability
before paid runs."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys

from _common import INTERNAL, load_cases, setup_logging

from aitx.agents.context import CaseContext
from aitx.agents.interpreter import bootstrap
from aitx.clinical import parse_clinical_context
from aitx.config import get_settings
from aitx.http import HttpClient
from aitx.ledger import EvidenceLedger
from aitx.retrieval.registry import ToolBox
from aitx.schemas import PatientCase


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=None)
    ap.add_argument("--out-dir", default=str(INTERNAL), help="output directory (default outputs/internal); use a new one to keep earlier dossiers")
    args = ap.parse_args()
    s = get_settings()
    setup_logging("WARNING")
    http = HttpClient(s)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for c in (load_cases(args.input) if args.input else load_cases()):
        case = PatientCase.model_validate(c)
        L = EvidenceLedger()
        ctx = CaseContext(case=case, clinical=parse_clinical_context(case.patient.clinical_context))
        d = bootstrap(ctx, http, L, ToolBox(http, L, s))
        diag = {"http_requests": http.request_count, "cache_evictions": http.cache_evictions, "validation_failures": http.validation_failures}
        (out_dir / f"{case.id}.dossier.json").write_text(json.dumps({"dossier": d, "ledger": L.to_dict(), "clinical": ctx.clinical.__dict__,
                                                                    "http_diagnostics": diag}, indent=1, default=str))
        cf = [v.get("computed_features", {}) for v in d["variants"]]
        print(f"{case.id}: {len(L)} ledger records; age={ctx.clinical.age_years} sex={ctx.clinical.sex}; errors={d['errors']}; "
              f"coverage gaps={[(g['family'], g['status']) for g in d.get('coverage_failures', [])]}; "
              f"failed queries={len(d.get('failed_queries', []))}; cache evictions={http.cache_evictions}")
        for v in d["variants"]:
            print("   ", L.get(f"COMPUTED:{v['gene']}:{v['variant_cdna']}").content[:260] if L.get(f"COMPUTED:{v['gene']}:{v['variant_cdna']}") else v)
    print(f"HTTP requests: {http.request_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
