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


def europepmc_fulltext_excerpts(http: HttpClient, ledger: EvidenceLedger, pmcid: str, keywords: list[str], max_paragraphs: int = 12) -> dict[str, Any]:
    pmcid = pmcid.strip().upper()
    if not pmcid.startswith("PMC"):
        pmcid = "PMC" + pmcid
    r = http.request("GET", f"{BASE}/{pmcid}/fullTextXML")
    if r.status != 200:
        ledger.log_query("europepmc_fulltext_excerpts", {"pmcid": pmcid, "keywords": keywords}, [], error=f"HTTP {r.status}")
        return {"error": f"Full text not available (HTTP {r.status}); only open-access PMC articles are retrievable."}
    paras = _paragraphs(r.text)
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
    ledger.log_query("europepmc_fulltext_excerpts", {"pmcid": pmcid, "keywords": keywords}, [key])
    return {"key": key, "title": art_title, "n_paragraphs_total": len(paras), "excerpts": excerpt}
