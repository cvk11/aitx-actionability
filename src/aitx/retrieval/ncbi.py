"""NCBI E-utilities: PubMed, ClinVar, RefSeq feature tables, NCBI Gene."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..http import HttpClient, HTTPFetchError, HTTPResult, ResponseValidationError
from ..ledger import EvidenceLedger, EvidenceRecord

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


# ------------------------------------------------------------------ E-utilities response validation
# NCBI E-utilities can answer HTTP 200 with a body that reports a backend failure (e.g. esearchresult.ERROR
# "Search Backend failed ... 502") or with a malformed/incomplete payload. Such a response is a FAILED retrieval:
# it must never be read as "count 0" and never be cached (http.request retries, evicts and raises).

def _json_body(r: HTTPResult) -> dict[str, Any]:
    try:
        d = r.json()
    except ValueError:
        raise ResponseValidationError("malformed E-utilities response (not JSON)", r.url) from None
    if not isinstance(d, dict):
        raise ResponseValidationError("malformed E-utilities response (not a JSON object)", r.url)
    for k in ("ERROR", "error"):
        if d.get(k):
            raise ResponseValidationError(f"E-utilities error: {str(d[k])[:200]}", r.url)
    return d


def validate_esearch(r: HTTPResult) -> None:
    d = _json_body(r)
    res = d.get("esearchresult")
    if not isinstance(res, dict):
        raise ResponseValidationError("malformed ESearch response (no esearchresult)", r.url)
    if res.get("ERROR"):
        raise ResponseValidationError(f"ESearch backend error: {str(res['ERROR'])[:200]}", r.url)
    count, ids = res.get("count"), res.get("idlist")
    if not str(count if count is not None else "").strip().isdigit():
        raise ResponseValidationError("malformed ESearch response (count missing or not numeric)", r.url)
    if not isinstance(ids, list):
        raise ResponseValidationError("malformed ESearch response (idlist missing)", r.url)
    q = parse_qs(urlparse(r.url).query)
    retstart = int((q.get("retstart") or ["0"])[0] or 0)
    retmax = int((q.get("retmax") or ["20"])[0] or 20)
    if int(count) > retstart and retmax > 0 and not ids:
        raise ResponseValidationError(f"incomplete ESearch response (count {count} but no ids returned)", r.url)


def validate_esummary(r: HTTPResult) -> None:
    d = _json_body(r)
    res = d.get("result")
    if not isinstance(res, dict) or not isinstance(res.get("uids", []), list):
        raise ResponseValidationError("malformed ESummary response (no result/uids)", r.url)
    if "uids" not in res:
        raise ResponseValidationError("malformed ESummary response (uids missing)", r.url)


def validate_efetch_xml(r: HTTPResult) -> None:
    head = r.text.lstrip()[:4000]
    if "<ERROR>" in head:
        raise ResponseValidationError("EFetch error: " + head.split("<ERROR>", 1)[1].split("</ERROR>", 1)[0][:200], r.url)
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        raise ResponseValidationError("malformed EFetch response (XML parse error)", r.url) from None
    if root.tag not in ("PubmedArticleSet", "eFetchResult"):
        raise ResponseValidationError(f"unexpected EFetch root element <{root.tag}>", r.url)
    if root.tag == "eFetchResult":
        raise ResponseValidationError("EFetch returned an eFetchResult error document", r.url)


def esearch(http: HttpClient, db: str, term: str, retmax: int, retstart: int = 0, sort: str = "") -> dict[str, Any]:
    """Validated ESearch. Raises (HTTPFetchError / ResponseValidationError / transport errors) on any failure."""
    params = {"db": db, "term": term, "retmode": "json", "retmax": retmax}
    if retstart:
        params["retstart"] = retstart
    if sort:
        params["sort"] = sort
    r = http.request("GET", f"{EUTILS}/esearch.fcgi", params=params, validate=validate_esearch)
    if r.status != 200:
        raise HTTPFetchError(r.status, r.url)
    return r.json()["esearchresult"]


def esummary(http: HttpClient, db: str, ids: list[str]) -> dict[str, Any]:
    r = http.request("GET", f"{EUTILS}/esummary.fcgi", params={"db": db, "id": ",".join(ids), "retmode": "json"}, validate=validate_esummary)
    if r.status != 200:
        raise HTTPFetchError(r.status, r.url)
    return r.json()["result"]


def failure_text(e: BaseException) -> str:
    """Short, value-free description of a retrieval failure for the ledger and audit."""
    return f"{type(e).__name__}: {str(e)[:200]}"


def pubmed_search(http: HttpClient, ledger: EvidenceLedger, query: str, max_results: int = 20, sort: str = "relevance", page: int = 1,
                  log: bool = True, tag: dict[str, Any] | None = None) -> dict[str, Any]:
    """One page of PubMed hits. A failed or malformed response is logged as a FAILED query and raised; it is never
    reported as zero hits. `total_count == 0` therefore means the search ran and genuinely matched nothing."""
    max_results = max(1, min(int(max_results), 50))
    page = max(1, int(page or 1))
    retstart = (page - 1) * max_results
    args = {"query": query, "page": page, **(tag or {})}
    try:
        res = esearch(http, "pubmed", query, max_results, retstart, sort)
        ids = [str(i) for i in res.get("idlist", [])]
        total = int(res["count"])
        summaries = pubmed_summaries(http, ledger, ids) if ids else []
    except Exception as e:
        if log:
            ledger.log_query("pubmed_search", args, [], error=failure_text(e))
        raise
    warnings = res.get("warninglist") or {}
    # coverage: reviewed hits are those up to the end of this page
    if log:
        ledger.log_query("pubmed_search", args, [f"PMID:{i}" for i in ids], total=max(0, total - retstart))
    return {"query": query, "page": page, "total_count": total, "returned": len(ids), "more_pages": retstart + len(ids) < total,
            "status": "ok" if total else "empty",
            "query_translation": res.get("querytranslation", ""), "results": summaries,
            "warnings": {k: v for k, v in warnings.items() if v} if isinstance(warnings, dict) else {},
            "note": "Titles only. Call pubmed_fetch_abstracts on relevant PMIDs before citing them. Use page=2,3,... to review further hits."}


def pubmed_search_all(http: HttpClient, ledger: EvidenceLedger, query: str, cap: int = 100, page_size: int = 50,
                      tag: dict[str, Any] | None = None) -> dict[str, Any]:
    """All PubMed hits of a query up to `cap` (paginated), logged as ONE query whose total and truncation describe the
    whole search. status: complete | truncated (more hits than cap) | empty (genuine zero) | failed (nothing retrieved)
    | partial (a later page failed: hits so far are kept, the search is incomplete)."""
    keys: list[str] = []
    titles: dict[str, str] = {}
    total: int | None = None
    err, pages = "", 0
    page = 1
    while True:
        try:
            r = pubmed_search(http, ledger, query, page_size, page=page, log=False)
        except Exception as e:
            err = failure_text(e)
            break
        pages += 1
        total = r["total_count"]
        for x in r["results"]:
            if x["key"] not in titles:
                keys.append(x["key"])
                titles[x["key"]] = x.get("title", "")
        if not r["more_pages"] or len(keys) >= cap or not r["results"]:
            break
        page += 1
    keys = keys[:cap]
    titles = {k: titles[k] for k in keys}
    if err:
        status = "partial" if keys else "failed"
    elif not total:
        status = "empty"
    else:
        status = "truncated" if total > len(keys) else "complete"
    ledger.log_query("pubmed_search", {"query": query, "pages": pages, "cap": cap, **(tag or {})}, keys, error=err,
                     total=total if total is not None else None)
    return {"query": query, "total": total, "reviewed": len(keys), "keys": keys, "titles": titles, "pages": pages,
            "truncated": status in ("truncated", "partial"), "status": status, "error": err}


# Mandatory therapeutic-coverage query families (gene-generic). Direction-specific modulator queries are both run,
# because whether activation or inhibition is appropriate depends on the (uncertain) variant mechanism.
COVERAGE_FAMILIES = {
    "activating_modulators": '{g}[tiab] AND (potentiat*[tiab] OR activator*[tiab] OR agonist*[tiab] OR "positive modulator*"[tiab])',
    "inhibiting_modulators": '{g}[tiab] AND (inhibitor*[tiab] OR antagonist*[tiab] OR blocker*[tiab] OR "negative modulator*"[tiab])',
    "human_treatment_reports": '{g}[tiab] AND (treat*[tiab] OR therap*[tiab]) AND (patient*[tiab] OR case*[tiab] OR child*[tiab])',
    "nucleic_acid_therapies": '{g}[tiab] AND (antisense[tiab] OR oligonucleotide*[tiab] OR siRNA[tiab] OR "gene therapy"[tiab] OR "allele-specific"[tiab])',
}


def therapeutic_coverage_search(http: HttpClient, ledger: EvidenceLedger, gene: str, max_results: int = 100) -> dict[str, Any]:
    """Run every coverage family for a gene, paginating up to `max_results` hits per family; record hits, totals,
    truncation and failures. A failed or partial family is a coverage GAP, never evidence of absence; only
    status 'empty' (the search ran and matched nothing) is a genuine zero."""
    out: dict[str, Any] = {}
    for fam, tmpl in COVERAGE_FAMILIES.items():
        q = tmpl.format(g=gene)
        out[fam] = pubmed_search_all(http, ledger, q, cap=max_results, tag={"coverage_family": fam})
    return out


def coverage_failures(coverage: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Coverage families whose retrieval failed or is incomplete (for prompts, audit and report wording)."""
    rows = []
    for gene, fams in (coverage or {}).items():
        for fam, f in (fams or {}).items():
            st = f.get("status") or ("failed" if f.get("error") else ("truncated" if f.get("truncated") else "complete"))
            if st in ("failed", "partial", "truncated"):
                rows.append({"gene": gene, "family": fam, "status": st, "query": f.get("query", ""), "total": f.get("total"),
                             "reviewed": len(f.get("keys", [])), "error": f.get("error", "")})
    return rows


