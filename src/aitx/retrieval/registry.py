"""Tool registry exposed to the research agents (provider-neutral client-side tools).

All schemas are strict (every property required, no additional properties),
so tool inputs are schema-valid; "" / [] mean "not used".
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from ..config import Settings
from ..http import HttpClient
from ..ledger import EvidenceLedger
from . import clinicaltrials, drugs, europepmc, genereviews, ncbi, variant, web

log = logging.getLogger(__name__)


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
LS = {"type": "array", "items": {"type": "string"}}

TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "pubmed_search", "description": "Search PubMed (live E-utilities). Returns PMIDs with titles, year, journal, publication types. Use PubMed field tags, e.g. 'GENE[tiab] AND (treatment[tiab] OR therapy[tiab])'. Results are titles only: fetch abstracts before citing.",
     "input_schema": _obj({"query": S, "max_results": I, "page": I})},
    {"name": "pubmed_fetch_abstracts", "description": "Fetch title/abstract/publication types/PMCID for up to 20 PMIDs. Required before citing a PMID.",
     "input_schema": _obj({"pmids": LS})},
    {"name": "europepmc_search", "description": "Search Europe PMC (includes preprints and open-access flags). Useful for case reports and full-text availability. Query syntax e.g. '\"GENE\" AND \"case report\"'. page=1,2,... reviews further hits.",
     "input_schema": _obj({"query": S, "max_results": I, "page": I})},
    {"name": "europepmc_fulltext_excerpts", "description": "Open-access full text paragraphs from a PMC article that match keywords (e.g. dose, outcome, adverse). Use to verify human treatment precedent details.",
     "input_schema": _obj({"pmcid": S, "keywords": LS})},
    {"name": "variant_literature_search", "description": "Find articles that mention the patient's EXACT variant in any common notation (e.g. p.Arg123Trp, R123W, c.367C>T, rsID), searching Europe PMC full text (incl. tables of open-access papers) and PubMed titles/abstracts. A hit is not evidence that a reported individual's treatment response applies to this patient.",
     "input_schema": _obj({"gene": S, "notations": LS, "extra_terms": LS})},
    {"name": "clinvar_lookup", "description": "ClinVar records for a variant (gene + cDNA HGVS + transcript) or, with empty hgvs_c, pathogenic variants in the gene.",
     "input_schema": _obj({"gene": S, "hgvs_c": S, "transcript": S})},
    {"name": "search_trials", "description": "ClinicalTrials.gov API v2 search (live). open_only=true restricts to RECRUITING, NOT_YET_RECRUITING, ENROLLING_BY_INVITATION and AVAILABLE expanded access. Search by condition, free-text term (gene), and/or intervention.",
     "input_schema": _obj({"condition": S, "term": S, "intervention": S, "open_only": B, "page_token": S})},
    {"name": "get_trial", "description": "Full live ClinicalTrials.gov record: status, full eligibility criteria, ages, interventions, countries. Required before claiming eligibility.",
     "input_schema": _obj({"nct_id": S})},
    {"name": "fda_label_search", "description": "FDA structured product label (openFDA/DailyMed) by drug name, or labels whose INDICATIONS mention a text (gene/disease). sections: subset of indications_and_usage, contraindications, boxed_warning, warnings_and_cautions, warnings, pediatric_use, drug_interactions, pharmacogenomics, mechanism_of_action, dosage_and_administration ([] = all).",
     "input_schema": _obj({"drug": S, "indication_text": S, "sections": LS})},
    {"name": "genereviews_find", "description": "Find GeneReviews chapters for a gene (curated gene->chapter map) and chapters mentioning the gene or extra terms.",
     "input_schema": _obj({"gene": S, "extra_terms": LS})},
    {"name": "genereviews_read", "description": "Read GeneReviews chapter sections (by shortname or NBK id). sections = title keywords, e.g. ['Treatment of Manifestations','Agents/Circumstances to Avoid','Therapies Under Investigation','Genotype-Phenotype','Molecular Pathogenesis']; [] = default management + molecular sections. Returns text, cited PMIDs and chapter revision date.",
     "input_schema": _obj({"chapter": S, "sections": LS})},
    {"name": "gene_profile", "description": "ClinGen validity/dosage, gnomAD constraint (pLI, LOEUF, missense Z) and UniProt function/domains/features at an amino-acid position (0 = none) for any gene.",
     "input_schema": _obj({"gene": S, "aa_position": I})},
    {"name": "clinpgx_gene_annotations", "description": "ClinPGx (PharmGKB) gene record: whether the gene is a CPIC pharmacogene (e.g. anesthesia-relevant RYR1/CACNA1S).",
     "input_schema": _obj({"gene": S})},
    {"name": "fetch_url", "description": "Fetch an authoritative page (FDA, EMA, NIH, N=1 Collaborative, PMC, major journals; allow-listed). Optional keywords filter lines. Pages behind bot challenges are not accessed.",
     "input_schema": _obj({"url": S, "keywords": LS})},
]

TOOL_NAMES = [t["name"] for t in TOOL_SPECS]


class ToolBox:
    def __init__(self, http: HttpClient, ledger: EvidenceLedger, settings: Settings) -> None:
        self.http, self.ledger, self.settings = http, ledger, settings
        self.gr = genereviews.get_index(str(settings.genereviews_dir))
        self.calls: list[dict[str, Any]] = []
        self._fns: dict[str, Callable[..., dict[str, Any]]] = {
            "pubmed_search": lambda query, max_results, page: ncbi.pubmed_search(http, ledger, query, max_results or 20, page=page or 1),
            "pubmed_fetch_abstracts": lambda pmids: ncbi.pubmed_fetch_abstracts(http, ledger, pmids),
            "europepmc_search": lambda query, max_results, page: europepmc.europepmc_search(http, ledger, query, max_results or 20, page or 1),
            "europepmc_fulltext_excerpts": lambda pmcid, keywords: europepmc.europepmc_fulltext_excerpts(http, ledger, pmcid, keywords),
            "variant_literature_search": lambda gene, notations, extra_terms: europepmc.variant_literature_search(http, ledger, gene, notations, extra_terms or None),
            "clinvar_lookup": lambda gene, hgvs_c, transcript: ncbi.clinvar_lookup(http, ledger, gene, hgvs_c, transcript),
            "search_trials": lambda condition, term, intervention, open_only, page_token: clinicaltrials.search_trials(
                http, ledger, condition, term, intervention, open_only, page_token=page_token),
            "get_trial": lambda nct_id: clinicaltrials.get_trial(http, ledger, nct_id),
            "fda_label_search": lambda drug, indication_text, sections: drugs.fda_label_search(http, ledger, drug, indication_text, sections),
            "genereviews_find": lambda gene, extra_terms: genereviews.genereviews_find(self.gr, ledger, gene, extra_terms),
            "genereviews_read": lambda chapter, sections: genereviews.genereviews_read(self.gr, ledger, chapter, sections),
            "gene_profile": lambda gene, aa_position: variant.gene_profile(http, ledger, gene, aa_position or None),
            "clinpgx_gene_annotations": lambda gene: drugs.clinpgx_gene_annotations(http, ledger, gene),
            "fetch_url": lambda url, keywords: web.fetch_url(http, ledger, url, keywords),
        }

    def specs(self, names: list[str] | None = None, strict: bool = True) -> list[dict[str, Any]]:
        sel = [t for t in TOOL_SPECS if names is None or t["name"] in names]
        return [{**t, "strict": True} if strict else dict(t) for t in sel]

    def execute(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool; returns (json_text, is_error). Never raises."""
        fn = self._fns.get(name)
        if fn is None:
            return json.dumps({"error": f"unknown tool {name}"}), True
        try:
            result = fn(**args)
            is_error = isinstance(result, dict) and "error" in result and len(result) <= 2
        except TypeError as e:
            result, is_error = {"error": f"bad arguments: {e}"}, True
        except Exception as e:  # network errors etc. are reported to the model, not hidden
            log.warning("tool %s failed (%s)", name, type(e).__name__)
            if not (self.ledger.queries and self.ledger.queries[-1].tool == name and self.ledger.queries[-1].error):
                self.ledger.log_query(name, args, [], error=str(e)[:300])
            result, is_error = {"error": f"{type(e).__name__}: {str(e)[:300]} (retrieval FAILED: no conclusion about absence)"}, True
        if is_error and not (self.ledger.queries and self.ledger.queries[-1].tool == name and self.ledger.queries[-1].error):
            self.ledger.log_query(name, args, [], error=str(result.get("error", ""))[:300] if isinstance(result, dict) else "error")
        text = json.dumps(result, ensure_ascii=False, default=str)
        limit = self.settings.tool_result_char_limit
        if len(text) > limit:
            text = text[:limit] + f'... [TRUNCATED: {len(text) - limit} more chars; narrow the query or request fewer items]'
        self.calls.append({"tool": name, "args": args, "is_error": is_error, "chars": len(text)})
        return text, is_error
