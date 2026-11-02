"""NCBI E-utilities: PubMed, ClinVar, RefSeq feature tables, NCBI Gene."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ..http import HttpClient
from ..ledger import EvidenceLedger, EvidenceRecord

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def pubmed_search(http: HttpClient, ledger: EvidenceLedger, query: str, max_results: int = 20, sort: str = "relevance", page: int = 1) -> dict[str, Any]:
    max_results = max(1, min(int(max_results), 50))
    page = max(1, int(page or 1))
    retstart = (page - 1) * max_results
    d = http.get_json(f"{EUTILS}/esearch.fcgi", params={"db": "pubmed", "term": query, "retmode": "json", "retmax": max_results, "sort": sort,
                                                        "retstart": retstart})
    res = d.get("esearchresult", {})
    ids = res.get("idlist", [])
    total = int(res.get("count", 0))
    summaries = pubmed_summaries(http, ledger, ids) if ids else []
    # coverage: reviewed hits are those up to the end of this page
    ledger.log_query("pubmed_search", {"query": query, "page": page}, [f"PMID:{i}" for i in ids], total=max(0, total - retstart))
    return {"query": query, "page": page, "total_count": total, "returned": len(ids), "more_pages": retstart + len(ids) < total,
            "query_translation": res.get("querytranslation", ""), "results": summaries,
            "note": "Titles only. Call pubmed_fetch_abstracts on relevant PMIDs before citing them. Use page=2,3,... to review further hits."}


def pubmed_summaries(http: HttpClient, ledger: EvidenceLedger, pmids: list[str]) -> list[dict[str, Any]]:
    d = http.get_json(f"{EUTILS}/esummary.fcgi", params={"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})
    out = []
    result = d.get("result", {})
    for pid in result.get("uids", []):
        r = result[pid]
        rec = EvidenceRecord(
            key=f"PMID:{pid}", source_type="pubmed", title=r.get("title", ""), url=f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            content="", content_scope="metadata", retrieved_by="pubmed_search",
            metadata={"year": (r.get("pubdate") or "")[:4], "journal": r.get("source", ""), "pub_types": r.get("pubtype", []),
                      "first_author": (r.get("sortfirstauthor") or "")},
        )
        ledger.add(rec)
        out.append({"key": rec.key, "title": rec.title, "year": rec.metadata["year"], "journal": rec.metadata["journal"], "pub_types": rec.metadata["pub_types"]})
    return out


def parse_pubmed_xml(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    arts = []
    for pa in root.findall(".//PubmedArticle"):
        pmid = _text(pa.find(".//MedlineCitation/PMID"))
        art = pa.find(".//Article")
        title = _text(art.find("ArticleTitle")) if art is not None else ""
        parts = []
        for ab in pa.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            t = _text(ab)
            parts.append(f"{label}: {t}" if label else t)
        abstract = "\n".join(p for p in parts if p)
        journal = _text(pa.find(".//Journal/Title"))
        year = _text(pa.find(".//JournalIssue/PubDate/Year")) or _text(pa.find(".//JournalIssue/PubDate/MedlineDate"))[:4]
        ptypes = [_text(x) for x in pa.findall(".//PublicationTypeList/PublicationType")]
        mesh = [_text(x.find("DescriptorName")) for x in pa.findall(".//MeshHeadingList/MeshHeading")]
        ids = {x.get("IdType"): _text(x) for x in pa.findall(".//PubmedData/ArticleIdList/ArticleId")}
        retracted = any("Retract" in t for t in ptypes) or bool(pa.findall(".//CommentsCorrections[@RefType='RetractionIn']"))
        arts.append({"pmid": pmid, "title": title, "abstract": abstract, "journal": journal, "year": year, "pub_types": ptypes,
                     "mesh": mesh[:15], "doi": ids.get("doi", ""), "pmcid": ids.get("pmc", ""), "retracted": retracted})
    return arts


def pubmed_fetch_abstracts(http: HttpClient, ledger: EvidenceLedger, pmids: list[str]) -> dict[str, Any]:
    pmids = [str(p).replace("PMID:", "").strip() for p in pmids][:20]
    r = http.request("GET", f"{EUTILS}/efetch.fcgi", params={"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
    arts = parse_pubmed_xml(r.text) if r.status == 200 else []
    out = []
    found = set()
    for a in arts:
        found.add(a["pmid"])
        rec = ledger.add(EvidenceRecord(
            key=f"PMID:{a['pmid']}", source_type="pubmed", title=a["title"], url=f"https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/",
            content=a["abstract"] or "(no abstract available)", content_scope="abstract" if a["abstract"] else "metadata",
            retrieved_by="pubmed_fetch_abstracts", accessed_at=r.fetched_at,
            metadata={"year": a["year"], "journal": a["journal"], "pub_types": a["pub_types"], "doi": a["doi"], "pmcid": a["pmcid"],
                      "mesh": a["mesh"], "retracted": a["retracted"]},
        ))
        rec.verified_exists = True
        out.append({"key": f"PMID:{a['pmid']}", "title": a["title"], "year": a["year"], "journal": a["journal"], "pub_types": a["pub_types"],
                    "pmcid": a["pmcid"], "retracted": a["retracted"], "abstract": a["abstract"]})
    missing = [p for p in pmids if p not in found]
    ledger.log_query("pubmed_fetch_abstracts", {"pmids": pmids}, [f"PMID:{a['pmid']}" for a in arts])
    return {"articles": out, "not_found": missing}


def pmid_exists(http: HttpClient, pmids: list[str]) -> dict[str, dict[str, Any]]:
    """Deterministic existence check (used by the verifier)."""
    pmids = [p.replace("PMID:", "") for p in pmids]
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(pmids), 100):
        chunk = pmids[i : i + 100]
        d = http.get_json(f"{EUTILS}/esummary.fcgi", params={"db": "pubmed", "id": ",".join(chunk), "retmode": "json"})
        res = d.get("result", {})
        for p in chunk:
            r = res.get(p)
            out[p] = {"exists": bool(r) and "error" not in r, "title": (r or {}).get("title", ""), "pubtype": (r or {}).get("pubtype", [])}
    return out


# ------------------------------------------------------------------ ClinVar

def clinvar_lookup(http: HttpClient, ledger: EvidenceLedger, gene: str, hgvs_c: str = "", transcript: str = "", max_results: int = 10,
                   hgvs_p: str = "", genomic_vcf: str = "") -> dict[str, Any]:
    """ClinVar records for exactly this variant.

    A record counts as evidence for the patient's variant only if its title denotes the same cDNA change on the
    SAME transcript version, or on another version of the same transcript AND its GRCh38 allele (canonical SPDI)
    equals the patient's genomic variant (`genomic_vcf` "chr-pos-ref-alt", e.g. from VariantValidator) after
    identical minimal left-aligned normalisation. Otherwise it is reported as a
    `version_mismatch_candidate` / `non_matching_hit` and never registered as evidence."""
    terms = []
    if transcript and hgvs_c:
        terms.append(f'"{transcript}:{hgvs_c}"')
        base_tx = transcript.split(".")[0]
        terms.append(f'"{base_tx}"[All Fields] AND "{hgvs_c}"[All Fields] AND {gene}[gene]')
    if hgvs_c:
        terms.append(f'{gene}[gene] AND "{hgvs_c}"')
    if hgvs_p:
        terms.append(f'{gene}[gene] AND "{hgvs_p.replace("(", "").replace(")", "")}"')
    if not terms:
        terms.append(f"{gene}[gene] AND (pathogenic[clinsig] OR likely_pathogenic[clinsig])")
    out: list[dict[str, Any]] = []
    non_matching: list[dict[str, str]] = []
    mismatch: list[dict[str, Any]] = []
    gpos = _norm_vcf(genomic_vcf) if genomic_vcf else None
    genomic_tried = bool(genomic_vcf)
    used = ""
    tried = []
    for t in terms:
        d = http.get_json(f"{EUTILS}/esearch.fcgi", params={"db": "clinvar", "term": t, "retmode": "json", "retmax": max_results})
        ids = d.get("esearchresult", {}).get("idlist", [])
        tried.append(t)
        if not ids:
            continue
        used = t
        d = http.get_json(f"{EUTILS}/esummary.fcgi", params={"db": "clinvar", "id": ",".join(ids), "retmode": "json"})
        res = d.get("result", {})
        for uid in res.get("uids", []):
            r = res[uid]
            gc = r.get("germline_classification", {}) or {}
            traits = [t.get("trait_name") for t in gc.get("trait_set", []) if t.get("trait_name")]
            acc = r.get("accession", f"VCV{uid}")
            title = r.get("title", "")
            kind = "gene_level_listing" if not hgvs_c else _clinvar_title_matches(title, hgvs_c, transcript)
            if kind == "version_differs":
                if gpos is None and not genomic_tried:
                    genomic_tried = True
                    gpos = _resolve_genomic(http, transcript, hgvs_c)
                loc = _clinvar_grch38(r)
                if gpos is not None and loc is not None and loc == gpos:
                    kind = "genomic_equivalent_other_transcript_version"
                else:
                    mismatch.append({"accession": acc, "title": title, "clinvar_grch38": loc, "patient_grch38": gpos,
                                     "reason": "same cDNA notation on another transcript version; genomic equivalence not established"})
                    continue
            row = {"key": f"CLINVAR:{acc}", "title": title, "classification": gc.get("description"), "review_status": gc.get("review_status"),
                   "last_evaluated": gc.get("last_evaluated"), "conditions": traits, "protein_change": r.get("protein_change"), "match": kind}
            if kind is False:
                # loose search hits for OTHER variants are reported but never registered as evidence for this variant
                non_matching.append({"accession": acc, "title": title})
                continue
            content = (f"{title}; germline classification: {gc.get('description')} (review status: {gc.get('review_status')}; "
                       f"last evaluated {gc.get('last_evaluated')}); conditions: {', '.join(traits) or 'n/a'}; "
                       f"molecular consequence: {', '.join(r.get('molecular_consequence_list', []))}")
            ledger.add(EvidenceRecord(
                key=f"CLINVAR:{acc}", source_type="clinvar", title=title,
                url=f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{uid}/", content=content, content_scope="structured_record",
                retrieved_by="clinvar_lookup", metadata={"classification": gc.get("description"), "review_status": gc.get("review_status"),
                                                         "conditions": traits, "match": kind},
            ))
            out.append(row)
        if out:
            break
    ledger.log_query("clinvar_lookup", {"gene": gene, "hgvs_c": hgvs_c, "transcript": transcript, "term_used": used}, [o["key"] for o in out])
    note = ("No ClinVar record whose title matches this exact cDNA change was found (searched; this is not evidence that the "
            "variant is benign or unreported elsewhere)." if hgvs_c and not out else "")
    return {"term_used": used, "terms_tried": tried, "records": out, "non_matching_hits": non_matching[:5],
            "version_mismatch_candidates": mismatch[:5], "note": note}


def _norm_c(h: str) -> str:
    h = h.split(":")[-1].replace("(", "").replace(")", "")
    return re.sub(r"(?<=del|dup|inv)[ACGTN]+", "", h)


def _clinvar_title_matches(title: str, hgvs_c: str, transcript: str):
    """Classify a ClinVar title such as 'NM_000492.3(CFTR):c.1521_1523delCTT (p.Phe508del)' against the query.

    Returns "exact_transcript_version" (same accession.version and same cDNA change), "version_differs"
    (same accession, different/unstated version, same cDNA notation -> needs genomic confirmation), or False.
    Complex alleles 'c.[a;b]' never match."""
    m = re.match(r"^\s*([NX][MR]_\d+)(\.\d+)?\([^)]*\):(c\.[^\s]+)", title)
    if not m or "[" in m.group(3):
        return False
    acc, ver = m.group(1), (m.group(2) or "")
    q_acc, _, q_ver = transcript.partition(".")
    if transcript and acc != q_acc:
        return False
    if _norm_c(m.group(3)) != _norm_c(hgvs_c):
        return False
    if transcript and q_ver and ver == f".{q_ver}":
        return "exact_transcript_version"
    return "version_differs"


def _resolve_genomic(http: HttpClient, transcript: str, hgvs_c: str) -> tuple | None:
    """Patient's GRCh38 allele via VariantValidator VCF (lazy import avoids a module cycle)."""
    try:
        from .variant import variantvalidator

        vv = variantvalidator(http, transcript, hgvs_c)
        v = vv.get("vcf_grch38") if vv.get("ok") else None
        acc = (vv.get("genomic_grch38") or "").split(":")[0]
        return _norm_vcf(f"{acc}-{v['pos']}-{v['ref']}-{v['alt']}") if v and acc else None
    except Exception:
        return None


