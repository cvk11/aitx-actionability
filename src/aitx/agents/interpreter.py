"""Agent A — deterministic bootstrap + clinical/genomic interpretation."""

from __future__ import annotations

import json
import logging
from typing import Any

from .. import prompts
from ..http import HttpClient
from ..ledger import EvidenceLedger
from ..retrieval import clinicaltrials, europepmc, genereviews, ncbi, variant
from ..retrieval.registry import ToolBox
from ..schemas import Interpretation
from .context import CaseContext

log = logging.getLogger(__name__)

INTERPRETER_TOOLS = ["pubmed_search", "pubmed_fetch_abstracts", "europepmc_search", "clinvar_lookup", "genereviews_find", "genereviews_read",
                     "gene_profile", "fetch_url"]


def bootstrap(ctx: CaseContext, http: HttpClient, ledger: EvidenceLedger, toolbox: ToolBox) -> dict[str, Any]:
    """Deterministic evidence collection (no LLM). Failures are recorded, not fatal."""
    dossier: dict[str, Any] = {"variants": [], "genes": {}, "clinvar": [], "genereviews": {}, "gene_summary": {}, "errors": []}
    genes: dict[str, int | None] = {}
    for v in ctx.case.patient.genotype:
        try:
            a = variant.annotate_variant(http, ledger, v.gene, v.transcript, v.variant_cdna, v.variant_protein)
        except Exception as e:  # pragma: no cover - network
            a = {"error": str(e)}
            dossier["errors"].append(f"annotate {v.gene}: {e}")
        dossier["variants"].append(a)
        try:  # variant-centric literature (full text + tables), so variant-specific reports are not missed
            from ..evidence import variant_notations

            rs = [c["id"] for c in ((a.get("vep") or {}).get("colocated_germline") or []) if str(c.get("id", "")).startswith("rs")]
            dossier.setdefault("variant_literature", []).append(
                europepmc.variant_literature_search(http, ledger, v.gene, sorted(variant_notations(v.variant_cdna, v.variant_protein, rs))))
        except Exception as e:  # pragma: no cover - network
            dossier["errors"].append(f"variant literature {v.gene}: {e}")
            ledger.log_query("variant_literature_search", {"gene": v.gene}, [], error=str(e)[:200])
        genes.setdefault(v.gene, (a.get("computed_features") or {}).get("aa_position"))
        try:
            dossier["clinvar"].append(ncbi.clinvar_lookup(http, ledger, v.gene, v.variant_cdna, v.transcript, hgvs_p=v.variant_protein,
                                                       genomic_vcf=_vcf_str(a.get("variantvalidator") or {})))
        except Exception as e:  # pragma: no cover
            dossier["errors"].append(f"clinvar {v.gene}: {e}")
            ledger.log_query("clinvar_lookup", {"gene": v.gene, "hgvs_c": v.variant_cdna}, [], error=str(e)[:200])
    for g, pos in genes.items():
        for name, fn in (("genes", lambda: variant.gene_profile(http, ledger, g, pos)),
                         ("gene_summary", lambda: ncbi.ncbi_gene_summary(http, ledger, g)),
                         ("genereviews", lambda: genereviews.genereviews_find(toolbox.gr, ledger, g))):
            try:
                dossier[name][g] = fn()
            except Exception as e:  # pragma: no cover
                dossier["errors"].append(f"{name} {g}: {e}")
                ledger.log_query(name, {"gene": g}, [], error=str(e)[:200])
        try:  # mandatory therapeutic coverage (modulators of both directions, human treatment reports, nucleic-acid therapies)
            dossier.setdefault("therapeutic_coverage", {})[g] = ncbi.therapeutic_coverage_search(http, ledger, g)
        except Exception as e:  # pragma: no cover
            dossier["errors"].append(f"coverage {g}: {type(e).__name__}")
        try:
            dossier.setdefault("trials_by_gene", {})[g] = clinicaltrials.search_trials(http, ledger, term=g, open_only=True)
        except Exception as e:  # pragma: no cover
            dossier["errors"].append(f"trials {g}: {e}")
            ledger.log_query("search_trials", {"term": g}, [], error=str(e)[:200])
    dossier["bootstrap_keys"] = ledger.keys()
    dossier["retrieval_status"] = ledger.retrieval_status()
    return dossier


def _vcf_str(vv: dict) -> str:
    """'NC_xxxxxx.v-pos-ref-alt' from VariantValidator (accession version preserved; '' if unavailable)."""
    v = vv.get("vcf_grch38") or {}
    acc = (vv.get("genomic_grch38") or "").split(":")[0]
    return f"{acc}-{v['pos']}-{v['ref']}-{v['alt']}" if acc and all(k in v for k in ("pos", "ref", "alt")) else ""


def dossier_text(d: dict[str, Any], limit: int = 30000) -> str:
    slim = {
        "variants": d.get("variants"),
        "clinvar": d.get("clinvar"),
        "genes": d.get("genes"),
        "gene_summary": {g: {k: v.get(k) for k in ("key", "name", "description", "summary")} for g, v in (d.get("gene_summary") or {}).items()},
        "genereviews": d.get("genereviews"),
        "variant_literature (articles mentioning the exact variant; read before citing)": [
            {"europepmc": [{k: r.get(k) for k in ("key", "title", "year", "pmcid", "open_access")} for r in (x.get("europepmc") or [])[:15]],
             "pubmed": [{k: r.get(k) for k in ("key", "title", "year")} for r in (x.get("pubmed") or [])[:10]]} for x in d.get("variant_literature", [])],
        "therapeutic_coverage (mandatory queries; every hit must be assessed or explicitly set aside)": {
            g: {fam: {"query": f["query"], "total": f["total"], "error": f["error"],
                      "hits": [{"key": k, "title": f["titles"].get(k, "")} for k in f["keys"][:20]]} for fam, f in fams.items()}
            for g, fams in (d.get("therapeutic_coverage") or {}).items()},
        "open_trials_by_gene_term": {g: [{k: s.get(k) for k in ("nct_id", "title", "overall_status", "study_type")} for s in v.get("studies", [])]
                                     for g, v in (d.get("trials_by_gene") or {}).items()},
        "errors": d.get("errors"),
        "retrieval_status (with_results / empty = searched, no record / failed = could not query)": d.get("retrieval_status"),
    }
    return json.dumps(slim, indent=1, default=str)[:limit]


def interpret(ctx: CaseContext, llm: Any, toolbox: ToolBox, max_rounds: int) -> Interpretation:
    system = prompts.render("interpreter")
    user = (f"PATIENT:\n{ctx.patient_json()}\n\nPARSED CLINICAL FACTS: age_years={ctx.clinical.age_years} (from '{ctx.clinical.age_text}'), sex={ctx.clinical.sex}\n\n"
            f"DETERMINISTIC DOSSIER (already in the evidence ledger; cite the keys shown):\n{dossier_text(ctx.dossier)}")
    out = llm.agent(stage="A_interpret", system=system, user=user, output_model=Interpretation, toolbox=toolbox,
                    tool_names=INTERPRETER_TOOLS, max_rounds=max_rounds)
    # deterministic cross-check of demographics
    if ctx.clinical.age_years is not None and abs((out.patient_age_years or -1) - ctx.clinical.age_years) > 0.05:
        log.info("age from deterministic parser overrides the model value")
        out.patient_age_years = ctx.clinical.age_years
    if ctx.clinical.sex != "unknown" and out.patient_sex != ctx.clinical.sex:
        out.patient_sex = ctx.clinical.sex  # type: ignore[assignment]
    return out
