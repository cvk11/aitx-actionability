"""Allow-listed fetch of authoritative web pages (regulatory, N=1 resources, journals).

Only domains on ALLOWED_DOMAINS can be fetched; pages that serve a bot
challenge are reported as inaccessible (never bypassed).
"""

from __future__ import annotations

import html.parser
import re
from typing import Any
from urllib.parse import urlparse

from ..http import HttpClient
from ..ledger import EvidenceLedger, EvidenceRecord

ALLOWED_DOMAINS = (
    "fda.gov", "accessdata.fda.gov", "dailymed.nlm.nih.gov", "ema.europa.eu", "nih.gov", "ncats.nih.gov", "rarediseases.info.nih.gov",
    "n1collaborative.org", "clinicaltrials.gov", "orpha.net", "omim.org", "medlineplus.gov", "who.int", "nice.org.uk",
    "pmc.ncbi.nlm.nih.gov", "europepmc.org", "ebi.ac.uk", "uniprot.org", "clinicalgenome.org", "thegencc.org", "gnomad.broadinstitute.org",
    "mondo.monarchinitiative.org", "monarchinitiative.org", "nature.com", "cell.com", "nejm.org", "thelancet.com", "jamanetwork.com",
    "academic.oup.com", "onlinelibrary.wiley.com", "sciencedirect.com", "springer.com", "frontiersin.org", "mdpi.com", "biorxiv.org", "medrxiv.org",
    "neurology.org", "aan.com", "ilae.org", "epilepsy.com", "pediatrics.aappublications.org", "genome.gov", "decipher.genomics.org", "deciphergenomics.org",
)
CHALLENGE_MARKERS = ("Checking your browser", "Just a moment...", "recaptcha", "cf-challenge", "Access Denied")


class _Text(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "noscript"):
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "li", "h1", "h2", "h3", "h4", "tr", "br", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "noscript") and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip:
            self.parts.append(data)


def domain_allowed(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def fetch_url(http: HttpClient, ledger: EvidenceLedger, url: str, keywords: list[str] | None = None, max_chars: int = 8000) -> dict[str, Any]:
    if not url.startswith("https://") or not domain_allowed(url):
        return {"error": "Domain not on the authoritative allow-list; use the structured tools (PubMed, ClinicalTrials.gov, FDA labels, GeneReviews) instead."}
    r = http.request("GET", url, ok_statuses=(200,))
    if r.status != 200:
        ledger.log_query("fetch_url", {"url": url}, [], error=f"HTTP {r.status}")
        return {"error": f"HTTP {r.status}"}
    if any(m.lower() in r.text[:5000].lower() for m in CHALLENGE_MARKERS):
        ledger.log_query("fetch_url", {"url": url}, [], error="bot challenge")
        return {"error": "Page is protected by a bot challenge and was not accessed (not bypassed)."}
    p = _Text()
    p.feed(r.text)
    text = re.sub(r"\n\s*\n+", "\n", "".join(p.parts)).strip()
    if keywords:
        kws = [k.lower() for k in keywords]
        lines = [ln for ln in text.splitlines() if any(k in ln.lower() for k in kws)]
        text = "\n".join(lines) if lines else text
    text = text[:max_chars]
    key = f"URL:{url}"
    ledger.add(EvidenceRecord(key=key, source_type="web", title=(p.title or url).strip()[:200], url=url, content=text,
                              content_scope="full_text_excerpt", retrieved_by="fetch_url", accessed_at=r.fetched_at)).verified_exists = True
    ledger.log_query("fetch_url", {"url": url}, [key])
    return {"key": key, "title": p.title.strip(), "text": text}
