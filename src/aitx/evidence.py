"""Deterministic evidence utilities used by verification (no LLM).

* passages: split a ledger record into location-labelled passages (abstract, full-text
  sections, table rows) and select those relevant to a claim — so the verifier sees the
  supporting passage wherever it is, not just the first characters of a document
* variant notations: every common spelling of the patient's variant (p.Arg123Trp,
  Arg123Trp, p.R123W, R123W, c.367C>T, 367C>T, fs short forms, rsIDs) to detect whether
  a source discusses THIS variant rather than a neighbouring one
* quote check: a verifier's supporting quote must occur in the cited source
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .ledger import EvidenceRecord
from .variant_model import AA3, parse_protein

_WS = re.compile(r"\s+")
STOP = {"the", "and", "with", "that", "this", "from", "were", "was", "have", "has", "for", "into", "patient", "patients", "their",
        "which", "been", "also", "than", "after", "these", "those", "there", "such", "other", "more", "most", "used", "using"}


def norm(text: str) -> str:
    t = text.replace(" ", " ").replace(" ", " ").replace("–", "-").replace("—", "-").replace("’", "'")
    return _WS.sub(" ", t).strip().lower()


def passages(rec: EvidenceRecord, chunk: int = 700) -> list[tuple[str, str]]:
    """Location-labelled passages of a record. Full-text/table passages (metadata['passages']) come first-class;
    the stored content is chunked as well so nothing retrieved is invisible."""
    out: list[tuple[str, str]] = []
    for loc, txt in rec.metadata.get("passages") or []:
        if txt and txt.strip():
            out.append((str(loc), txt.strip()))
    scope = rec.content_scope or "record"
    paras = [p.strip() for p in re.split(r"\n+", rec.content or "") if p.strip()]
    for p in paras:
        if len(p) <= chunk:
            out.append((scope, p))
        else:
            sents = re.split(r"(?<=[.;])\s+", p)
            buf = ""
            for s in sents:
                if len(buf) + len(s) > chunk and buf:
                    out.append((scope, buf.strip()))
                    buf = ""
                buf += s + " "
            if buf.strip():
                out.append((scope, buf.strip()))
    return out


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9\-\.>*]{2,}", text.lower()) if w not in STOP}


# Passages that qualify, contradict or limit a positive result. Claim-term scoring alone can miss these, because a
# harm or null finding is often worded with none of the claim's vocabulary ("unaffected littermates showed reduced
# rotarod latency"). They are reserved a separate budget so a verifier never sees only the supporting half of a source.
CONTRAST = re.compile(
    r"\b(no significant|not significant|did not|failed to|without (?:any )?(?:significant |detectable )?(?:effect|benefit|improvement|change)|"
    r"no (?:significant )?(?:effect|benefit|improvement|difference|change|response)|unchanged|"
    r"worsen\w*|deteriorat\w*|impair\w*|exacerbat\w*|aggravat\w*|detriment\w*|harm\w*|"
    r"adverse|toxic\w*|side effects?|mortality|lethal\w*|died|death[s]?|"
    r"wild[- ]type|wildtype|unaffected (?:littermates?|controls?|mice|animals)|control (?:mice|animals|group)|"
    r"limitation[s]?|caveat|however|in contrast|conversely|but not|only in|whereas)\b", re.I)


def contrast_passages(ps: list[tuple[str, str]]) -> list[int]:
    """Indices of passages carrying a negative, null, harmful or limiting signal."""
    return [i for i, (_, t) in enumerate(ps) if CONTRAST.search(t)]


def select_passages(rec: EvidenceRecord, query: str, extra_terms: Iterable[str] = (), max_chars: int = 3500) -> tuple[str, dict[str, Any]]:
    """Most relevant passages for `query` (claim text + intervention + variant notations), in document order,
    always including the record head. Returns (text, info) where info states how much was shown."""
    ps = passages(rec)
    if not ps:
        return "(no text available for this source)", {"n_passages": 0, "shown": 0}
    q = _terms(query) | {t.lower() for t in extra_terms if t}
    scored = []
    for i, (loc, txt) in enumerate(ps):
        low = txt.lower()
        s = sum(1 for t in q if t in low) + (3 if any(t in low for t in extra_terms if t) else 0)
        scored.append((s, i))
    head_loc, head_txt = ps[0]
    ps[0] = (head_loc, head_txt if len(head_txt) <= 600 else head_txt[:600] + " …")  # head never consumes the budget
    keep = {0}  # head (title/abstract start or first full-text passage)
    used = len(ps[0][1])
    for s, i in sorted(scored, key=lambda x: (-x[0], x[1])):
        if s <= 0 or i in keep:
            continue
        if used + len(ps[i][1]) > max_chars:
            continue
        keep.add(i)
        used += len(ps[i][1])
    # Reserve a separate budget for negative/null/harmful passages, so a source's qualifying evidence is never
    # dropped merely because it shares no vocabulary with the claim being checked.
    contrast = contrast_passages(ps)
    c_used, c_budget = 0, max(600, max_chars // 3)
    for i in sorted(contrast, key=lambda i: (-scored[i][0], i)):
        if i in keep:
            continue
        if c_used + len(ps[i][1]) > c_budget:
            continue
        keep.add(i)
        c_used += len(ps[i][1])
    text = "\n".join(f"[{ps[i][0]}] {ps[i][1]}" for i in sorted(keep))
    return text, {"n_passages": len(ps), "shown": len(keep), "contrast_passages": len(contrast),
                  "contrast_shown": len([i for i in keep if i in set(contrast)])}


# ---------------------------------------------------------------- variant notations

def variant_notations(variant_cdna: str, variant_protein: str, rsids: Iterable[str] = ()) -> set[str]:
    out: set[str] = set()
    c = variant_cdna.split(":")[-1].strip()
    if c:
        out |= {c, c.replace("c.", "", 1)}
    p = parse_protein(variant_protein)
    raw = variant_protein.split(":")[-1].replace("(", "").replace(")", "").strip()
    if raw and raw not in ("p.?", "p.="):
        out |= {raw, raw.replace("p.", "", 1)}
    if p.position and p.ref_aa:
        r1 = AA3.get(p.ref_aa, "")
        a1 = AA3.get(p.alt_aa, "") if p.alt_aa else ""
        if p.kind in ("missense", "nonsense", "synonymous") and r1 and a1:
            short = f"{r1}{p.position}{a1}"
            out |= {short, f"p.{short}", f"{p.ref_aa}{p.position}{p.alt_aa}"}
            if a1 == "*":
                out |= {f"{r1}{p.position}X", f"{r1}{p.position}Ter"}
        if p.kind == "frameshift" and r1:
            out |= {f"{p.ref_aa}{p.position}fs", f"{r1}{p.position}fs", f"p.{r1}{p.position}fs"}
            if p.alt_aa and AA3.get(p.alt_aa) and p.fs_ter_offset:
                out |= {f"{r1}{p.position}{AA3[p.alt_aa]}fs*{p.fs_ter_offset}", f"{r1}{p.position}{AA3[p.alt_aa]}fsX{p.fs_ter_offset}"}
    out |= {r for r in rsids if r}
    return {o for o in out if len(o) >= 3}


def mentions_variant(text: str, notations: Iterable[str]) -> list[str]:
    """Notations of the patient's variant that occur in text (token-bounded, so R123W does not match R123WX)."""
    found = []
    for n in notations:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", text):
            found.append(n)
    return sorted(set(found))


def record_text(rec: EvidenceRecord) -> str:
    return (rec.title or "") + "\n" + (rec.content or "") + "\n" + "\n".join(t for _, t in rec.metadata.get("passages") or [])


def quote_in_source(quote: str, rec: EvidenceRecord | None) -> bool:
    """True if the (whitespace/case-normalised) quote occurs in the record; long quotes may be elided with '...'."""
    if rec is None or not quote.strip():
        return False
    hay = norm(record_text(rec))
    parts = [norm(p) for p in re.split(r"\.\.\.|…", quote) if norm(p)]
    if not parts:
        return False
    if len(parts) == 1:
        return len(parts[0]) >= 8 and parts[0] in hay
    return all(len(p) >= 12 and p in hay for p in parts)
