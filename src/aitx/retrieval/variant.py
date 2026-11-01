"""Variant- and gene-level annotation: VariantValidator, Ensembl VEP, RefSeq
transcript model (deterministic features), gnomAD constraint, ClinGen,
UniProt."""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote

from ..http import HttpClient
from ..ledger import EvidenceLedger, EvidenceRecord
from ..variant_model import TranscriptModel, analyse_variant, parse_feature_table
from .ncbi import refseq_feature_table

log = logging.getLogger(__name__)


def variantvalidator(http: HttpClient, transcript: str, hgvs_c: str) -> dict[str, Any]:
    hgvs = f"{transcript}:{hgvs_c}"
    url = f"https://rest.variantvalidator.org/VariantValidator/variantvalidator/GRCh38/{quote(hgvs, safe='')}/mane_select"
    r = http.request("GET", url, params={"content-type": "application/json"})
    if r.status != 200:
        return {"ok": False, "error": f"HTTP {r.status}"}
    d = r.json()
    flag = d.get("flag")
    key = next((k for k in d if k not in ("flag", "metadata")), None)
    if not key:
        return {"ok": False, "flag": flag}
    v = d[key]
    loci = v.get("primary_assembly_loci", {}).get("grch38", {})
    return {
        "ok": flag == "gene_variant",
        "flag": flag,
        "hgvs_transcript_variant": v.get("hgvs_transcript_variant"),
        "predicted_protein": (v.get("hgvs_predicted_protein_consequence") or {}).get("tlr"),
        "gene_symbol": v.get("gene_symbol"),
        "genomic_grch38": loci.get("hgvs_genomic_description"),
        "vcf_grch38": loci.get("vcf"),
        "exonic_positions": v.get("variant_exonic_positions"),
        "warnings": v.get("validation_warnings", []),
        "url": f"https://variantvalidator.org/service/validate/?variant={quote(hgvs)}&genomebuild=GRCh38",
    }


def ensembl_vep(http: HttpClient, transcript: str, hgvs_c: str) -> dict[str, Any]:
    hgvs = f"{transcript}:{hgvs_c}"
    url = f"https://rest.ensembl.org/vep/human/hgvs/{quote(hgvs, safe=':>')}"
    r = http.request("GET", url, params={"content-type": "application/json", "refseq": 1, "mane": 1, "canonical": 1, "hgvs": 1,
                                          "numbers": 1, "protein": 1, "domains": 1, "uniprot": 1, "variant_class": 1})
    if r.status != 200:
        return {"ok": False, "error": f"HTTP {r.status}: {r.text[:200]}"}
    d = r.json()
    if not d:
        return {"ok": False, "error": "empty"}
    v = d[0]
    tcs = v.get("transcript_consequences", [])
    base = transcript.split(".")[0]
    match = next((t for t in tcs if t.get("transcript_id", "").split(".")[0] == base), tcs[0] if tcs else {})
    colocated = []
    for cv in v.get("colocated_variants", []) or []:
        if cv.get("somatic"):
            continue
        colocated.append({"id": cv.get("id"), "clin_sig": cv.get("clin_sig"), "clinvar": (cv.get("var_synonyms") or {}).get("ClinVar", []),
                          "frequencies": cv.get("frequencies")})
    return {
        "ok": True,
        "most_severe_consequence": v.get("most_severe_consequence"),
        "variant_class": v.get("variant_class"),
        "location": f"chr{v.get('seq_region_name')}:{v.get('start')}-{v.get('end')} (GRCh38)",
        "transcript_id": match.get("transcript_id"),
        "consequence_terms": match.get("consequence_terms"),
        "exon": match.get("exon"),
        "intron": match.get("intron"),
        "protein_start": match.get("protein_start"),
        "amino_acids": match.get("amino_acids"),
        "hgvsp": match.get("hgvsp"),
        "sift": match.get("sift_prediction"),
        "polyphen": match.get("polyphen_prediction"),
        "mane_select": match.get("mane_select"),
        "domains": [f"{x.get('db')}:{x.get('name')}" for x in (match.get("domains") or [])][:12],
        "colocated_germline": colocated,
        "url": f"https://rest.ensembl.org/vep/human/hgvs/{quote(hgvs, safe=':>')}?content-type=application/json&refseq=1",
    }