def _trim(chrom: str, pos0: int, ref: str, alt: str) -> tuple:
    """Minimal allele: trim common suffix first, then common prefix (0-based interbase position).
    Applied identically to both representations, so a left-aligned VCF and a fully justified SPDI of the
    same indel reduce to the same tuple."""
    ref, alt = ref.upper(), alt.upper()
    while ref and alt and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]
    while ref and alt and ref[0] == alt[0]:
        ref, alt, pos0 = ref[1:], alt[1:], pos0 + 1
    return (chrom, pos0, ref, alt)


def _norm_vcf(v: str) -> tuple | None:
    """'NC_000007.14-117559590-ATCT-A' (versioned RefSeq accession, VCF 1-based) -> minimal allele tuple.
    The accession VERSION is kept, so a GRCh37 (e.g. NC_000007.13) allele can never match a GRCh38 one.
    A bare chromosome name is rejected (assembly unknown)."""
    parts = v.strip().split("-")
    if len(parts) != 4 or not parts[1].isdigit() or not re.fullmatch(r"N[CTW]_\d+\.\d+", parts[0]):
        return None
    return _trim(parts[0], int(parts[1]) - 1, parts[2], parts[3])


def _norm_spdi(spdi: str) -> tuple | None:
    """'NC_000007.14:117559590:TCTT:T' (SPDI, 0-based) -> minimal allele tuple."""
    parts = spdi.split(":")
    if len(parts) != 4 or not parts[1].isdigit() or not re.fullmatch(r"N[CTW]_\d+\.\d+", parts[0]):
        return None
    return _trim(parts[0], int(parts[1]), parts[2], parts[3])


