"""GeneReviews retrieval from the locally built NCBI litarch snapshot.

The snapshot is built by ``scripts/build_genereviews_index.py`` (official NCBI
archive; manifest with archive date + SHA-256). Every returned section carries
the chapter's "last updated/revised" date so stages can judge currency, and the
canonical Bookshelf URL (with section anchor) for citation.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..ledger import EvidenceLedger, EvidenceRecord

DEFAULT_SECTIONS = ("Treatment of Manifestations", "Prevention of Primary Manifestations", "Prevention of Secondary Complications",
                    "Agents/Circumstances to Avoid", "Therapies Under Investigation", "Genotype-Phenotype Correlations",
                    "Molecular Pathogenesis", "Mechanism of disease causation", "Pregnancy Management")


class GeneReviewsIndex:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.nxml = self.root / "nxml"
        self.available = self.nxml.is_dir() and any(self.nxml.glob("*.nxml"))
        self.gene_map: dict[str, list[dict[str, str]]] = {}
        self.titles: dict[str, str] = {}
        self.short_to_nbk: dict[str, str] = {}
        self.manifest: dict[str, Any] = {}
        if not self.available:
            return
        mp = self.root / "GRshortname_NBKid_genesymbol_dzname.txt"
        if mp.exists():
            for line in mp.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split("|")
                if len(parts) >= 4 and parts[1].startswith("NBK"):
                    short, nbk, gene, dz = parts[0], parts[1], parts[2], parts[3]
                    self.gene_map.setdefault(gene.upper(), []).append({"shortname": short, "nbk": nbk, "disease": dz})
                    self.short_to_nbk[short] = nbk
        tp = self.root / "GRtitle_shortname_NBKid.txt"
        if tp.exists():
            for line in tp.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split("|")
                if len(parts) >= 3 and parts[2].startswith("NBK"):
                    self.titles[parts[2]] = parts[0]
                    self.short_to_nbk.setdefault(parts[1], parts[2])
        mf = self.root / "MANIFEST.json"
        if mf.exists():
            self.manifest = json.loads(mf.read_text())

    def nbk_to_short(self, nbk: str) -> str | None:
        for s, n in self.short_to_nbk.items():
            if n == nbk:
                return s
        return None

    @lru_cache(maxsize=64)
    def _parse(self, shortname: str) -> ET.Element | None:
        p = self.nxml / f"{shortname}.nxml"
        if not p.exists():
            return None
        return ET.parse(p).getroot()

    def chapter_meta(self, shortname: str) -> dict[str, Any]:
        root = self._parse(shortname)
        if root is None:
            return {}
        meta: dict[str, Any] = {"shortname": shortname}
        tg = root.find(".//book-part-meta/title-group/title")
        meta["title"] = re.sub(r"\s+", " ", "".join(tg.itertext())).strip() if tg is not None else shortname
        for d in root.findall(".//book-part-meta//date"):
            y, m, dd = d.findtext("year"), d.findtext("month"), d.findtext("day")
            if y:
                meta[d.get("date-type", "date")] = f"{y}-{int(m or 1):02d}-{int(dd or 1):02d}"
        return meta

    def search_text(self, term: str, max_chapters: int = 12) -> list[dict[str, Any]]:
        """Chapters whose text mentions `term` (word match), with section titles of hits."""
        if not self.available:
            return []
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])")
        hits = []
        for p in self.nxml.glob("*.nxml"):
            if p.name.startswith("GR"):
                continue
            raw = p.read_text(encoding="utf-8", errors="replace")
            n = len(pat.findall(raw))
            if n:
                hits.append((n, p.stem))
        hits.sort(reverse=True)
        out = []
        for n, short in hits[:max_chapters]:
            root = self._parse(short)
            secs = []
            if root is not None:
                for s in root.iter("sec"):
                    title = "".join((s.find("title").itertext() if s.find("title") is not None else []))
                    own = "".join(t for c in s if c.tag != "sec" for t in c.itertext())
                    if pat.search(own):
                        secs.append(title)
            meta = self.chapter_meta(short)
            title = meta.get("title") or ""
            if "RETIRED" in title.upper():
                continue  # retired chapters are kept by NCBI for historical reference only
            out.append({"shortname": short, "nbk": self.short_to_nbk.get(short, ""), "title": title, "mentions": n,
                        "sections_with_mentions": secs[:12], "last_updated": meta.get("revised") or meta.get("updated")})
        return out

    def read_sections(self, shortname: str, wanted: list[str] | None = None, max_chars_per_section: int = 6000) -> list[dict[str, Any]]:
        root = self._parse(shortname)
        if root is None:
            return []
        wanted_l = [w.lower() for w in (wanted or DEFAULT_SECTIONS)]
        refs = {}
        for r in root.iter("ref"):
            pmid = next((x.text for x in r.iter("pub-id") if x.get("pub-id-type") == "pmid"), None)
            refs[r.get("id")] = pmid
        out = []
        for s in root.iter("sec"):
            t_el = s.find("title")
            title = re.sub(r"\s+", " ", "".join(t_el.itertext())).strip() if t_el is not None else ""
            if not any(w in title.lower() for w in wanted_l):
                continue
            text = re.sub(r"\s+", " ", " ".join(s.itertext())).strip()
            cited = sorted({refs[x.get("rid")] for x in s.iter("xref") if refs.get(x.get("rid"))})
            out.append({"section_id": s.get("id", ""), "title": title, "text": text[:max_chars_per_section],
                        "truncated": len(text) > max_chars_per_section, "cited_pmids": cited[:40]})
        return out


@lru_cache(maxsize=4)
def get_index(root: str) -> GeneReviewsIndex:
    return GeneReviewsIndex(Path(root))


def genereviews_find(index: GeneReviewsIndex, ledger: EvidenceLedger, gene: str, extra_terms: list[str] | None = None) -> dict[str, Any]:
    if not index.available:
        ledger.log_query("genereviews_find", {"gene": gene}, [], error="snapshot not built")
        return {"error": "GeneReviews snapshot not available; run scripts/build_genereviews_index.py"}
    primary = []
    for e in index.gene_map.get(gene.upper(), []):
        meta = index.chapter_meta(e["shortname"])
        primary.append({**e, "title": meta.get("title") or index.titles.get(e["nbk"], ""), "last_updated": meta.get("revised") or meta.get("updated"),
                        "url": f"https://www.ncbi.nlm.nih.gov/books/{e['nbk']}/"})
    mentions = index.search_text(gene)
    for t in extra_terms or []:
        mentions += index.search_text(t, max_chapters=5)
    ledger.log_query("genereviews_find", {"gene": gene, "extra_terms": extra_terms or []}, [f"GENEREVIEWS:{p['nbk']}" for p in primary])
    return {"chapters_for_gene": primary, "chapters_mentioning": mentions[:15], "snapshot": index.manifest.get("archive_last_updated"),
            "note": "Primary chapters are curated gene->chapter links. Mentions may be differential-diagnosis only."}


def genereviews_read(index: GeneReviewsIndex, ledger: EvidenceLedger, chapter: str, sections: list[str] | None = None) -> dict[str, Any]:
    if not index.available:
        return {"error": "GeneReviews snapshot not available"}
    short = chapter
    if chapter.upper().startswith("NBK"):
        short = index.nbk_to_short(chapter.upper()) or ""
    nbk = index.short_to_nbk.get(short, chapter if chapter.upper().startswith("NBK") else "")
    if not short or index._parse(short) is None:
        return {"error": f"chapter {chapter!r} not found in snapshot"}
    meta = index.chapter_meta(short)
    secs = index.read_sections(short, sections)
    keys = []
    for s in secs:
        anchor = s["section_id"]
        key = f"GENEREVIEWS:{nbk}#{anchor.split('.', 1)[-1] if anchor else s['title'].replace(' ', '_')}"
        ledger.add(EvidenceRecord(
            key=key, source_type="genereviews", title=f"GeneReviews: {meta.get('title')} - {s['title']}",
            url=f"https://www.ncbi.nlm.nih.gov/books/{nbk}/#{anchor}" if anchor else f"https://www.ncbi.nlm.nih.gov/books/{nbk}/",
            content=s["text"], content_scope="full_text_excerpt", retrieved_by="genereviews_read",
            metadata={"section_title": s["title"], "chapter_title": meta.get("title"), "last_updated": meta.get("revised") or meta.get("updated"),
                      "cited_pmids": s["cited_pmids"], "snapshot": index.manifest.get("archive_last_updated")},
        )).verified_exists = True
        keys.append(key)
        s["key"] = key
    ledger.log_query("genereviews_read", {"chapter": chapter, "sections": sections or list(DEFAULT_SECTIONS)}, keys)
    return {"chapter": meta, "nbk": nbk, "sections": secs,
            "currency_note": f"Chapter last revised/updated {meta.get('revised') or meta.get('updated')}; newer evidence may exist - check PubMed/ClinicalTrials.gov."}