def gnomad_constraint(http: HttpClient, gene: str) -> dict[str, Any]:
    q = {"query": "query($g:String!){ gene(gene_symbol:$g, reference_genome:GRCh38){ gene_id gnomad_constraint{ pLI oe_lof oe_lof_lower oe_lof_upper oe_mis mis_z lof_z } } }",
         "variables": {"g": gene}}
    r = http.request("POST", "https://gnomad.broadinstitute.org/api", json_body=q, headers={"Content-Type": "application/json"})
    if r.status == 200 and (r.json().get("errors") or not (r.json().get("data") or {}).get("gene")):
        r = http.request("POST", "https://gnomad.broadinstitute.org/api", json_body=q, headers={"Content-Type": "application/json"}, use_cache=False)
    if r.status != 200:
        return {"ok": False, "error": f"HTTP {r.status}"}
    if r.json().get("errors"):
        return {"ok": False, "error": str(r.json()["errors"])[:300]}
    d = (r.json().get("data") or {}).get("gene") or {}
    return {"ok": bool(d), "gene_id": d.get("gene_id"), **(d.get("gnomad_constraint") or {}),
            "url": f"https://gnomad.broadinstitute.org/gene/{d.get('gene_id', gene)}?dataset=gnomad_r4"}


_CLINGEN_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _clingen_table(http: HttpClient, kind: str) -> list[dict]:
    hit = _CLINGEN_CACHE.get(kind)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    d = http.get_json(f"https://search.clinicalgenome.org/api/{kind}", params={"queryParams": ""})
    rows = d.get("rows", [])
    _CLINGEN_CACHE[kind] = (time.time(), rows)
    return rows


def clingen_gene(http: HttpClient, gene: str) -> dict[str, Any]:
    out: dict[str, Any] = {"validity": [], "dosage": None}
    try:
        for r in _clingen_table(http, "validity"):
            if r.get("symbol") == gene:
                out["validity"].append({"disease": (r.get("disease_name") or "").strip(), "moi": r.get("moi"), "classification": r.get("classification"),
                                        "mondo": r.get("mondo"), "released": r.get("released"), "expert_panel": r.get("ep")})
                out["hgnc"] = r.get("hgnc_id")
    except Exception as e:  # pragma: no cover - network
        out["validity_error"] = str(e)
    try:
        for r in _clingen_table(http, "dosage"):
            if r.get("symbol") == gene:
                out["dosage"] = {"haploinsufficiency_score": r.get("haplo_assertion"), "triplosensitivity_score": r.get("triplo_assertion"),
                                 "haplo_disease": r.get("haplo_disease"), "date": r.get("date")}
                out["hgnc"] = out.get("hgnc") or r.get("hgnc_id")
    except Exception as e:  # pragma: no cover
        out["dosage_error"] = str(e)
    return out


def uniprot_entry(http: HttpClient, gene: str, position: int | None = None) -> dict[str, Any]:
    r = http.request("GET", "https://rest.uniprot.org/uniprotkb/search",
                     params={"query": f"gene_exact:{gene} AND organism_id:9606 AND reviewed:true", "format": "json", "size": 1,
                             "fields": "accession,protein_name,cc_function,cc_subcellular_location,cc_disease,ft_domain,ft_region,ft_topo_dom,ft_transmem,ft_binding,ft_variant,ft_mutagen,length"})
    if r.status != 200:
        return {"ok": False, "error": f"HTTP {r.status}"}
    res = r.json().get("results", [])
    if not res:
        return {"ok": False, "error": "no reviewed entry"}
    e = res[0]
    acc = e.get("primaryAccession")
    comments = e.get("comments", [])

    def ctext(ctype: str) -> list[str]:
        out = []
        for c in comments:
            if c.get("commentType") == ctype:
                for t in c.get("texts", []) or []:
                    out.append(t.get("value", ""))
                if ctype == "DISEASE" and c.get("disease"):
                    dz = c["disease"]
                    out.append(f"{dz.get('diseaseId')}: {dz.get('description', '')}")
                if ctype == "SUBCELLULAR LOCATION":
                    for loc in c.get("subcellularLocations", []) or []:
                        out.append(loc.get("location", {}).get("value", ""))
        return [o for o in out if o]

    feats_at = []
    domains = []
    for f in e.get("features", []):
        loc = f.get("location", {})
        s, t = loc.get("start", {}).get("value"), loc.get("end", {}).get("value")
        desc = f"{f.get('type')}: {f.get('description', '')} [{s}-{t}]"
        if f.get("type") in ("Domain", "Region", "Transmembrane", "Topological domain"):
            domains.append(desc)
        if position and s and t and s <= position <= t and f.get("type") in ("Domain", "Region", "Transmembrane", "Topological domain", "Binding site", "Natural variant", "Mutagenesis"):
            extra = ""
            if f.get("type") in ("Natural variant", "Mutagenesis"):
                alt = f.get("alternativeSequence", {})
                extra = f" {alt.get('originalSequence', '')}->{','.join(alt.get('alternativeSequences', []))}"
            feats_at.append(desc + extra)
    return {
        "ok": True, "accession": acc,
        "protein_name": ((e.get("proteinDescription") or {}).get("recommendedName") or {}).get("fullName", {}).get("value"),
        "length": (e.get("sequence") or {}).get("length"),
        "function": ctext("FUNCTION")[:3], "subcellular_location": ctext("SUBCELLULAR LOCATION")[:6], "disease": ctext("DISEASE")[:6],
        "features_at_position": feats_at[:15], "domains": domains[:25], "url": f"https://www.uniprot.org/uniprotkb/{acc}/entry",
    }