def _clinvar_grch38(r: dict[str, Any]) -> tuple | None:
    """GRCh38 canonical SPDI of a ClinVar esummary record, normalised like `_norm_vcf`."""
    for vs in r.get("variation_set", []) or []:
        if vs.get("canonical_spdi"):
            return _norm_spdi(vs["canonical_spdi"])
    return None


# ------------------------------------------------------------------ RefSeq

def refseq_feature_table(http: HttpClient, accession: str) -> str:
    r = http.request("GET", f"{EUTILS}/efetch.fcgi", params={"db": "nuccore", "id": accession, "rettype": "ft", "retmode": "text"})
    if r.status != 200 or ">Feature" not in r.text:
        raise ValueError(f"Feature table unavailable for {accession} (HTTP {r.status})")
    return r.text


# ------------------------------------------------------------------ Gene

def ncbi_gene_summary(http: HttpClient, ledger: EvidenceLedger, gene: str) -> dict[str, Any]:
    d = http.get_json(f"{EUTILS}/esearch.fcgi", params={"db": "gene", "term": f"{gene}[sym] AND human[orgn]", "retmode": "json", "retmax": 3})
    ids = d.get("esearchresult", {}).get("idlist", [])
    if not ids:
        ledger.log_query("ncbi_gene_summary", {"gene": gene}, [])
        return {"found": False}
    s = http.get_json(f"{EUTILS}/esummary.fcgi", params={"db": "gene", "id": ids[0], "retmode": "json"})
    r = s.get("result", {}).get(ids[0], {})
    key = f"URL:https://www.ncbi.nlm.nih.gov/gene/{ids[0]}"
    ledger.add(EvidenceRecord(key=key, source_type="ncbi_gene", title=f"NCBI Gene {r.get('name')} ({r.get('description')})",
                              url=f"https://www.ncbi.nlm.nih.gov/gene/{ids[0]}", content=r.get("summary", ""), content_scope="structured_record",
                              retrieved_by="ncbi_gene_summary", metadata={"gene_id": ids[0], "aliases": r.get("otheraliases", "")}))
    ledger.log_query("ncbi_gene_summary", {"gene": gene}, [key])
    return {"key": key, "gene_id": ids[0], "name": r.get("name"), "description": r.get("description"), "aliases": r.get("otheraliases"),
            "summary": r.get("summary", ""), "mim": r.get("mim", [])}
