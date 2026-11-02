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
import re
from dataclasses import dataclass, field
from typing import Any

from .. import prompts
from ..http import HttpClient
from ..ledger import EvidenceLedger
from ..retrieval import clinicaltrials, europepmc, ncbi
from ..rules import item_citations
from ..schemas import ActionItem, ClaimVerdict, Section, VerificationBatch

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
    # open-access full text (all sections + table rows) for every cited article, so the verifier sees results/patient tables
    report["fulltext"] = {}
    for k in keys:
        rec = ledger.get(k)
        if rec is None or rec.source_type not in ("pubmed", "europepmc") or rec.metadata.get("passages"):
            continue
        pmcid = rec.metadata.get("pmcid") or (k[6:] if k.startswith("PMCID:") else "")
        if pmcid:
            report["fulltext"][k] = europepmc.load_fulltext(http, ledger, k, pmcid)
        else:
            rec.metadata["fulltext_status"] = "no_open_access_full_text (abstract/metadata only; tables and supplements not accessible)"
            report["fulltext"][k] = "abstract_only"
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


@dataclass
class VerifyContext:
    """Patient-specific context given to EVERY verification decision."""

    patient_block: str = "(no patient context)"
    gene: str = ""
    notations: set[str] = field(default_factory=set)

    @classmethod
    def from_case(cls, ctx: Any) -> "VerifyContext":
        from ..evidence import variant_notations

        case = ctx.case
        nots: set[str] = set()
        vlines = []
        for v in case.patient.genotype:
            nots |= variant_notations(v.variant_cdna, v.variant_protein)
            vlines.append(f"{v.gene} {v.transcript}:{v.variant_cdna} {v.variant_protein} ({v.zygosity})")
        it = ctx.interpretation
        mech = ""
        if it is not None:
            mech = "; ".join(f"{x.gene} {x.hgvs_p}: mechanism={x.mechanism_label} ({x.mechanism_confidence}) — {x.variant_mechanism_assessment[:300]}"
                             for x in it.variants)
        block = (f"Genotype: {'; '.join(vlines)}\nClinical context: {case.patient.clinical_context}\n"
                 f"Age (years): {ctx.clinical.age_years}; sex: {ctx.clinical.sex}\n"
                 f"Diagnosis (interpretation stage, itself a model output): {it.disease_name if it else 'n/a'}\n"
                 f"Variant mechanism (interpretation stage, uncertain unless stated): {mech or 'n/a'}\n"
                 f"Notations of the patient's exact variant: {', '.join(sorted(nots))}")
        return cls(block, case.patient.genotype[0].gene, nots)


def _statement_text(it: ActionItem) -> str:
    """Everything that will be rendered for the item, so no rendered field escapes verification."""
    parts = [it.statement]
    if it.section == Section.BESPOKE:
        parts.insert(0, f"Verdict: {it.bespoke_verdict or 'UNCERTAIN'}.")
    if it.human_precedent.strip():
        parts.append(f"Human treatment precedent: {it.human_precedent}")
    if it.conditional_action.strip():
        parts.append(f"Conditional action: {it.conditional_action}")
    if it.criteria_met:
        parts.append("Criteria met on available data: " + "; ".join(it.criteria_met))
    if it.criteria_unassessable:
        parts.append("Cannot be assessed from the case: " + "; ".join(it.criteria_unassessable))
    if it.potential_exclusions:
        parts.append("Potential exclusions: " + "; ".join(it.potential_exclusions))
    return " ".join(parts)


def _claims_payload(items: list[ActionItem], statements_only: bool = False) -> list[dict[str, Any]]:
    out = []
    for it in items:
        if not statements_only:
            for i, c in enumerate(it.claims):
                out.append({"item_id": it.item_id, "claim_index": i, "section": it.section.value, "intervention": it.title,
                            "claim": c.text, "kind": c.kind, "citations": c.citations})
        out.append({"item_id": it.item_id, "claim_index": STATEMENT_INDEX, "section": it.section.value, "intervention": it.title,
                    "claim": _statement_text(it), "kind": "full_item_statement", "citations": item_citations(it)})
    return out


def _evidence_packet(claim: dict[str, Any], ledger: EvidenceLedger, vctx: VerifyContext, per_source: int = 3000) -> str:
    """Claim-specific evidence: the most relevant passages (abstract, full-text sections, table rows) of every cited
    source, with access scope and whether the source mentions the patient's exact variant."""
    from ..evidence import mentions_variant, record_text, select_passages

    blocks = []
    for k in claim["citations"]:
        r = ledger.get(k)
        if not r:
            blocks.append(f"=== [{k}] (not in evidence ledger)")
            continue
        meta = {m: r.metadata.get(m) for m in ("year", "journal", "pub_types", "overall_status", "min_age_years", "max_age_years", "last_updated",
                                               "retracted", "fulltext_status", "supplementary_files") if r.metadata.get(m) not in (None, "", [])}
        text, info = select_passages(r, claim["claim"] + " " + claim.get("intervention", ""),
                                     extra_terms=sorted(vctx.notations) + [w for w in re.findall(r"[A-Za-z0-9-]{4,}", claim.get("intervention", ""))],
                                     max_chars=per_source)
        found = mentions_variant(record_text(r), vctx.notations) if vctx.notations else []
        blocks.append(f"=== [{r.key}] {r.title}\nsource_type={r.source_type}; text_scope={r.content_scope}; meta={json.dumps(meta, default=str)[:400]}\n"
                      f"patient_variant_mentioned_in_source={'yes: ' + ', '.join(found) if found else 'no'}; "
                      f"passages_shown={info['shown']} of {info['n_passages']}\n{text}")
    return "\n".join(blocks) if blocks else "(no citation: reasoning claim)"


