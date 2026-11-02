"""Evidence ledger.

Every record any tool retrieves is registered here under a canonical key
(``PMID:12345``, ``NCT01234567``, ``CLINVAR:VCV000000000``, ``GENEREVIEWS:NBK0000#Management`` ...).
LLM stages may cite *only* ledger keys; the renderer turns keys into links
using the URL stored here, so citation URLs are never produced by a model.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

KEY_RE = re.compile(
    r"\[((?:PMID|PMCID|CLINVAR|GENEREVIEWS|FDALABEL|UNIPROT|CLINGEN|GNOMAD|VARIANT|REFSEQ|ENSEMBL|CLINPGX|URL|COMPUTED|EPMC)"
    r":[^\]\s]+|NCT\d{8})\]"
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class EvidenceRecord:
    key: str
    source_type: str  # pubmed | clinicaltrials | clinvar | genereviews | fda_label | uniprot | clingen | gnomad | variant | computed | web | clinpgx | europepmc
    title: str
    url: str
    content: str = ""  # text available for claim verification
    metadata: dict[str, Any] = field(default_factory=dict)
    accessed_at: str = field(default_factory=now_iso)
    retrieved_by: str = ""
    content_scope: str = "metadata"  # metadata | abstract | full_text_excerpt | structured_record | computed
    verified_exists: bool | None = None  # set by the deterministic verifier

    def citation_label(self) -> str:
        k = self.key
        if k.startswith("PMID:"):
            return f"PMID {k[5:]}"
        if k.startswith("NCT"):
            return k
        if k.startswith("CLINVAR:"):
            return f"ClinVar {k[8:]}"
        if k.startswith("GENEREVIEWS:"):
            return f"GeneReviews {k.split(':', 1)[1].split('#')[0]}" + (f" ({self.metadata.get('section_title')})" if self.metadata.get("section_title") else "")
        if k.startswith("FDALABEL:"):
            return f"FDA label: {self.metadata.get('drug', self.title)}"
        if k.startswith("UNIPROT:"):
            return f"UniProt {k[8:]}"
        if k.startswith("CLINGEN:"):
            return "ClinGen " + self.metadata.get("label", "curation")
        if k.startswith("GNOMAD:"):
            return f"gnomAD constraint ({self.metadata.get('gene', '')})"
        if k.startswith("REFSEQ:"):
            return f"RefSeq {k[7:]}"
        if k.startswith("VARIANT:"):
            return self.metadata.get("label", "Variant annotation")
        if k.startswith("COMPUTED:"):
            return self.metadata.get("label", "Computed analysis")
        if k.startswith("CLINPGX:"):
            return f"ClinPGx {k[8:]}"
        if k.startswith("PMCID:"):
            return k[6:]
        return self.title[:80] or k


@dataclass
class QueryLogEntry:
    tool: str
    args: dict[str, Any]
    timestamp: str
    n_results: int
    result_keys: list[str]
    error: str = ""
    total_available: int | None = None  # hits the source reported; > n_results means only the first page was reviewed


class EvidenceLedger:
    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}
        self.queries: list[QueryLogEntry] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------ records
    def add(self, rec: EvidenceRecord) -> EvidenceRecord:
        with self._lock:
            old = self._records.get(rec.key)
            if old is None:
                self._records[rec.key] = rec
                return rec
            # merge: keep the richest content, union metadata
            if len(rec.content) > len(old.content):
                old.content = rec.content
                old.content_scope = rec.content_scope
            old.metadata.update({k: v for k, v in rec.metadata.items() if v not in (None, "", [])})
            if not old.title and rec.title:
                old.title = rec.title
            return old

    def add_many(self, recs: Iterable[EvidenceRecord]) -> list[EvidenceRecord]:
        return [self.add(r) for r in recs]

    def get(self, key: str) -> EvidenceRecord | None:
        return self._records.get(normalize_key(key))

    def __contains__(self, key: str) -> bool:
        return normalize_key(key) in self._records

    def keys(self) -> list[str]:
        return list(self._records)

    def records(self) -> list[EvidenceRecord]:
        return list(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    # ------------------------------------------------------------ queries
    def log_query(self, tool: str, args: dict[str, Any], result_keys: list[str], error: str = "", total: int | None = None) -> None:
        with self._lock:
            self.queries.append(QueryLogEntry(tool, dict(args), now_iso(), len(result_keys), list(result_keys), error, total))

    def searches_for(self, *tools: str) -> list[QueryLogEntry]:
        return [q for q in self.queries if q.tool in tools]

    def failed_queries(self, *tools: str) -> list[QueryLogEntry]:
        return [q for q in self.queries if q.error and (not tools or q.tool in tools)]

    def retrieval_status(self) -> dict[str, dict[str, int]]:
        """Per source/tool: queries that returned records, returned nothing, or FAILED,
        plus how many were TRUNCATED (the source reported more hits than were reviewed).

        'empty' means the query ran and the source had no matching record (not
        proof of biological absence); 'failed' means the source could not be
        queried (timeout/HTTP error, or a backend error/malformed body inside an
        HTTP 200 response) and nothing can be concluded; 'partial' (a subset of
        'failed') means some results were retrieved before the failure, so the
        search is incomplete; 'truncated' means more hits existed than were
        reviewed."""
        out: dict[str, dict[str, int]] = {}
        for q in self.queries:
            d = out.setdefault(q.tool, {"with_results": 0, "empty": 0, "failed": 0, "truncated": 0, "partial": 0})
            if not q.error and q.total_available is not None and q.total_available > q.n_results:
                d["truncated"] += 1
            if q.error:
                d["failed"] += 1
                if q.n_results:
                    d["partial"] += 1
            elif q.n_results:
                d["with_results"] += 1
            else:
                d["empty"] += 1
        return out

    # ------------------------------------------------------------ prompts
    def digest(self, keys: Iterable[str] | None = None, max_chars_per_record: int = 900, max_total: int = 60000) -> str:
        """Compact text view of records for LLM prompts."""
        out, total = [], 0
        for k in keys if keys is not None else self._records:
            r = self.get(k)
            if r is None:
                continue
            body = r.content[:max_chars_per_record].replace("\n", " ")
            meta = {m: r.metadata[m] for m in ("year", "journal", "pub_types", "status", "drug", "section_title") if m in r.metadata}
            line = f"[{r.key}] ({r.source_type}; {r.content_scope}) {r.title}\n  meta={meta}\n  {body}"
            if total + len(line) > max_total:
                out.append(f"... ({len(self._records) - len(out)} more records omitted)")
                break
            out.append(line)
            total += len(line)
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        return {"records": [asdict(r) for r in self._records.values()], "queries": [asdict(q) for q in self.queries]}


def normalize_key(key: str) -> str:
    k = key.strip().strip("[]").strip()
    if re.fullmatch(r"(?i)nct\d{8}", k):
        return k.upper()
    m = re.fullmatch(r"(?i)pmid:?\s*(\d+)", k)
    if m:
        return f"PMID:{m.group(1)}"
    return k


def extract_keys(text: str) -> list[str]:
    """All bracketed ledger keys cited in a text, in order of appearance."""
    return [normalize_key(m.group(1)) for m in KEY_RE.finditer(text)]


def find_bracket_tokens(text: str) -> list[str]:
    """Any [TOKEN:...] or [NCT...] looking citations, including unknown prefixes."""
    return [normalize_key(m) for m in re.findall(r"\[([A-Z][A-Za-z]+:[^\]\s]+|NCT\d{8})\]", text)]
