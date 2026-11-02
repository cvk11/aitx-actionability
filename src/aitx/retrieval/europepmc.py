"""Europe PMC: literature search (incl. preprints flag) and open-access full-text excerpts."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ..http import HttpClient
from ..ledger import EvidenceLedger, EvidenceRecord

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def europepmc_search(http: HttpClient, ledger: EvidenceLedger, query: str, max_results: int = 20, page: int = 1) -> dict[str, Any]:
    size = max(1, min(int(max_results), 50))
    page = max(1, int(page or 1))
    r = http.request("GET", f"{BASE}/search", params={"query": query, "format": "json", "pageSize": size, "page": page, "resultType": "lite"})
    if r.status != 200:
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
    try:
        r = http.request("GET", f"{BASE}/{pmcid}/fullTextXML")
    except Exception:
        rec.metadata["fulltext_status"] = "failed"
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
    r = http.request("GET", f"{BASE}/{pmcid}/fullTextXML")
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


def variant_literature_search(http: HttpClient, ledger: EvidenceLedger, gene: str, notations: list[str], extra_terms: list[str] | None = None,
                              max_results: int = 25) -> dict[str, Any]:
    """Variant-centric literature search. PubMed indexes titles/abstracts only, so variant notations that appear only
    in full text or patient tables are searched in Europe PMC full text as well (open-access articles)."""
    nots = [n for n in dict.fromkeys(notations) if n]
    if not gene or not nots:
        return {"error": "gene and at least one variant notation required"}
    ors = " OR ".join(f'"{n}"' for n in nots)
    q = f'"{gene}" AND ({ors})' + (" AND (" + " OR ".join(f'"{t}"' for t in extra_terms) + ")" if extra_terms else "")
    epmc = europepmc_search(http, ledger, q, max_results)
    from .ncbi import pubmed_search

    pm_q = f"{gene}[tiab] AND (" + " OR ".join(f'"{n}"[tiab]' for n in nots) + ")"
    pm = pubmed_search(http, ledger, pm_q, max_results)
    ledger.log_query("variant_literature_search", {"gene": gene, "notations": nots},
                     sorted({r["key"] for r in epmc.get("results", [])} | {r["key"] for r in pm.get("results", [])}))
    return {"europepmc_fulltext_query": q, "europepmc": epmc.get("results", []), "europepmc_hits": epmc.get("hit_count"),
            "pubmed_query": pm_q, "pubmed": pm.get("results", []), "pubmed_hits": pm.get("total_count"),
            "note": "A hit means the variant notation occurs somewhere in the article (often a patient table); it does not show "
                    "that a reported individual's treatment response applies to this patient. Read the passage before citing."}