def annotate_variant(http: HttpClient, ledger: EvidenceLedger, gene: str, transcript: str, variant_cdna: str, variant_protein: str) -> dict[str, Any]:
    """One-shot deterministic annotation bundle for a variant; registers ledger records."""
    out: dict[str, Any] = {"gene": gene, "transcript": transcript, "variant_cdna": variant_cdna, "variant_protein": variant_protein}
    keys: list[str] = []
    # Transcript model + computed features
    tm: TranscriptModel | None = None
    if transcript.startswith(("NM_", "XM_", "NR_")):
        try:
            tm = parse_feature_table(refseq_feature_table(http, transcript), transcript)
            ledger.add(EvidenceRecord(key=f"REFSEQ:{transcript}", source_type="refseq", title=f"RefSeq {transcript} ({tm.gene})", url=tm.source_url,
                                      content=f"{tm.n_exons} exons; CDS {tm.cds_start}-{tm.cds_end} (transcript coordinates); protein length {tm.protein_length} aa.",
                                      content_scope="structured_record", retrieved_by="annotate_variant"))
            keys.append(f"REFSEQ:{transcript}")
        except Exception as e:
            out["transcript_model_error"] = str(e)
            ledger.log_query("refseq_feature_table", {"transcript": transcript}, [], error=str(e)[:200])
    try:
        feats = analyse_variant(variant_cdna, variant_protein, tm)
        out["computed_features"] = feats.as_dict()
        ck = f"COMPUTED:{gene}:{variant_cdna}"
        ledger.add(EvidenceRecord(
            key=ck, source_type="computed", title=f"Computed transcript-level analysis of {transcript}:{variant_cdna}",
            url=tm.source_url if tm else f"https://www.ncbi.nlm.nih.gov/nuccore/{transcript}",
            content=feats.summary() + (" Notes: " + " ".join(feats.notes) if feats.notes else "") +
                    " (Rule-based prediction from RefSeq exon structure; NMD rules: last-exon and ~50-55 nt rule.)",
            content_scope="computed", retrieved_by="annotate_variant",
            metadata={"label": f"Transcript analysis of {variant_cdna} on {transcript} (computed from RefSeq record)"}))
        keys.append(ck)
    except Exception as e:
        out["computed_features_error"] = str(e)
    # VariantValidator (nomenclature check)
    try:
        vv = variantvalidator(http, transcript, variant_cdna)
        out["variantvalidator"] = vv
        if vv.get("ok"):
            k = f"VARIANT:VV:{transcript}:{variant_cdna}"
            ledger.add(EvidenceRecord(key=k, source_type="variant", title=f"VariantValidator {transcript}:{variant_cdna}", url=vv["url"],
                                      content=json.dumps({x: vv.get(x) for x in ("hgvs_transcript_variant", "predicted_protein", "genomic_grch38", "exonic_positions", "warnings")}),
                                      content_scope="structured_record", retrieved_by="annotate_variant", metadata={"label": "VariantValidator"}))
            keys.append(k)
            pred = (vv.get("predicted_protein") or "")
            given = variant_protein.replace("(", "").replace(")", "")
            pred_n = pred.split(":")[-1].replace("(", "").replace(")", "")
            out["protein_notation_consistent"] = (not given) or pred_n == given
    except Exception as e:
        out["variantvalidator_error"] = str(e)
        ledger.log_query("variantvalidator", {"hgvs": f"{transcript}:{variant_cdna}"}, [], error=str(e)[:200])
    # VEP
    try:
        vep = ensembl_vep(http, transcript, variant_cdna)
        out["vep"] = vep
        if vep.get("ok"):
            k = f"VARIANT:VEP:{transcript}:{variant_cdna}"
            ledger.add(EvidenceRecord(key=k, source_type="variant", title=f"Ensembl VEP {transcript}:{variant_cdna}", url=vep["url"],
                                      content=json.dumps({x: vep.get(x) for x in ("most_severe_consequence", "consequence_terms", "exon", "intron", "hgvsp", "sift", "polyphen", "domains", "colocated_germline", "location")}),
                                      content_scope="structured_record", retrieved_by="annotate_variant", metadata={"label": "Ensembl VEP"}))
            keys.append(k)
    except Exception as e:
        out["vep_error"] = str(e)
        ledger.log_query("ensembl_vep", {"hgvs": f"{transcript}:{variant_cdna}"}, [], error=str(e)[:200])
    # deterministic cross-check: computed exon vs Ensembl VEP exon
    cf, vep = out.get("computed_features") or {}, out.get("vep") or {}
    if cf.get("exon_number") and vep.get("exon"):
        vep_exon = str(vep["exon"]).split("/")[0]
        out["exon_consistent_with_vep"] = vep_exon == str(cf["exon_number"])
        if not out["exon_consistent_with_vep"]:
            out.setdefault("warnings", []).append(f"Computed exon {cf['exon_number']} differs from VEP exon {vep['exon']}; treat exon-level features with caution.")
    ledger.log_query("annotate_variant", {"gene": gene, "transcript": transcript, "variant_cdna": variant_cdna}, keys)
    out["ledger_keys"] = keys
    return out


