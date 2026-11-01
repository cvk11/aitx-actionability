#!/usr/bin/env python
"""FREE deterministic evidence pre-scan (no LLM, no cost): variant model, ClinVar,
ClinGen, gnomAD, UniProt, GeneReviews pointers, open trials by gene term.
Writes outputs/internal/<id>.dossier.json. Useful to check data availability
before paid runs."""
from __future__ import annotations

import argparse
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
    args = ap.parse_args()
    s = get_settings()
    setup_logging("WARNING")
    http = HttpClient(s)
    INTERNAL.mkdir(parents=True, exist_ok=True)
    for c in (load_cases(args.input) if args.input else load_cases()):
        case = PatientCase.model_validate(c)
        L = EvidenceLedger()
        ctx = CaseContext(case=case, clinical=parse_clinical_context(case.patient.clinical_context))
        d = bootstrap(ctx, http, L, ToolBox(http, L, s))
        (INTERNAL / f"{case.id}.dossier.json").write_text(json.dumps({"dossier": d, "ledger": L.to_dict(),
                                                                      "clinical": ctx.clinical.__dict__}, indent=1, default=str))
        cf = [v.get("computed_features", {}) for v in d["variants"]]
        print(f"{case.id}: {len(L)} ledger records; age={ctx.clinical.age_years} sex={ctx.clinical.sex}; errors={d['errors']}")
        for v in d["variants"]:
            print("   ", L.get(f"COMPUTED:{v['gene']}:{v['variant_cdna']}").content[:260] if L.get(f"COMPUTED:{v['gene']}:{v['variant_cdna']}") else v)
    print(f"HTTP requests: {http.request_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
