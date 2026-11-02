"""Agent D — independent claim-level verification.

1. Deterministic: every cited PMID must exist (PubMed esummary) and have its
   abstract loaded; every NCT ID must exist with its *live* status re-checked
   (cache bypassed); retracted records are flagged.
2. LLM entailment: a separate Claude invocation (different prompt, no tools,
   no access to the researchers' reasoning) judges each atomic claim against the
   text of the sources it cites.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .. import prompts
from ..http import HttpClient
from ..ledger import EvidenceLedger
from ..retrieval import clinicaltrials, ncbi
from ..rules import item_citations
from ..schemas import ActionItem, ClaimVerdict, VerificationBatch

log = logging.getLogger(__name__)


def deterministic_checks(items: list[ActionItem], ledger: EvidenceLedger, http: HttpClient) -> dict[str, Any]:
    keys = sorted({k for it in items for k in item_citations(it)})
    pmids = [k[5:] for k in keys if k.startswith("PMID:")]
    report: dict[str, Any] = {"pmids_checked": len(pmids), "pmids_missing": [], "abstracts_loaded": 0, "trials": {}}
    need_abs = [p for p in pmids if (ledger.get(f"PMID:{p}") and ledger.get(f"PMID:{p}").content_scope == "metadata")]
    for i in range(0, len(need_abs), 20):
        try:
            r = ncbi.pubmed_fetch_abstracts(http, ledger, need_abs[i : i + 20])
            report["abstracts_loaded"] += len(r.get("articles", []))
        except Exception as e:  # pragma: no cover
            report.setdefault("errors", []).append(str(e))
    if pmids:
        try:
            ex = ncbi.pmid_exists(http, pmids)
            for p, info in ex.items():
                rec = ledger.get(f"PMID:{p}")
                if rec:
                    rec.verified_exists = info["exists"]
                if not info["exists"]:
                    report["pmids_missing"].append(p)
        except Exception as e:  # pragma: no cover
            report.setdefault("errors", []).append(str(e))
    for k in keys:
        if k.startswith("NCT"):
            try:
                st = clinicaltrials.trial_status(http, k)
            except Exception as e:  # pragma: no cover
                st = {"exists": None, "error": str(e)}
            report["trials"][k] = {x: y for x, y in st.items() if x != "record"}
            rec = ledger.get(k)
            if st.get("exists"):
                # overwrite status, ages, sex and eligibility text with the live record (no stale values survive)
                clinicaltrials.refresh_record(ledger, st["record"], st.get("checked_at", ""))
            elif rec and st.get("exists") is False:
                rec.verified_exists = False
                rec.metadata["overall_status"] = "NOT_FOUND"
                rec.metadata["status_verification"] = "not_found"
            elif rec:  # live check FAILED: current status is unverified, not absent
                rec.metadata["status_verification"] = "failed"
    return report


STATEMENT_INDEX = -1  # pseudo-claim: the full rendered statement of an item


def _claims_payload(items: list[ActionItem], statements_only: bool = False) -> list[dict[str, Any]]:
    out = []
    for it in items:
        if not statements_only:
            for i, c in enumerate(it.claims):
                out.append({"item_id": it.item_id, "claim_index": i, "section": it.section.value, "item_title": it.title,
                            "claim": c.text, "kind": c.kind, "citations": c.citations})
        text = it.statement + (f" Human treatment precedent: {it.human_precedent}" if it.human_precedent.strip() else "")
        out.append({"item_id": it.item_id, "claim_index": STATEMENT_INDEX, "section": it.section.value, "item_title": it.title,
                    "claim": text, "kind": "full_item_statement", "citations": item_citations(it)})
    return out


def _sources_text(keys: list[str], ledger: EvidenceLedger, per: int = 3500, total: int = 60000) -> str:
    parts, n = [], 0
    for k in keys:
        r = ledger.get(k)
        if not r:
            continue
        meta = {m: r.metadata.get(m) for m in ("year", "journal", "pub_types", "overall_status", "min_age_years", "max_age_years", "last_updated", "retracted") if r.metadata.get(m) not in (None, "", [])}
        block = f"=== [{r.key}] {r.title}\nsource_type={r.source_type}; text_scope={r.content_scope}; meta={json.dumps(meta, default=str)}\n{(r.content or '(no text available)')[:per]}"
        if n + len(block) > total:
            break
        parts.append(block)
        n += len(block)
    return "\n\n".join(parts)


SEVERITY = {"contradicted": 4, "unsupported": 3, "partially_supported": 2, "not_checkable": 1, "supported": 0}


@dataclass
class VerificationResult:
    verdicts: list[ClaimVerdict]
    coverage: dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.coverage.get("missing_after_reverification")


def _collect(out: VerificationBatch, expected: set[tuple[str, int]], got: dict, cov: dict) -> None:
    for v in out.verdicts:
        key = (v.item_id, v.claim_index)
        if key not in expected:
            cov["malformed_or_unexpected"] += 1
            continue
        prev = got.get(key)
        if prev is None:
            got[key] = v
        elif prev.verdict != v.verdict:
            cov["conflicting_duplicates"].append(list(key))
            if SEVERITY[v.verdict] > SEVERITY[prev.verdict]:  # keep the most conservative decision
                got[key] = v
        else:
            cov["identical_duplicates"] += 1


def verify_payload(claims: list[dict[str, Any]], ledger: EvidenceLedger, llm: Any, effort: str, stage: str,
                   batch_size: int = 18) -> VerificationResult:
    """Verify claim payloads; every expected (item_id, claim_index) must receive exactly one decision.

    Missing decisions trigger ONE bounded re-verification of only the missing claims; whatever is still missing
    is reported in coverage and treated as unverified (fail closed) by the rules."""
    expected = {(c["item_id"], c["claim_index"]) for c in claims}
    got: dict[tuple[str, int], ClaimVerdict] = {}
    cov: dict[str, Any] = {"expected": len(expected), "conflicting_duplicates": [], "identical_duplicates": 0, "malformed_or_unexpected": 0,
                           "reverified": 0, "missing_after_reverification": []}

    def run(batch: list[dict[str, Any]], tag: str) -> None:
        keys = list(dict.fromkeys(k for c in batch for k in c["citations"]))
        system = prompts.render("verifier", CLAIMS=json.dumps(batch, indent=1), SOURCES=_sources_text(keys, ledger))
        out = llm.structured(stage=f"{stage}:{tag}", system=system,
                             user="Verify every claim listed. Return exactly one verdict per (item_id, claim_index).",
                             output_model=VerificationBatch, effort=effort)
        _collect(out, {(c["item_id"], c["claim_index"]) for c in batch}, got, cov)

    for b in range(0, len(claims), batch_size):
        run(claims[b : b + batch_size], f"b{b // batch_size}")
    missing = [c for c in claims if (c["item_id"], c["claim_index"]) not in got]
    if missing:
        cov["reverified"] = len(missing)
        for b in range(0, len(missing), batch_size):
            run(missing[b : b + batch_size], f"retry{b // batch_size}")
    cov["missing_after_reverification"] = [[c["item_id"], c["claim_index"]] for c in claims if (c["item_id"], c["claim_index"]) not in got]
    cov["received"] = len(got)
    order = {k: i for i, k in enumerate((c["item_id"], c["claim_index"]) for c in claims)}
    return VerificationResult(sorted(got.values(), key=lambda v: order[(v.item_id, v.claim_index)]), cov)


def llm_verify(items: list[ActionItem], ledger: EvidenceLedger, llm: Any, effort: str, batch_size: int = 18,
               statements_only: bool = False) -> VerificationResult:
    return verify_payload(_claims_payload(items, statements_only), ledger, llm, effort, "D_verify", batch_size)