SEVERITY = {"contradicted": 5, "unsupported": 4, "insufficient_source_text": 3, "partially_supported": 2, "not_checkable": 1, "supported": 0}


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


def ground_verdict(v: ClaimVerdict, claim: dict[str, Any], ledger: EvidenceLedger, vctx: VerifyContext, cov: dict) -> ClaimVerdict:
    """Deterministic checks on a verifier decision (cannot be talked around by the model):
    * a supported/partially supported cited claim needs a verbatim quote from one of ITS cited sources
    * 'same_variant' applicability needs the patient's variant notation in a cited source
    * a 'supported' claim without any citation cannot be grounded -> not_checkable"""
    from ..evidence import mentions_variant, quote_in_source, record_text

    upd: dict[str, Any] = {}
    cites = claim["citations"]
    if v.verdict in ("supported", "partially_supported"):
        if not cites:
            if v.verdict == "supported":
                upd["verdict"] = "not_checkable"
                cov["uncited_supported_to_not_checkable"] += 1
        elif v.quote_source_key not in cites or not quote_in_source(v.supporting_quote, ledger.get(v.quote_source_key)):
            upd["verdict"] = "insufficient_source_text"
            upd["rationale"] = "[deterministic] supporting quote not found verbatim in a cited source; " + v.rationale
            cov["quote_failures"] += 1
    if v.applicability == "same_variant" and vctx.notations:
        if not any(ledger.get(k) and mentions_variant(record_text(ledger.get(k)), vctx.notations) for k in cites):
            upd["applicability"] = "same_gene_other_variant_or_mechanism"
            upd["overgeneralization"] = (v.overgeneralization + " [deterministic] no cited source mentions the patient's exact variant.").strip()
            cov["applicability_downgrades"] += 1
    return v.model_copy(update=upd) if upd else v


def verify_payload(claims: list[dict[str, Any]], ledger: EvidenceLedger, llm: Any, effort: str, stage: str,
                   vctx: VerifyContext | None = None, max_claims: int = 12, max_chars: int = 45000) -> VerificationResult:
    """Verify claim payloads with patient context and claim-specific evidence packets.

    Every expected (item_id, claim_index) must receive exactly one decision; missing decisions trigger ONE bounded
    re-verification; whatever is still missing is reported and treated as unverified (fail closed). Batches are sized
    by the evidence they carry, so no source text is silently dropped."""
    vctx = vctx or VerifyContext()
    expected = {(c["item_id"], c["claim_index"]) for c in claims}
    got: dict[tuple[str, int], ClaimVerdict] = {}
    cov: dict[str, Any] = {"expected": len(expected), "conflicting_duplicates": [], "identical_duplicates": 0, "malformed_or_unexpected": 0,
                           "reverified": 0, "missing_after_reverification": [], "quote_failures": 0, "applicability_downgrades": 0,
                           "uncited_supported_to_not_checkable": 0, "batches": 0}
    packets = {(c["item_id"], c["claim_index"]): _evidence_packet(c, ledger, vctx) for c in claims}

    def batches(cs):
        cur, size = [], 0
        for c in cs:
            n = len(packets[(c["item_id"], c["claim_index"])]) + len(c["claim"])
            if cur and (len(cur) >= max_claims or size + n > max_chars):
                yield cur
                cur, size = [], 0
            cur.append(c)
            size += n
        if cur:
            yield cur

    def run(batch: list[dict[str, Any]], tag: str) -> None:
        cov["batches"] += 1
        sources = "\n\n".join(f"##### Evidence for item_id={c['item_id']} claim_index={c['claim_index']}\n{packets[(c['item_id'], c['claim_index'])]}"
                                for c in batch)
        system = prompts.render("verifier", PATIENT=vctx.patient_block, CLAIMS=json.dumps(batch, indent=1), SOURCES=sources)
        out = llm.structured(stage=f"{stage}:{tag}", system=system,
                             user="Verify every claim listed. Return exactly one verdict per (item_id, claim_index).",
                             output_model=VerificationBatch, effort=effort)
        _collect(out, {(c["item_id"], c["claim_index"]) for c in batch}, got, cov)

    for n, b in enumerate(batches(claims)):
        run(b, f"b{n}")
    missing = [c for c in claims if (c["item_id"], c["claim_index"]) not in got]
    if missing:
        cov["reverified"] = len(missing)
        for n, b in enumerate(batches(missing)):
            run(b, f"retry{n}")
    by_key = {(c["item_id"], c["claim_index"]): c for c in claims}
    for k in list(got):
        got[k] = ground_verdict(got[k], by_key[k], ledger, vctx, cov)
    cov["missing_after_reverification"] = [[c["item_id"], c["claim_index"]] for c in claims if (c["item_id"], c["claim_index"]) not in got]
    cov["received"] = len(got)
    order = {k: i for i, k in enumerate((c["item_id"], c["claim_index"]) for c in claims)}
    return VerificationResult(sorted(got.values(), key=lambda v: order[(v.item_id, v.claim_index)]), cov)


def llm_verify(items: list[ActionItem], ledger: EvidenceLedger, llm: Any, effort: str, statements_only: bool = False,
               vctx: VerifyContext | None = None) -> VerificationResult:
    return verify_payload(_claims_payload(items, statements_only), ledger, llm, effort, "D_verify", vctx)
