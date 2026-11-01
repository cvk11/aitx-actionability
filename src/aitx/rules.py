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
HUMAN_WORDS = re.compile(r"\b(patient|patients|proband|child|children|boy|girl|infant|individual|individuals|subject|subjects|case|cases|participants|adult|adults)\b", re.I)
GUIDELINE_SOURCES = {"fda_label", "genereviews"}
# claim kinds that carry an item's core assertion, per section; if all such claims fail, the item is dropped
REQUIRED_KINDS = {
    Section.TRIALS: {"trial_status_or_eligibility"},
    Section.OFF_LABEL: {"treatment_effect_human"},
    Section.EXISTING_TARGETED: {"regulatory_status", "treatment_effect_human"},
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
    r = ledger.get(key)
    if r is None or r.source_type not in ("pubmed", "europepmc"):
        return False
    pts = set(r.metadata.get("pub_types") or [])
    if pts & HUMAN_PUBTYPES:
        return True
    return bool(HUMAN_WORDS.search((r.title or "") + " " + (r.content or "")[:3000]))


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
                res.events.append(RuleEvent(it.item_id, "offlabel_without_human_precedent", "moved", "-> drug_repurposing"))
                res.problem(it.item_id, "Moved to Drug Repurposing: no cited human treatment precedent; state that no human precedent exists in this context.")
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


def apply_verdicts(items: list[ActionItem], verdicts: list[ClaimVerdict]) -> RuleResult:
    """Apply claim-level verification results.

    * supported / not_checkable claims are kept
    * partially_supported claims are replaced by the verifier's revision (if given)
    * unsupported / contradicted claims are removed, and the item is flagged for
      statement revision; items left with no supported claim are dropped
    * repurposing items with a verified human same-gene treatment claim are
      flagged (may belong in Off-label)
    """
    by_item: dict[str, dict[int, ClaimVerdict]] = {}
    for v in verdicts:
        by_item.setdefault(v.item_id, {})[v.claim_index] = v
    res = RuleResult(items=[])
    for it in items:
        it = it.model_copy(deep=True)
        vs = by_item.get(it.item_id, {})
        kept = []
        req = REQUIRED_KINDS.get(it.section, set())
        had_required = any(c.kind in req for c in it.claims)
        for i, c in enumerate(it.claims):
            v = vs.get(i)
            if v is None or v.verdict in ("supported", "not_checkable"):
                kept.append(c)
                continue
            if v.verdict == "partially_supported":
                if v.suggested_revision.strip():
                    c.text = v.suggested_revision.strip()
                kept.append(c)
                res.problem(it.item_id, f"Claim partially supported: '{c.text[:160]}' — {v.rationale[:200]}")
            else:
                res.events.append(RuleEvent(it.item_id, f"claim_{v.verdict}", "flagged", f"{c.text[:160]} | {v.rationale[:200]}"))
                res.problem(it.item_id, f"Claim {v.verdict}: '{c.text[:160]}' — {v.rationale[:200]}. Remove or correct it in the statement.")
            if v.overgeneralization.strip():
                res.problem(it.item_id, f"Overgeneralisation: {v.overgeneralization[:200]}")
        it.claims = kept
        sv = vs.get(-1)  # full-statement verdict
        if sv is not None and sv.verdict == "partially_supported" and sv.suggested_revision.strip():
            res.events.append(RuleEvent(it.item_id, "statement_partially_supported", "set_field", "statement replaced by verifier revision"))
            it.statement = sv.suggested_revision.strip()
        elif sv is not None and sv.verdict in ("unsupported", "contradicted"):
            res.problem(it.item_id, f"Full statement {sv.verdict}: {sv.rationale[:250]}. Rewrite it to say only what the cited sources support.")
            if sv.overgeneralization.strip():
                res.problem(it.item_id, f"Overgeneralisation in statement: {sv.overgeneralization[:200]}")
        if not kept:
            res.events.append(RuleEvent(it.item_id, "no_supported_claims", "dropped", ""))
            continue
        if had_required and not any(c.kind in req for c in kept):
            res.events.append(RuleEvent(it.item_id, "core_claims_unsupported", "dropped", f"no supported {sorted(req)} claim remains"))
            continue
        if it.section == Section.REPURPOSING:
            for i, c in enumerate(it.claims):
                v = vs.get(i)
                if v and v.verdict == "supported" and c.kind == "treatment_effect_human" and v.evidence_type in ("direct_human_same_variant", "direct_human_same_gene"):
                    res.problem(it.item_id, "Verified human treatment precedent in this gene: consider moving to Off-label (Tier 2).")
                    break
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
