"""Europe PMC: literature search (incl. preprints flag) and open-access full-text excerpts."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from ..http import HttpClient, HTTPResult, ResponseValidationError, RetryableHTTPError
from ..ledger import EvidenceLedger, EvidenceRecord

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

# The fullTextXML endpoint intermittently returns HTTP 500 for specific articles (upstream server error,
# not a 404 and not rate limiting). A per-process negative cache makes repeated requests for the same
# article fail fast for a short window; the failure is still recorded, never treated as absence.
_FULLTEXT_UPSTREAM_FAIL: dict[str, float] = {}  # PMCID -> time.monotonic() of the last upstream 500/429
_FULLTEXT_UPSTREAM_FAIL_TTL = 15 * 60.0


def _upstream_error(pmcid: str) -> dict[str, str]:
    return {"error": f"Europe PMC full-text service returned an upstream error for {pmcid} "
                     "(HTTP 500/429; this is a retrieval failure, not evidence that the article lacks the content). "
                     "Verify from the abstract or retry later."}


def validate_search(r: HTTPResult) -> None:
    """Europe PMC search JSON must carry a numeric hitCount and a resultList; a body without them is a failed search."""
    try:
        d = r.json()
    except ValueError:
        raise ResponseValidationError("malformed Europe PMC response (not JSON)", r.url) from None
    if not isinstance(d, dict) or not str(d.get("hitCount", "")).strip().isdigit() or not isinstance(d.get("resultList"), dict):
        raise ResponseValidationError("malformed Europe PMC response (hitCount/resultList missing)", r.url)
    if d.get("errCode") or d.get("errMsg"):
        raise ResponseValidationError(f"Europe PMC error: {str(d.get('errMsg') or d.get('errCode'))[:200]}", r.url)


def europepmc_search(http: HttpClient, ledger: EvidenceLedger, query: str, max_results: int = 20, page: int = 1) -> dict[str, Any]:
    """One page of Europe PMC hits. Failures are logged as FAILED queries and returned as {'error': ...} (never as zero hits)."""
    size = max(1, min(int(max_results), 50))
    page = max(1, int(page or 1))
    try:
        r = http.request("GET", f"{BASE}/search", params={"query": query, "format": "json", "pageSize": size, "page": page, "resultType": "lite"},
                         validate=validate_search)
    except Exception as e:
        ledger.log_query("europepmc_search", {"query": query, "page": page}, [], error=f"{type(e).__name__}: {str(e)[:200]}")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    if r.status != 200:
        ledger.log_query("europepmc_search", {"query": query, "page": page}, [], error=f"HTTP {r.status}")
        return {"error": f"HTTP {r.status}"}
    d = r.json()
    out = []
    for x in d.get("resultList", {}).get("result", []):
        pmid, pmcid = x.get("pmid"), x.get("pmcid")
        src = x.get("source")
        key = f"PMID:{pmid}" if pmid else (f"PMCID:{pmcid}" if pmcid else f"EPMC:{src}:{x.get('id')}")
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else (f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/" if pmcid else f"https://europepmc.org/article/{src}/{x.get('id')}")
        ledger.add(EvidenceRecord(key=key, source_type="pubmed" if pmid else "europepmc", title=x.get("title", ""), url=url, content="",
                                  content_scope="metadata", retrieved_by="europepmc_search",
                                  metadata={"year": x.get("pubYear"), "journal": x.get("journalTitle"), "pmcid": pmcid,
                                            "preprint": src == "PPR", "open_access": x.get("isOpenAccess") == "Y"}))
        out.append({"key": key, "title": x.get("title"), "year": x.get("pubYear"), "journal": x.get("journalTitle"), "pmcid": pmcid,
                    "open_access": x.get("isOpenAccess") == "Y", "preprint": src == "PPR"})
    hits = int(d.get("hitCount") or 0)
    ledger.log_query("europepmc_search", {"query": query, "page": page}, [o["key"] for o in out], total=max(0, hits - (page - 1) * size))
    return {"query": query, "page": page, "hit_count": hits, "more_pages": page * size < hits, "results": out,
            "note": "Preprints (preprint=true) are not peer reviewed. Fetch abstracts/full text before citing."}


def _paragraphs(xml_text: str) -> list[tuple[str, str]]:
    root = ET.fromstring(xml_text)
    out = []
    for sec in root.iter("sec"):
        title = "".join(sec.findtext("title") or "")
        for p in sec.findall("p"):
            t = re.sub(r"\s+", " ", "".join(p.itertext())).strip()
            if t:
                out.append((title, t))
    return out


def fulltext_passages(xml_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """All section paragraphs AND table rows of a PMC/JATS full text, location-labelled, plus supplementary file names.
    Table rows keep their caption and header so patient-level rows remain attributable."""
    root = ET.fromstring(xml_text)
    out: list[tuple[str, str]] = []
    for sec in root.iter("sec"):
        title = re.sub(r"\s+", " ", "".join(sec.findtext("title") or "")).strip() or "section"
        for p_el in sec.findall("p"):
            t = re.sub(r"\s+", " ", "".join(p_el.itertext())).strip()
            if t:
                out.append((title, t))
    for n, tw in enumerate(root.iter("table-wrap"), start=1):
        label = re.sub(r"\s+", " ", "".join(tw.findtext("label") or f"Table {n}")).strip()
        cap_el = tw.find("caption")
        cap = re.sub(r"\s+", " ", " ".join(cap_el.itertext())).strip()[:200] if cap_el is not None else ""
        header = ""
        for i, tr in enumerate(tw.iter("tr")):
            cells = [re.sub(r"\s+", " ", " ".join(c.itertext())).strip() for c in tr if c.tag in ("td", "th")]
            row = " | ".join(cells)
            if not row:
                continue
            if i == 0 and all(c.tag == "th" for c in tr if c.tag in ("td", "th")):
                header = row
                continue
            out.append((f"{label}{': ' + cap if cap else ''}", (f"[{header}] " if header else "") + row))
    supp = []
    for sm in root.iter("supplementary-material"):
        href = next((v for k, v in sm.attrib.items() if k.endswith("href")), "") or ""
        lab = re.sub(r"\s+", " ", " ".join(sm.itertext())).strip()[:120]
        supp.append(href or lab)
    return out, supp


def load_fulltext(http: HttpClient, ledger: EvidenceLedger, key: str, pmcid: str) -> str:
    """Attach all full-text passages (incl. tables) of an open-access PMC article to an existing ledger record.
    Returns 'loaded' | 'not_open_access' | 'failed'. Never raises."""
    rec = ledger.get(key)
    if rec is None:
        return "failed"
    pmcid = pmcid.strip().upper()
    if not pmcid.startswith("PMC"):
        pmcid = "PMC" + pmcid
    if time.monotonic() - _FULLTEXT_UPSTREAM_FAIL.get(pmcid, -1e9) < _FULLTEXT_UPSTREAM_FAIL_TTL:
        rec.metadata["fulltext_status"] = "failed"
        ledger.log_query("europepmc_fulltext", {"pmcid": pmcid}, [], error=f"recent upstream failure cached for {pmcid}")
        return "failed"
    try:
        r = http.request("GET", f"{BASE}/{pmcid}/fullTextXML")
    except RetryableHTTPError:
        _FULLTEXT_UPSTREAM_FAIL[pmcid] = time.monotonic()
        rec.metadata["fulltext_status"] = "failed"
        ledger.log_query("europepmc_fulltext", {"pmcid": pmcid}, [], error=f"upstream HTTP 500/429 for {pmcid}")
        return "failed"
    except Exception:
        rec.metadata["fulltext_status"] = "failed"
        ledger.log_query("europepmc_fulltext", {"pmcid": pmcid}, [], error="request failed")
        return "failed"
    if r.status != 200 or "<" not in r.text[:10]:
        rec.metadata["fulltext_status"] = "not_open_access" if r.status in (404, 400) else "failed"
        return rec.metadata["fulltext_status"]
    try:
        ps, supp = fulltext_passages(r.text)
    except ET.ParseError:
        rec.metadata["fulltext_status"] = "failed"
        return "failed"
    rec.metadata["passages"] = ps
    rec.metadata["supplementary_files"] = supp
    rec.metadata["fulltext_status"] = "loaded" + ("; supplementary files listed but not parsed" if supp else "")
    rec.metadata["pmcid"] = pmcid
    rec.content_scope = "full_text_excerpt"
    return "loaded"


def europepmc_fulltext_excerpts(http: HttpClient, ledger: EvidenceLedger, pmcid: str, keywords: list[str], max_paragraphs: int = 12) -> dict[str, Any]:
    pmcid = pmcid.strip().upper()
    if not pmcid.startswith("PMC"):
        pmcid = "PMC" + pmcid
    if time.monotonic() - _FULLTEXT_UPSTREAM_FAIL.get(pmcid, -1e9) < _FULLTEXT_UPSTREAM_FAIL_TTL:
        ledger.log_query("europepmc_fulltext_excerpts", {"pmcid": pmcid, "keywords": keywords}, [],
                         error=f"recent upstream failure cached for {pmcid}")
        return _upstream_error(pmcid)
    try:
        r = http.request("GET", f"{BASE}/{pmcid}/fullTextXML")
    except RetryableHTTPError:
        _FULLTEXT_UPSTREAM_FAIL[pmcid] = time.monotonic()
        ledger.log_query("europepmc_fulltext_excerpts", {"pmcid": pmcid, "keywords": keywords}, [],
                         error=f"upstream HTTP 500/429 for {pmcid}")
        return _upstream_error(pmcid)
    if r.status != 200:
        ledger.log_query("europepmc_fulltext_excerpts", {"pmcid": pmcid, "keywords": keywords}, [], error=f"HTTP {r.status}")
        return {"error": f"Full text not available (HTTP {r.status}); only open-access PMC articles are retrievable."}
    paras = _paragraphs(r.text)
    try:
        all_passages, supp = fulltext_passages(r.text)
    except ET.ParseError:
        all_passages, supp = [], []
    paras = paras + [(loc, t) for loc, t in all_passages if loc.lower().startswith("table")]
    kws = [k.lower() for k in keywords if k.strip()]
    scored = []
    for i, (title, t) in enumerate(paras):
        low = t.lower()
        s = sum(low.count(k) for k in kws)
        if s or not kws:
            scored.append((s, i, title, t))
    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen = sorted(scored[:max_paragraphs], key=lambda x: x[1])
    excerpt = "\n".join(f"[{title}] {t[:1500]}" for _, _, title, t in chosen)
    # register under the PMID if we know it
    root = ET.fromstring(r.text)
    pmid = next((e.text for e in root.iter("article-id") if e.get("pub-id-type") == "pmid"), None)
    art_title = re.sub(r"\s+", " ", "".join(next(root.iter("article-title")).itertext())) if next(root.iter("article-title"), None) is not None else pmcid
    key = f"PMID:{pmid}" if pmid else f"PMCID:{pmcid}"
    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    rec = ledger.add(EvidenceRecord(key=key, source_type="pubmed", title=art_title, url=url, content=excerpt, content_scope="full_text_excerpt",
                                    retrieved_by="europepmc_fulltext_excerpts", accessed_at=r.fetched_at, metadata={"pmcid": pmcid}))
    if len(excerpt) > len(rec.content):  # ensure full-text excerpt is kept even if abstract was stored
        rec.content = excerpt
    if rec.content_scope != "full_text_excerpt" and excerpt:
        rec.content = (rec.content + "\n[FULL-TEXT EXCERPTS]\n" + excerpt)[:20000]
        rec.content_scope = "full_text_excerpt"
    rec.metadata["passages"] = all_passages
    rec.metadata["supplementary_files"] = supp
    rec.metadata["fulltext_status"] = "loaded" + ("; supplementary files listed but not parsed" if supp else "")
    ledger.log_query("europepmc_fulltext_excerpts", {"pmcid": pmcid, "keywords": keywords}, [key])
    return {"key": key, "title": art_title, "n_paragraphs_total": len(paras), "n_table_rows": sum(1 for l, _ in all_passages if l.lower().startswith("table")),
            "supplementary_files": supp[:10], "excerpts": excerpt}


def _anchor_re(terms: list[str]) -> re.Pattern[str]:
    return re.compile("|".join(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])" for t in terms if t), re.I)


def variant_context(rec: EvidenceRecord | None, gene_terms: list[str], notations: list[str]) -> dict[str, Any]:
    """Does the accessible text of an article mention the patient's variant IN THE CONTEXT OF THIS GENE?

    A Europe PMC full-text hit only shows that the gene symbol and a variant notation string occur somewhere in
    the article, possibly about another protein. The hit is 'confirmed' only if one passage (paragraph or table row,
    including its table caption) contains both a gene anchor and a variant notation. Otherwise:
    'notation_without_gene_context' (the notation occurs only away from the gene), 'notation_not_in_accessible_text'
    (indexed match not reproducible in the parsed full text, e.g. figures/supplements), or 'full_text_unavailable'."""
    from ..evidence import mentions_variant

    if rec is None:
        return {"status": "full_text_unavailable", "passages": []}
    passages = rec.metadata.get("passages") or []
    if not passages:
        st = rec.metadata.get("fulltext_status", "")
        # title/abstract are still checkable
        txt = (rec.title or "") + "\n" + (rec.content or "")
        gene_re = _anchor_re(gene_terms)
        if txt.strip() and mentions_variant(txt, notations) and gene_re.search(txt):
            return {"status": "confirmed", "where": "title/abstract", "passages": [txt[:300]], "fulltext_status": st}
        return {"status": "full_text_unavailable", "passages": [], "fulltext_status": st or "not loaded"}
    gene_re = _anchor_re(gene_terms)
    with_notation = [(loc, t) for loc, t in passages if mentions_variant(t, notations)]
    same = [(loc, t) for loc, t in with_notation if gene_re.search(loc + " " + t)]
    if same:
        return {"status": "confirmed", "where": same[0][0][:80], "passages": [f"[{loc[:60]}] {t[:300]}" for loc, t in same[:3]],
                "notations_found": sorted({n for _, t in same for n in mentions_variant(t, notations)})}
    if with_notation:
        return {"status": "notation_without_gene_context", "passages": [f"[{loc[:60]}] {t[:200]}" for loc, t in with_notation[:2]],
                "notations_found": sorted({n for _, t in with_notation for n in mentions_variant(t, notations)})}
    return {"status": "notation_not_in_accessible_text", "passages": []}


def variant_literature_search(http: HttpClient, ledger: EvidenceLedger, gene: str, notations: list[str], extra_terms: list[str] | None = None,
                              max_results: int = 25, gene_aliases: list[str] | None = None, check_context: bool = True) -> dict[str, Any]:
    """Variant-centric literature search. PubMed indexes titles/abstracts only, so variant notations that appear only
    in full text or patient tables are searched in Europe PMC full text as well (open-access articles).

    Europe PMC hits are then checked for gene context (`variant_context`): only hits whose accessible text mentions the
    variant together with the gene are returned as variant literature; the others are listed under
    `europepmc_unconfirmed` with the reason (a short notation such as 'R123W' can belong to any protein). A failed source is
    reported in `failures` and the result status is 'partial'/'failed' (never an empty result)."""
    nots = [n for n in dict.fromkeys(notations) if n]
    if not gene or not nots:
        return {"error": "gene and at least one variant notation required"}
    ors = " OR ".join(f'"{n}"' for n in nots)
    q = f'"{gene}" AND ({ors})' + (" AND (" + " OR ".join(f'"{t}"' for t in extra_terms) + ")" if extra_terms else "")
    failures: dict[str, str] = {}
    epmc = europepmc_search(http, ledger, q, max_results)
    if epmc.get("error"):
        failures["europepmc"] = epmc["error"]
    from .ncbi import failure_text, gene_aliases as _aliases, pubmed_search

    pm_q = f"{gene}[tiab] AND (" + " OR ".join(f'"{n}"[tiab]' for n in nots) + ")"
    try:
        pm = pubmed_search(http, ledger, pm_q, max_results)
    except Exception as e:
        failures["pubmed"] = failure_text(e)
        pm = {}
    terms = gene_aliases if gene_aliases is not None else _aliases(http, gene)
    confirmed, unconfirmed = [], []
    for h in epmc.get("results", []):
        if not check_context:
            confirmed.append(h)
            continue
        rec = ledger.get(h["key"])
        if rec is not None and h.get("pmcid") and not rec.metadata.get("passages"):
            load_fulltext(http, ledger, h["key"], h["pmcid"])
        ctx = variant_context(ledger.get(h["key"]), terms, nots)
        if rec is not None:
            rec.metadata["variant_context"] = ctx["status"]
        (confirmed if ctx["status"] == "confirmed" else unconfirmed).append({**h, "variant_context": ctx})
    status = "failed" if len(failures) == 2 else ("partial" if failures else "ok")
    keys = sorted({r["key"] for r in confirmed} | {r["key"] for r in pm.get("results", [])})
    ledger.log_query("variant_literature_search", {"gene": gene, "notations": nots}, keys,
                     error="; ".join(f"{k}: {v}" for k, v in failures.items()))
    return {"europepmc_fulltext_query": q, "europepmc": confirmed, "europepmc_hits": epmc.get("hit_count"),
            "europepmc_unconfirmed": unconfirmed, "gene_anchor_terms": terms,
            "pubmed_query": pm_q, "pubmed": pm.get("results", []), "pubmed_hits": pm.get("total_count"),
            "status": status, "failures": failures,
            "note": "Europe PMC hits are listed under 'europepmc' only if a passage of the accessible text mentions the variant "
                    "together with the gene; others are in 'europepmc_unconfirmed' and are NOT evidence about this variant. "
                    "PubMed hits match gene and notation in the same title/abstract. A hit does not show that a reported "
                    "individual's treatment response applies to this patient. Read the passage before citing."}
