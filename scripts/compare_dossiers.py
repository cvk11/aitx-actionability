#!/usr/bin/env python
"""FREE, read-only comparison of two evidence dossiers written by scripts/bootstrap_evidence.py.

  python scripts/compare_dossiers.py OLD.dossier.json NEW.dossier.json --out comparison.json

Neither input is modified. Reports per-family coverage (status/total/reviewed, keys gained/lost), retrieval status,
failed queries, ledger records gained/lost by source type, variant-literature context classification, and ClinVar
matches, plus SHA-256 of both inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _fam_status(f: dict) -> str:
    return f.get("status") or ("failed" if f.get("error") else ("truncated" if f.get("truncated") else ("empty" if not f.get("total") else "complete")))


def compare(old: dict, new: dict) -> dict:
    od, nd = old["dossier"], new["dossier"]
    out: dict = {"coverage": {}, "ledger": {}, "queries": {}, "variant_literature": {}, "clinvar": {}}
    for gene in sorted(set(od.get("therapeutic_coverage", {})) | set(nd.get("therapeutic_coverage", {}))):
        of, nf = od.get("therapeutic_coverage", {}).get(gene, {}), nd.get("therapeutic_coverage", {}).get(gene, {})
        for fam in sorted(set(of) | set(nf)):
            a, b = of.get(fam, {}), nf.get(fam, {})
            ka, kb = a.get("keys", []), b.get("keys", [])
            out["coverage"][f"{gene}:{fam}"] = {
                "old": {"status": _fam_status(a), "total": a.get("total"), "reviewed": len(ka), "error": a.get("error", "")},
                "new": {"status": _fam_status(b), "total": b.get("total"), "reviewed": len(kb), "error": b.get("error", "")},
                "gained": [k for k in kb if k not in set(ka)], "lost": [k for k in ka if k not in set(kb)],
                "gained_titles": {k: (b.get("titles") or {}).get(k, "") for k in kb if k not in set(ka)}}
    ro = {r["key"]: r for r in old["ledger"]["records"]}
    rn = {r["key"]: r for r in new["ledger"]["records"]}
    out["ledger"] = {"old_records": len(ro), "new_records": len(rn),
                     "gained_by_source": dict(Counter(rn[k]["source_type"] for k in rn.keys() - ro.keys())),
                     "lost": sorted(ro.keys() - rn.keys()),
                     "variant_context_flags": dict(Counter(r["metadata"].get("variant_context") for r in rn.values() if r["metadata"].get("variant_context")))}
    out["queries"] = {"old_retrieval_status": od.get("retrieval_status"), "new_retrieval_status": nd.get("retrieval_status"),
                      "old_failed": [q for q in old["ledger"]["queries"] if q.get("error")],
                      "new_failed": [q for q in new["ledger"]["queries"] if q.get("error")],
                      "new_coverage_failures": nd.get("coverage_failures", [])}
    vo, vn = (od.get("variant_literature") or [{}])[0], (nd.get("variant_literature") or [{}])[0]
    out["variant_literature"] = {
        "old": {"europepmc_listed": len(vo.get("europepmc", [])), "pubmed": vo.get("pubmed_hits")},
        "new": {"status": vn.get("status"), "europepmc_confirmed": len(vn.get("europepmc", [])), "europepmc_unconfirmed": len(vn.get("europepmc_unconfirmed", [])),
                "unconfirmed_reasons": dict(Counter(h["variant_context"]["status"] for h in vn.get("europepmc_unconfirmed", []))),
                "gene_anchor_terms": vn.get("gene_anchor_terms"), "pubmed": vn.get("pubmed_hits"), "failures": vn.get("failures")}}
    out["clinvar"] = {"old": [(r["key"], r.get("match"), r.get("classification")) for c in od.get("clinvar", []) for r in c.get("records", [])],
                      "new": [(r["key"], r.get("match"), r.get("classification")) for c in nd.get("clinvar", []) for r in c.get("records", [])]}
    out["http_diagnostics_new"] = new.get("http_diagnostics")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    po, pn = Path(a.old), Path(a.new)
    res = {"inputs": {"old": {"path": str(po), "sha256": hashlib.sha256(po.read_bytes()).hexdigest()},
                      "new": {"path": str(pn), "sha256": hashlib.sha256(pn.read_bytes()).hexdigest()}},
           **compare(_load(po), _load(pn))}
    text = json.dumps(res, indent=1, ensure_ascii=False)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    for k, v in res["coverage"].items():
        print(f"{k}: {v['old']['status']} {v['old']['reviewed']}/{v['old']['total']} -> {v['new']['status']} {v['new']['reviewed']}/{v['new']['total']}"
              f" (+{len(v['gained'])} / -{len(v['lost'])})")
    print("ledger:", res["ledger"]["old_records"], "->", res["ledger"]["new_records"], "gained", res["ledger"]["gained_by_source"], "lost", len(res["ledger"]["lost"]))
    print("variant literature:", res["variant_literature"]["old"], "->", {k: res["variant_literature"]["new"][k] for k in ("status", "europepmc_confirmed", "europepmc_unconfirmed", "unconfirmed_reasons")})
    print("clinvar:", res["clinvar"]["old"] == res["clinvar"]["new"], res["clinvar"]["new"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
