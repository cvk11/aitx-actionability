"""Deterministic, generalizable report rules (no LLM).

These implement the official template's tier definitions and the challenge's
citation requirements as code, so they hold regardless of model behaviour.
Every change is recorded as a `RuleEvent` in the audit trail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ledger import EvidenceLedger, extract_keys, find_bracket_tokens
from .retrieval.clinicaltrials import EXPANDED_ACCESS_OPEN, OPEN_STATUSES
from .schemas import ActionItem, ClaimVerdict, Section

HUMAN_PUBTYPES = {"Case Reports", "Clinical Trial", "Clinical Study", "Clinical Trial, Phase I", "Clinical Trial, Phase II",
                  "Clinical Trial, Phase III", "Randomized Controlled Trial", "Observational Study", "Multicenter Study", "Comparative Study"}
GUIDELINE_SOURCES = {"fda_label", "genereviews"}
LITERATURE_SOURCES = {"pubmed", "europepmc"}
HUMAN_PRECEDENT_TYPES = {"direct_human_same_variant", "direct_human_same_gene", "direct_human_same_disease"}
OK_VERDICTS = {"supported", "not_checkable"}
# claim kinds that carry an item's core assertion: trials/contraindications without a supported core claim are dropped
REQUIRED_KINDS = {
    Section.TRIALS: {"trial_status_or_eligibility"},
    Section.CONTRAINDICATIONS: {"safety_or_contraindication"},
}


@dataclass
class RuleEvent:
    item_id: str
    rule: str
    action: str  # removed_citation | moved | dropped | flagged | set_field
    detail: str


@dataclass
class RuleResult:
    items: list[ActionItem]
    events: list[RuleEvent] = field(default_factory=list)
    problems: dict[str, list[str]] = field(default_factory=dict)  # item_id -> problems requiring LLM revision
    # item_id -> verdict of the FULL rendered statement: supported | not_checkable | partially_supported | unsupported |
    # contradicted | unverified | needs_reverification (only "supported"/"not_checkable" may be rendered)
    statement_status: dict[str, str] = field(default_factory=dict)
    # claim verdicts re-indexed to the kept claims (for later re-application)
    reindexed: list[ClaimVerdict] = field(default_factory=list)

    def problem(self, item_id: str, text: str) -> None:
        self.problems.setdefault(item_id, []).append(text)


def _strip_unknown_citations(text: str, ledger: EvidenceLedger) -> tuple[str, list[str]]:
    removed = []
    for tok in set(find_bracket_tokens(text)):
        if tok not in ledger:
            removed.append(tok)
            text = re.sub(r"\s*\[" + re.escape(tok) + r"\]", "", text)
    return text, removed


def item_citations(item: ActionItem) -> list[str]:
    keys = extract_keys(item.statement) + extract_keys(item.human_precedent) + extract_keys(item.conditional_action)
    for c in item.claims:
        keys += c.citations
    seen, out = set(), []
    for k in keys:
        from .ledger import normalize_key

        k = normalize_key(k)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _is_human_record(ledger: EvidenceLedger, key: str) -> bool:
    """Deterministic PRE-filter only: a literature record whose publication type denotes human clinical data.

    Words such as "patient" are deliberately NOT used (a mouse study can say "no patients received the drug").
    Whether the record really documents the intervention given to patients in the same gene/disease/variant
    context, with an outcome, is decided by claim-level verification (`apply_verdicts`)."""
    r = ledger.get(key)
    if r is None or r.source_type not in LITERATURE_SOURCES:
        return False
    return bool(set(r.metadata.get("pub_types") or []) & HUMAN_PUBTYPES)


def apply_citation_rules(items: list[ActionItem], ledger: EvidenceLedger) -> RuleResult:
    res = RuleResult(items=[])
    for it in items:
        it = it.model_copy(deep=True)
        it.statement, rem = _strip_unknown_citations(it.statement, ledger)
        it.human_precedent, rem2 = _strip_unknown_citations(it.human_precedent, ledger)
        it.conditional_action, rem3 = _strip_unknown_citations(it.conditional_action, ledger)
        for tok in rem + rem2 + rem3:
            res.events.append(RuleEvent(it.item_id, "citation_not_in_ledger", "removed_citation", tok))
        for c in it.claims:
            bad = [k for k in c.citations if k not in ledger]
            if bad:
                c.citations = [k for k in c.citations if k in ledger]
                res.events.append(RuleEvent(it.item_id, "claim_citation_not_in_ledger", "removed_citation", ",".join(bad)))
        for k in item_citations(it):
            r = ledger.get(k)
            if r is not None and r.metadata.get("retracted"):
                it.statement = re.sub(r"\s*\[" + re.escape(k) + r"\]", "", it.statement)
                for c in it.claims:
                    c.citations = [x for x in c.citations if x != k]
                res.events.append(RuleEvent(it.item_id, "retracted_source", "removed_citation", k))
                res.problem(it.item_id, f"Cited source {k} is retracted; remove dependent statements.")
        if not item_citations(it):
            res.problem(it.item_id, "Item has no valid ledger citation; support it with retrieved evidence or drop it.")
        res.items.append(it)
    return res


def apply_section_rules(items: list[ActionItem], ledger: EvidenceLedger, age_years: float | None, sex: str) -> RuleResult:
    res = RuleResult(items=[])
    for it in items:
        it = it.model_copy(deep=True)
        cites = item_citations(it)
        # ---------------- clinical trials
        if it.section == Section.TRIALS:
            nct = it.nct_id.strip().upper() or next((k for k in cites if k.startswith("NCT")), "")
            rec = ledger.get(nct) if nct else None
            if rec is None:
                res.events.append(RuleEvent(it.item_id, "trial_not_retrieved", "dropped", nct or "no NCT id"))
                continue
            it.nct_id = nct
            status = rec.metadata.get("overall_status")
            if status not in OPEN_STATUSES + EXPANDED_ACCESS_OPEN:
                res.events.append(RuleEvent(it.item_id, "trial_not_recruiting", "dropped", f"{nct} status {status}"))
                continue
            if status != "RECRUITING":
                # not actively recruiting: kept only with an explicit status label and ranked below recruiting trials
                it.priority = max(it.priority, 50)
            if rec.metadata.get("status_verification") == "failed":
                it.criteria_unassessable = list(dict.fromkeys(
                    it.criteria_unassessable + ["Current recruitment status and eligibility could not be re-verified live in this run"]))
            lo, hi = rec.metadata.get("min_age_years"), rec.metadata.get("max_age_years")
            if age_years is not None and ((lo is not None and age_years < lo - 1e-6) or (hi is not None and age_years > hi + 1e-6)):
                res.events.append(RuleEvent(it.item_id, "trial_age_ineligible", "dropped", f"{nct} ages {lo}-{hi}, patient {age_years}"))
                continue
            tsex = (rec.metadata.get("sex") or "ALL").upper()
            if sex in ("male", "female") and tsex in ("MALE", "FEMALE") and tsex.lower() != sex:
                res.events.append(RuleEvent(it.item_id, "trial_sex_ineligible", "dropped", f"{nct} sex {tsex}"))
                continue
            if age_years is None:
                it.criteria_unassessable = list(dict.fromkeys(it.criteria_unassessable + ["Patient age (not stated in case)"]))
            if re.search(r"\b(is|are) eligible\b", it.statement, re.I):
                res.problem(it.item_id, "Statement asserts definite eligibility; rephrase as 'may qualify' with unassessable criteria.")
        # ---------------- off-label requires human precedent
        elif it.section == Section.OFF_LABEL:
            human = [k for k in cites if _is_human_record(ledger, k)]
            if not human:
                it.section = Section.REPURPOSING
                it.human_precedent = ""
                res.events.append(RuleEvent(it.item_id, "offlabel_without_human_clinical_record", "moved", "-> drug_repurposing"))
                res.problem(it.item_id, "Moved to Drug Repurposing: no cited human clinical publication; restate the item as a preclinical/"
                                        "mechanistic hypothesis and say that no human treatment precedent was identified in the sources searched.")
            elif not it.human_precedent.strip():
                res.problem(it.item_id, "Off-label item lacks an explicit 'Human treatment precedent' description.")
        # ---------------- established targeted requires approval/guideline-level support
        elif it.section == Section.EXISTING_TARGETED:
            strong = [k for k in cites if (ledger.get(k) and ledger.get(k).source_type in GUIDELINE_SOURCES)]
            if it.evidence_level not in ("regulatory_approval", "guideline_or_expert_review") or not strong:
                human = [k for k in cites if _is_human_record(ledger, k)]
                target = Section.OFF_LABEL if human else Section.REPURPOSING
                it.section = target
                res.events.append(RuleEvent(it.item_id, "targeted_without_approval_or_guideline", "moved", f"-> {target.value}"))
                res.problem(it.item_id, f"Moved to {target.value}: no label/guideline evidence for an established targeted therapy.")
        # ---------------- bespoke needs a verdict
        elif it.section == Section.BESPOKE and not it.bespoke_verdict:
            it.bespoke_verdict = "UNCERTAIN"
            res.events.append(RuleEvent(it.item_id, "bespoke_missing_verdict", "set_field", "UNCERTAIN"))
        res.items.append(it)
    return res


def _cites_source(c, ledger: EvidenceLedger, kinds: set[str]) -> bool:
    return any((ledger.get(k) is not None and ledger.get(k).source_type in kinds) for k in c.citations)


def apply_verdicts(items: list[ActionItem], verdicts: list[ClaimVerdict], *, ledger: EvidenceLedger | None = None,
                   statements_only: bool = False) -> RuleResult:
    """Apply claim-level verification results — FAIL CLOSED.

    * A claim without a decision is unverified and removed; an item whose full statement has no decision is
      marked `unverified` and can never be rendered.
    * supported / not_checkable claims are kept; partially supported claims are kept for traceability but do
      not count as support, and the verifier's suggested wording is NOT applied (it is passed to the revision
      step and must be re-verified).
    * Evidence-based tier gate (needs `ledger`): Off-label requires a SUPPORTED human-treatment claim with a
      direct_human_* evidence type citing a literature record; Existing Targeted requires a SUPPORTED
      regulatory/treatment claim citing a label or GeneReviews. Items failing the gate are moved down a tier
      when supported lower-tier evidence remains (e.g., preclinical -> Drug Repurposing) — never silently lost —
      and their statements must be rewritten and re-verified.
    * With `statements_only`, claim decisions are taken as previously established (only the -1 statement
      verdict is new) — used after a revision that changed only the statement."""
    by_item: dict[str, dict[int, ClaimVerdict]] = {}
    for v in verdicts:
        by_item.setdefault(v.item_id, {})[v.claim_index] = v
    res = RuleResult(items=[])
    for it in items:
        it = it.model_copy(deep=True)
        vs = by_item.get(it.item_id, {})
        kept: list = []
        kept_v: list[ClaimVerdict | None] = []
        for i, c in enumerate(it.claims):
            v = vs.get(i)
            if v is None:
                if statements_only:
                    kept.append(c)
                    kept_v.append(None)
                    continue
                res.events.append(RuleEvent(it.item_id, "claim_unverified", "removed_claim", c.text[:160]))
                res.problem(it.item_id, f"Claim could not be verified (no decision): '{c.text[:160]}'.")
                continue
            if v.verdict in OK_VERDICTS:
                kept.append(c)
                kept_v.append(v)
            elif v.verdict == "partially_supported":
                kept.append(c)
                kept_v.append(v)
                hint = f" Verifier-suggested wording (unverified, must be re-checked): '{v.suggested_revision[:200]}'" if v.suggested_revision.strip() else ""
                res.problem(it.item_id, f"Claim only partially supported: '{c.text[:160]}' — {v.rationale[:200]}.{hint}")
            else:
                res.events.append(RuleEvent(it.item_id, f"claim_{v.verdict}", "removed_claim", f"{c.text[:160]} | {v.rationale[:200]}"))
                res.problem(it.item_id, f"Claim {v.verdict}: '{c.text[:160]}' — {v.rationale[:200]}. Remove or correct it in the statement.")
            if v.overgeneralization.strip():
                res.problem(it.item_id, f"Overgeneralisation: {v.overgeneralization[:200]}")
        it.claims = kept
        # -------- full statement
        sv = vs.get(-1)
        status = sv.verdict if sv is not None else "unverified"
        if sv is None:
            res.events.append(RuleEvent(it.item_id, "statement_unverified", "flagged", "no decision for the full statement"))
            res.problem(it.item_id, "Full statement could not be verified (no decision); it cannot be rendered unless re-verified.")
        elif sv.verdict == "partially_supported":
            hint = f" Verifier-suggested wording (unverified): '{sv.suggested_revision[:300]}'" if sv.suggested_revision.strip() else ""
            res.problem(it.item_id, f"Full statement partially supported: {sv.rationale[:250]}. Rewrite it to say only what the cited sources support.{hint}")
        elif sv.verdict in ("unsupported", "contradicted"):
            res.problem(it.item_id, f"Full statement {sv.verdict}: {sv.rationale[:250]}. Rewrite it to say only what the cited sources support.")
        if sv is not None and sv.overgeneralization.strip():
            res.problem(it.item_id, f"Overgeneralisation in statement: {sv.overgeneralization[:200]}")
        ok = [(c, v) for c, v in zip(kept, kept_v) if v is None or v.verdict in OK_VERDICTS]
        if not ok:
            res.events.append(RuleEvent(it.item_id, "no_supported_claims", "dropped", ""))
            continue
        req = REQUIRED_KINDS.get(it.section, set())
        if req and not any(c.kind in req for c, _ in ok):
            res.events.append(RuleEvent(it.item_id, "core_claims_unsupported", "dropped", f"no supported {sorted(req)} claim remains"))
            continue
        # -------- evidence-based tier gate
        if ledger is not None and not statements_only:
            def human_precedent() -> bool:
                return any(c.kind == "treatment_effect_human" and v is not None and v.verdict == "supported"
                           and v.evidence_type in HUMAN_PRECEDENT_TYPES and _cites_source(c, ledger, LITERATURE_SOURCES) for c, v in ok)

            def demote(target: Section, rule: str, why: str) -> None:
                nonlocal status
                res.events.append(RuleEvent(it.item_id, rule, "moved", f"{it.section.value} -> {target.value}"))
                it.section = target
                if target == Section.REPURPOSING:
                    it.human_precedent = ""
                status = "needs_reverification"
                res.problem(it.item_id, f"Moved to {target.value}: {why} Rewrite the statement for this tier using only the supported claims.")

            if it.section == Section.EXISTING_TARGETED:
                strong = any(c.kind in ("regulatory_status", "treatment_effect_human") and v is not None and v.verdict == "supported"
                             and _cites_source(c, ledger, GUIDELINE_SOURCES) for c, v in ok)
                if not strong:
                    demote(Section.OFF_LABEL if human_precedent() else Section.REPURPOSING, "targeted_without_verified_label_or_guideline",
                           "no verified label/guideline support for this indication.")
            if it.section == Section.OFF_LABEL and not human_precedent():
                demote(Section.REPURPOSING, "offlabel_without_verified_human_precedent",
                       "no verified human treatment precedent in the same gene/disease/variant context.")
            if it.section == Section.REPURPOSING and human_precedent():
                res.problem(it.item_id, "Verified human treatment precedent in this context: consider moving to Off-label (Tier 2).")
        res.statement_status[it.item_id] = status
        res.reindexed += [v.model_copy(update={"claim_index": j}) for j, v in enumerate(kept_v) if v is not None]
        if sv is not None:
            res.reindexed.append(sv)
        res.items.append(it)
    return res


def _norm_title(t: str) -> set[str]:
    stop = {"the", "a", "an", "of", "for", "and", "or", "with", "in", "to", "therapy", "treatment", "aso", "based"}
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in stop}


def find_cross_section_duplicates(items: list[ActionItem]) -> list[tuple[str, str]]:
    out = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if a.section == b.section:
                continue
            ta, tb = _norm_title(a.title), _norm_title(b.title)
            if ta and tb and len(ta & tb) / len(ta | tb) >= 0.75:
                out.append((a.item_id, b.item_id))
    return out
