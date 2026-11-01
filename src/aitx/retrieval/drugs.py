"""Drug labelling (openFDA / DailyMed) and pharmacogenomics (ClinPGx, formerly PharmGKB)."""

from __future__ import annotations

from typing import Any

from ..http import HttpClient
from ..ledger import EvidenceLedger, EvidenceRecord

LABEL_SECTIONS = ("indications_and_usage", "contraindications", "boxed_warning", "warnings_and_cautions", "warnings",
                  "use_in_specific_populations", "pediatric_use", "drug_interactions", "pharmacogenomics", "clinical_pharmacology",
                  "mechanism_of_action", "dosage_and_administration")


def _first(v: Any) -> str:
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v or "")


def _register_label(ledger: EvidenceLedger, d: dict[str, Any], fetched_at: str, focus: list[str]) -> dict[str, Any]:
    of = d.get("openfda", {})
    set_id = d.get("set_id")
    name = ", ".join(of.get("generic_name", []) or of.get("brand_name", []) or ["unknown"])
    sections = {s: _first(d.get(s))[:6000] for s in LABEL_SECTIONS if d.get(s)}
    content = "\n".join(f"[{s.upper()}] {t[:2500]}" for s, t in sections.items() if (not focus or s in focus))
    key = f"FDALABEL:{set_id}"
    ledger.add(EvidenceRecord(
        key=key, source_type="fda_label", title=f"FDA label: {name} ({', '.join(of.get('brand_name', [])[:3])})",
        url=f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}", content=content, content_scope="full_text_excerpt",
        retrieved_by="fda_label_search", accessed_at=fetched_at,
        metadata={"drug": name, "brand": of.get("brand_name", [])[:3], "effective_time": d.get("effective_time"), "manufacturer": of.get("manufacturer_name", [])[:2]},
    )).verified_exists = True
    return {"key": key, "drug": name, "brand": of.get("brand_name", [])[:3], "effective_time": d.get("effective_time"),
            "sections": {s: t[:1800] for s, t in sections.items() if (not focus or s in focus)}}


def fda_label_search(http: HttpClient, ledger: EvidenceLedger, drug: str = "", indication_text: str = "", sections: list[str] | None = None, limit: int = 3) -> dict[str, Any]:
    """Search FDA structured product labels by drug name or by text in the indications section."""
    if drug:
        search = f'(openfda.generic_name:"{drug}" OR openfda.brand_name:"{drug}" OR openfda.substance_name:"{drug}")'
    elif indication_text:
        search = f'indications_and_usage:"{indication_text}"'
    else:
        return {"error": "provide drug or indication_text"}
    r = http.request("GET", "https://api.fda.gov/drug/label.json", params={"search": search, "limit": max(1, min(int(limit), 10))}, ok_statuses=(200, 404))
    if r.status == 404:
        ledger.log_query("fda_label_search", {"drug": drug, "indication_text": indication_text}, [])
        return {"results": [], "note": "No FDA label matched."}
    if r.status != 200:
        return {"error": f"HTTP {r.status}"}
    focus = [s for s in (sections or []) if s in LABEL_SECTIONS]
    res = [_register_label(ledger, d, r.fetched_at, focus) for d in r.json().get("results", [])]
    ledger.log_query("fda_label_search", {"drug": drug, "indication_text": indication_text}, [x["key"] for x in res])
    return {"results": res, "accessed_at": r.fetched_at}


def clinpgx_gene_annotations(http: HttpClient, ledger: EvidenceLedger, gene: str) -> dict[str, Any]:
    """Clinical guideline / label annotations for a pharmacogene (only relevant if the diagnostic gene is a pharmacogene)."""
    r = http.request("GET", "https://api.clinpgx.org/v1/data/gene", params={"symbol": gene, "view": "base"})
    if r.status != 200:
        ledger.log_query("clinpgx_gene_annotations", {"gene": gene}, [], error=f"HTTP {r.status}")
        return {"found": False, "note": f"ClinPGx returned HTTP {r.status}"}
    data = r.json().get("data", [])
    if not data:
        ledger.log_query("clinpgx_gene_annotations", {"gene": gene}, [])
        return {"found": False}
    g = data[0]
    pa = g.get("id")
    key = f"CLINPGX:{pa}"
    ledger.add(EvidenceRecord(key=key, source_type="clinpgx", title=f"ClinPGx gene {gene} ({pa})", url=f"https://www.clinpgx.org/gene/{pa}",
                              content=f"cpicGene={g.get('cpicGene')}; hasVariantAnnotation={g.get('hasVariantAnnotation')}; pharmVarGene={g.get('pharmVarGene')}",
                              content_scope="structured_record", retrieved_by="clinpgx_gene_annotations"))
    ledger.log_query("clinpgx_gene_annotations", {"gene": gene}, [key])
    return {"found": True, "key": key, "id": pa, "cpic_gene": g.get("cpicGene"), "has_variant_annotation": g.get("hasVariantAnnotation")}
