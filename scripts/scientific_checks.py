#!/usr/bin/env python
"""FREE deterministic scientific checks (no LLM): HGVS/transcript consistency,
computed vs VEP exon, NMD rule inputs, ClinVar/ClinGen/GeneReviews status with
explicit 'found / searched-not-found / retrieval-failed' categories, open-trial
retrieval, and GeneReviews provenance. Includes CFTR p.Phe508del (the
organizers' public format example) as a positive control.

Writes outputs/internal/scientific_checks.json. These checks validate the
deterministic layer only; they do NOT establish the clinical validity of any
LLM-generated report."""
from __future__ import annotations

import json
import sys

from _common import INTERNAL, load_cases, setup_logging

from aitx.config import get_settings
from aitx.http import HttpClient
from aitx.ledger import EvidenceLedger
from aitx.retrieval import clinicaltrials, genereviews, ncbi, variant
from aitx.retrieval.ncbi import refseq_feature_table
from aitx.variant_model import parse_feature_table

CONTROL = {"id": "CONTROL-CFTR", "patient": {"genotype": [{"gene": "CFTR", "transcript": "NM_000492.4", "variant_cdna": "c.1521_1523del",
                                                            "variant_protein": "p.Phe508del", "zygosity": "homozygous"}], "clinical_context": "control"}}


def status(qs, tool):
    q = [x for x in qs if x.tool == tool]
    if not q:
        return "not_queried"
    if all(x.error for x in q):
        return "retrieval_failed"
    return "found" if any(x.n_results for x in q if not x.error) else "searched_not_found"


def main() -> int:
    setup_logging("WARNING")
    s = get_settings()
    http = HttpClient(s)
    gr = genereviews.get_index(str(s.genereviews_dir))
    report = {"genereviews_snapshot": gr.manifest, "cases": {}}
    for case in load_cases() + [CONTROL]:
        for v in case["patient"]["genotype"]:
            L = EvidenceLedger()
            a = variant.annotate_variant(http, L, v["gene"], v["transcript"], v["variant_cdna"], v["variant_protein"])
            cf = a.get("computed_features", {})
            g = variant.gene_profile(http, L, v["gene"], cf.get("aa_position"))
            cv = ncbi.clinvar_lookup(http, L, v["gene"], v["variant_cdna"], v["transcript"], hgvs_p=v["variant_protein"])
            grf = genereviews.genereviews_find(gr, L, v["gene"])
            tr = clinicaltrials.search_trials(http, L, term=v["gene"])
            nmd_inputs = {}
            if cf.get("ptc_codon"):
                tm = parse_feature_table(refseq_feature_table(http, v["transcript"]), v["transcript"])
                nmd_inputs = {"ptc_c_position": cf["ptc_c_position"], "last_exon_start_c": tm.tx_to_c(tm.exons[-1][0]),
                              "last_junction_c": tm.last_junction_c(), "cds_end_c": tm.tx_to_c(tm.cds_end), "n_exons": tm.n_exons}
            qs = L.queries
            report["cases"][f"{case['id']}:{v['gene']}"] = {
                "hgvs": f"{v['transcript']}:{v['variant_cdna']} {v['variant_protein']}",
                "variantvalidator_ok": (a.get("variantvalidator") or {}).get("ok"),
                "variantvalidator_warnings": (a.get("variantvalidator") or {}).get("warnings"),
                "protein_notation_consistent": a.get("protein_notation_consistent"),
                "computed_exon": f"{cf.get('exon_number')}/{cf.get('exon_total')}", "vep_exon": (a.get("vep") or {}).get("exon"),
                "exon_consistent_with_vep": a.get("exon_consistent_with_vep"),
                "vep_consequence": (a.get("vep") or {}).get("consequence_terms"), "computed_consequence": cf.get("consequence_class"),
                "nmd_prediction": cf.get("nmd_prediction"), "nmd_inputs": nmd_inputs,
                "uniprot_features_at_residue": (g.get("uniprot") or {}).get("features_at_position"),
                "clinvar": {"status": status(qs, "clinvar_lookup"), "term_used": cv.get("term_used"),
                            "records": [(r["key"], r["classification"], r["review_status"]) for r in cv.get("records", [])]},
                "clingen_validity": {"status": status(qs, "clingen") if not (g.get("clingen") or {}).get("validity_error") else "retrieval_failed",
                                     "curations": [(x["disease"], x["moi"], x["classification"]) for x in (g.get("clingen") or {}).get("validity", [])],
                                     "dosage": (g.get("clingen") or {}).get("dosage")},
                "gnomad": {"status": status(qs, "gnomad"), "pLI": (g.get("gnomad_constraint") or {}).get("pLI"),
                           "loeuf": (g.get("gnomad_constraint") or {}).get("oe_lof_upper"), "mis_z": (g.get("gnomad_constraint") or {}).get("mis_z")},
                "genereviews": {"status": "found" if grf.get("chapters_for_gene") else ("searched_not_found" if "error" not in grf else "unavailable"),
                                "chapters": [(c["nbk"], c["title"], c["last_updated"]) for c in grf.get("chapters_for_gene", [])],
                                "text_mentions": [(c["shortname"], c["mentions"]) for c in grf.get("chapters_mentioning", [])[:5]]},
                "open_trials_gene_term": {"status": status(qs, "search_trials"), "studies": [(x["nct_id"], x["overall_status"], x["study_type"]) for x in tr.get("studies", [])]},
            }
    INTERNAL.mkdir(parents=True, exist_ok=True)
    (INTERNAL / "scientific_checks.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report["cases"], indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