def pubmed_summaries(http: HttpClient, ledger: EvidenceLedger, pmids: list[str]) -> list[dict[str, Any]]:
    result = esummary(http, "pubmed", pmids)
    out = []
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
    try:
        r = http.request("GET", f"{EUTILS}/efetch.fcgi", params={"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}, validate=validate_efetch_xml)
        if r.status != 200:
            raise HTTPFetchError(r.status, r.url)
        arts = parse_pubmed_xml(r.text)
    except Exception as e:  # a failed fetch is not "not found"
        ledger.log_query("pubmed_fetch_abstracts", {"pmids": pmids}, [], error=failure_text(e))
        raise
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
        res = esummary(http, "pubmed", chunk)  # raises on failure: existence then stays unverified, never "missing"
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
    failed_terms: list[dict[str, str]] = []
    for t in terms:
        tried.append(t)
        try:
            ids = [str(i) for i in esearch(http, "clinvar", t, max_results).get("idlist", [])]
            if not ids:
                continue
            res = esummary(http, "clinvar", ids)
        except Exception as e:  # a failed term is a retrieval failure, not "no record"
            failed_terms.append({"term": t, "error": failure_text(e)})
            continue
        used = t
        for uid in res.get("uids", []):
            r = res[uid]
            gc = r.get("germline_classification", {}) or {}
            traits = [t.get("trait_name") for t in gc.get("trait_set", []) if t.get("trait_name")]
            acc = r.get("accession", f"VCV{uid}")
            title = r.get("title", "")
            kind = "gene_level_listing" if not hgvs_c else _clinvar_title_matches(title, hgvs_c, transcript)
            if kind is False and hgvs_c and "[" not in title:
                # cDNA named on another transcript (or with other numbering): same variant only if the GRCh38 allele is identical
                if gpos is None and not genomic_tried:
                    genomic_tried = True
                    gpos = _resolve_genomic(http, transcript, hgvs_c)
                loc = _clinvar_grch38(r)
                if gpos is not None and loc is not None and alleles_equivalent(loc, gpos, http):
                    kind = "genomic_equivalent_other_transcript"
            if kind == "version_differs":
                if gpos is None and not genomic_tried:
                    genomic_tried = True
                    gpos = _resolve_genomic(http, transcript, hgvs_c)
                loc = _clinvar_grch38(r)
                if gpos is not None and loc is not None and alleles_equivalent(loc, gpos, http):
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
    args = {"gene": gene, "hgvs_c": hgvs_c, "transcript": transcript, "term_used": used}
    if failed_terms and not out:
        err = f"{len(failed_terms)} of {len(tried)} ClinVar search terms failed: {failed_terms[0]['error']}"
        ledger.log_query("clinvar_lookup", args, [], error=err)
        raise ResponseValidationError(err)
    ledger.log_query("clinvar_lookup", args, [o["key"] for o in out])
    note = ("No ClinVar record whose title matches this exact cDNA change was found (searched; this is not evidence that the "
            "variant is benign or unreported elsewhere)." if hgvs_c and not out else "")
    return {"term_used": used, "terms_tried": tried, "records": out, "non_matching_hits": non_matching[:5],
            "version_mismatch_candidates": mismatch[:5], "note": note, "failed_terms": failed_terms}


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


def _left_align(t: tuple, seq: str, start0: int) -> tuple:
    """Shift a pure insertion/deletion to its leftmost position using reference sequence `seq` (starting at 0-based `start0`)."""
    acc, pos0, ref, alt = t
    if ref and alt:
        return t
    allele = ref or alt
    while pos0 - 1 >= start0 and pos0 - 1 - start0 < len(seq) and seq[pos0 - 1 - start0].upper() == allele[-1]:
        allele = seq[pos0 - 1 - start0].upper() + allele[:-1]
        pos0 -= 1
    return (acc, pos0, allele, "") if ref else (acc, pos0, "", allele)


def alleles_equivalent(a: tuple, b: tuple, http: HttpClient | None = None) -> bool:
    """Same allele on the same versioned reference. Minimal forms are compared first; differing pure indels of the
    same type and length near each other (repeat regions) are compared after left-alignment against the reference
    sequence fetched from NCBI. If the reference cannot be fetched the alleles are NOT treated as equivalent."""
    if a == b:
        return True
    if a[0] != b[0] or http is None:
        return False
    da, db = (a[2], a[3]), (b[2], b[3])
    pure = lambda x: (x[0] == "") != (x[1] == "")
    if not (pure(da) and pure(db)) or (da[0] == "") != (db[0] == "") or len(da[0] or da[1]) != len(db[0] or db[1]) or abs(a[1] - b[1]) > 200:
        return False
    n = len(da[0] or da[1])
    lo, hi = max(0, min(a[1], b[1]) - 250), max(a[1], b[1]) + n + 10
    try:
        r = http.request("GET", f"{EUTILS}/efetch.fcgi", params={"db": "nuccore", "id": a[0], "rettype": "fasta", "retmode": "text",
                                                                 "seq_start": lo + 1, "seq_stop": hi})
        if r.status != 200 or not r.text.startswith(">"):
            return False
        seq = "".join(r.text.splitlines()[1:]).strip().upper()
    except Exception:
        return False
    if len(seq) < hi - lo - 5:
        return False
    return _left_align(a, seq, lo) == _left_align(b, seq, lo)


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
    try:
        ids = [str(i) for i in esearch(http, "gene", f"{gene}[sym] AND human[orgn]", 3).get("idlist", [])]
        if not ids:
            ledger.log_query("ncbi_gene_summary", {"gene": gene}, [])
            return {"found": False}
        r = esummary(http, "gene", ids[:1]).get(ids[0], {})
    except Exception as e:
        ledger.log_query("ncbi_gene_summary", {"gene": gene}, [], error=failure_text(e))
        raise
    key = f"URL:https://www.ncbi.nlm.nih.gov/gene/{ids[0]}"
    ledger.add(EvidenceRecord(key=key, source_type="ncbi_gene", title=f"NCBI Gene {r.get('name')} ({r.get('description')})",
                              url=f"https://www.ncbi.nlm.nih.gov/gene/{ids[0]}", content=r.get("summary", ""), content_scope="structured_record",
                              retrieved_by="ncbi_gene_summary", metadata={"gene_id": ids[0], "aliases": r.get("otheraliases", "")}))
    ledger.log_query("ncbi_gene_summary", {"gene": gene}, [key])
    return {"key": key, "gene_id": ids[0], "name": r.get("name"), "description": r.get("description"), "aliases": r.get("otheraliases"),
            "summary": r.get("summary", ""), "mim": r.get("mim", [])}


def gene_aliases(http: HttpClient, gene: str) -> list[str]:
    """Official symbol plus NCBI Gene aliases usable as reasonably specific text anchors: >= 5 characters, containing a
    digit, and not a prefix of another alias/the symbol (a prefix such as a family name covers paralogues). Short or
    generic aliases are dropped because they also name other genes. Not logged as evidence; symbol only on failure."""
    out = [gene]
    try:
        ids = [str(i) for i in esearch(http, "gene", f"{gene}[sym] AND human[orgn]", 3).get("idlist", [])]
        if ids:
            r = esummary(http, "gene", ids[:1]).get(ids[0], {})
            cands = [a.strip() for a in re.split(r"[,;]\s*", r.get("otheraliases") or "") if a.strip()]
            pool = cands + [gene]
            for a in cands:
                prefix_of_other = any(o != a and o.upper().startswith(a.upper()) for o in pool)
                if len(a) >= 5 and re.search(r"\d", a) and not prefix_of_other and a not in out:
                    out.append(a)
    except Exception:
        pass
    return out