def gene_profile(http: HttpClient, ledger: EvidenceLedger, gene: str, aa_position: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"gene": gene}
    keys = []
    try:
        cg = clingen_gene(http, gene)
        out["clingen"] = cg
        err = cg.get("validity_error") or cg.get("dosage_error") or ""
        ledger.log_query("clingen", {"gene": gene}, [f"CLINGEN:{gene}"] if (cg.get("validity") or cg.get("dosage")) else [], error=err)
        if cg.get("validity") or cg.get("dosage"):
            k = f"CLINGEN:{gene}"
            hg = cg.get("hgnc") or gene
            ledger.add(EvidenceRecord(key=k, source_type="clingen", title=f"ClinGen curations for {gene}",
                                      url=f"https://search.clinicalgenome.org/kb/genes/{hg}", content=json.dumps(cg)[:4000],
                                      content_scope="structured_record", retrieved_by="gene_profile", metadata={"label": f"gene curation ({gene})"}))
            keys.append(k)
    except Exception as e:
        out["clingen_error"] = str(e)
        ledger.log_query("clingen", {"gene": gene}, [], error=str(e)[:200])
    try:
        gc = gnomad_constraint(http, gene)
        out["gnomad_constraint"] = gc
        ledger.log_query("gnomad", {"gene": gene}, [f"GNOMAD:{gene}"] if gc.get("ok") else [], error="" if gc.get("ok") else gc.get("error", "no data"))
        if gc.get("ok"):
            k = f"GNOMAD:{gene}"
            ledger.add(EvidenceRecord(key=k, source_type="gnomad", title=f"gnomAD v4 constraint for {gene}", url=gc["url"],
                                      content=json.dumps({x: gc.get(x) for x in ("pLI", "oe_lof", "oe_lof_upper", "oe_mis", "mis_z", "lof_z")}),
                                      content_scope="structured_record", retrieved_by="gene_profile", metadata={"gene": gene}))
            keys.append(k)
    except Exception as e:
        out["gnomad_error"] = str(e)
        ledger.log_query("gnomad", {"gene": gene}, [], error=str(e)[:200])
    try:
        up = uniprot_entry(http, gene, aa_position)
        out["uniprot"] = up
        ledger.log_query("uniprot", {"gene": gene}, [f"UNIPROT:{up['accession']}"] if up.get("ok") else [],
                         error="" if up.get("ok") or up.get("error") == "no reviewed entry" else up.get("error", ""))
        if up.get("ok"):
            k = f"UNIPROT:{up['accession']}"
            ledger.add(EvidenceRecord(key=k, source_type="uniprot", title=f"UniProtKB {up['accession']} ({up.get('protein_name')})", url=up["url"],
                                      content=json.dumps({x: up.get(x) for x in ("function", "subcellular_location", "disease", "features_at_position", "domains")})[:6000],
                                      content_scope="structured_record", retrieved_by="gene_profile"))
            keys.append(k)
    except Exception as e:
        out["uniprot_error"] = str(e)
        ledger.log_query("uniprot", {"gene": gene}, [], error=str(e)[:200])
    ledger.log_query("gene_profile", {"gene": gene, "aa_position": aa_position}, keys)
    out["ledger_keys"] = keys
    return out
