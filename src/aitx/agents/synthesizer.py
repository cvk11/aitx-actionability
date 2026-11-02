"""Agent F — prioritisation, summary, missing information, outcome measures;
plus the bounded item-revision step and the development judge."""

from __future__ import annotations

import json
import re
from typing import Any

from .. import prompts
from ..ledger import EvidenceLedger, find_bracket_tokens
from ..schemas import ActionItem, JudgeReport, RevisionPlan, Section, Synthesis
from .context import CaseContext, items_json


def _strip_unknown(text: str, ledger: EvidenceLedger) -> str:
    for tok in set(find_bracket_tokens(text)):
        if tok not in ledger:
            text = re.sub(r"\s*\[" + re.escape(tok) + r"\]", "", text)
    return text


def synthesize(ctx: CaseContext, llm: Any, items: list[ActionItem], ledger: EvidenceLedger) -> Synthesis:
    system = prompts.render("synthesis", CONTEXT=ctx.context_block(), ITEMS=items_json(items),
                            EVIDENCE=ledger.digest([k for it in items for c in it.claims for k in c.citations], max_chars_per_record=400, max_total=30000))
    s = llm.structured(stage="F_synthesize", system=system, user="Produce the synthesis JSON.", output_model=Synthesis)
    ids = {i.item_id: i for i in items}
    tier3 = {i.item_id for i in items if i.section in (Section.BESPOKE, Section.REPURPOSING)
             and not (i.section == Section.BESPOKE and i.bespoke_verdict == "NOT AMENABLE")}
    for line in s.summary_lines:
        line.text = _strip_unknown(line.text, ledger)
        line.item_ids = [x for x in line.item_ids if x in ids]
    # exactly one line per tier (the first one wins); a missing tier gets a deterministic fallback
    seen, lines = set(), []
    for line in s.summary_lines:
        if line.tier not in seen:
            seen.add(line.tier)
            lines.append(line)
    for t in (1, 2, 3):
        if t not in seen:
            from ..schemas import SummaryLine

            lines.append(SummaryLine(tier=t, text="", item_ids=[]))
    s.summary_lines = sorted(lines, key=lambda l: l.tier)
    s.missing_information = [_strip_unknown(m, ledger) for m in s.missing_information]
    s.outcome_measures = [om for om in s.outcome_measures if om.item_id in tier3]
    for k, om in enumerate(sorted(s.outcome_measures, key=lambda o: o.rank), start=1):
        om.rank = k
        om.citations = [c for c in om.citations if c in ledger]
        for f in ("rank_rationale", "safety_metrics", "efficacy_metrics", "preclinical_testing"):
            setattr(om, f, _strip_unknown(getattr(om, f), ledger))
    s.duplicate_item_ids_to_drop = [x for x in s.duplicate_item_ids_to_drop if x in ids]
    return s


def revise(llm: Any, items: list[ActionItem], problems: dict[str, list[str]], ledger: EvidenceLedger, round_i: int) -> RevisionPlan:
    targets = [i for i in items if i.item_id in problems]
    if not targets:
        return RevisionPlan(revisions=[])
    keys = [k for it in targets for c in it.claims for k in c.citations]
    system = prompts.render("revise_items", PROBLEMS=json.dumps(problems, indent=1), ITEMS=items_json(targets),
                            EVIDENCE=ledger.digest(keys, max_chars_per_record=1200, max_total=40000))
    return llm.structured(stage=f"F_revise:r{round_i}", system=system, user="Return the revision plan JSON.", output_model=RevisionPlan)


def apply_revisions(items: list[ActionItem], plan: RevisionPlan, ledger: EvidenceLedger) -> tuple[list[ActionItem], list[dict[str, Any]]]:
    by_id = {r.item_id: r for r in plan.revisions}
    out, log = [], []
    for it in items:
        r = by_id.get(it.item_id)
        if r is None or r.action == "keep":
            out.append(it)
            continue
        log.append(r.model_dump(mode="json"))
        if r.action == "drop":
            continue
        upd: dict[str, Any] = {}
        if r.revised_statement.strip():
            upd["statement"] = _strip_unknown(r.revised_statement, ledger)
        if r.revised_title.strip():
            upd["title"] = r.revised_title.strip()
        if r.revised_claims:
            upd["claims"] = r.revised_claims
        if r.action == "move" and r.new_section:
            upd["section"] = Section(r.new_section)
            if upd["section"] == Section.REPURPOSING:
                upd["human_precedent"] = ""
        out.append(it.model_copy(update=upd))
    return out, log


def judge(ctx: CaseContext, llm: Any, markdown: str, ledger: EvidenceLedger) -> JudgeReport:
    system = prompts.render("judge", PATIENT=ctx.patient_json(), REPORT=markdown, EVIDENCE=ledger.digest(max_chars_per_record=300, max_total=30000))
    return llm.structured(stage="Z_judge", system=system, user="Score the report.", output_model=JudgeReport)


# ---------------------------------------------------------------- B2: verify the synthesis
TIER_OF = {Section.EXISTING_TARGETED: 1, Section.SUPPORTIVE: 1, Section.CONTRAINDICATIONS: 1, Section.TRIALS: 2, Section.OFF_LABEL: 2,
           Section.BESPOKE: 3, Section.REPURPOSING: 3}


def fallback_summary(tier: int, items: list[ActionItem]) -> str:
    """Deterministic summary line that adds no new clinical assertion: points to the top verified item of the tier."""
    from ..template import SECTION_HEADING

    its = sorted([i for i in items if TIER_OF[i.section] == tier], key=lambda i: (i.priority, i.item_id))
    if not its:
        return "No qualifying action was identified at this tier in this run."
    top = its[0]
    return f"{top.title.strip().rstrip(':')} — see {SECTION_HEADING[top.section]} below."


def _mentions_item(text: str, it: ActionItem) -> bool:
    """The summary text must name the referenced candidate (its intervention), so a synthesis sentence cannot
    swap the intervention while keeping the original citation."""
    from ..rules import distinctive_terms

    toks = distinctive_terms(it.title)
    low = text.lower()

    def hit(t: str) -> bool:
        if len(t) >= 5:
            return t[:5] in low  # tolerate inflection (seizures / seizure)
        return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low) is not None

    return bool(toks) and sum(1 for t in toks if hit(t)) >= max(1, (len(toks) + 1) // 2)


def verify_synthesis(s: Synthesis, items: list[ActionItem], ledger: EvidenceLedger, verifier_llm: Any, effort: str, vctx: Any = None):
    """Every model-written synthesis text is checked deterministically and then by the verifier.
    Failed or undecided texts are replaced by deterministic fallbacks (summary) or removed (missing information,
    outcome measures). Returns (synthesis, coverage, events)."""
    from ..rules import item_citations
    from .verifier import verify_payload

    by_id = {i.item_id: i for i in items}
    events: list[dict[str, Any]] = []
    payload: list[dict[str, Any]] = []
    lines = {}
    for line in s.summary_lines:
        refs = [by_id[x] for x in line.item_ids if x in by_id and TIER_OF[by_id[x].section] == line.tier]
        allowed = {k for r in refs for k in item_citations(r)}
        cited = set(re.findall(r"\[([A-Z][A-Za-z]+:[^\]\s]+|NCT\d{8})\]", line.text))
        tier_items = [i for i in items if TIER_OF[i.section] == line.tier]
        if not refs or not cited <= allowed or not any(_mentions_item(line.text, r) for r in refs):
            reason = ("tier has verified items but the line references none" if tier_items and not refs else
                      "summary does not name the referenced candidate (intervention identity)" if refs and cited <= allowed else
                      "not linked to verified items of this tier / foreign citation")
            events.append({"target": f"summary-{line.tier}", "action": "fallback", "reason": reason})
            line.text, line.item_ids = fallback_summary(line.tier, items), []
            continue
        lines[f"SYN-summary-{line.tier}"] = line
        payload.append({"item_id": f"SYN-summary-{line.tier}", "claim_index": 0, "section": "summary", "item_title": "summary",
                        "claim": line.text, "kind": "summary_of_verified_items", "citations": sorted(allowed)})
    all_keys = sorted({k for i in items for k in item_citations(i)})
    for n, m in enumerate(s.missing_information):
        payload.append({"item_id": f"SYN-missing-{n}", "claim_index": 0, "section": "summary", "item_title": "missing information",
                        "claim": m, "kind": "conditional_action", "citations": all_keys[:20]})
    for om in s.outcome_measures:
        it = by_id.get(om.item_id)
        if it is not None and om.action_name.strip() != it.title.strip():
            events.append({"target": f"SYN-om-{om.item_id}", "action": "renamed", "reason": "outcome-measure name replaced by the candidate's exact title"})
            om.action_name = it.title.strip()
        cites = sorted(set(om.citations) | (set(item_citations(it)) if it else set()))
        payload.append({"item_id": f"SYN-om-{om.item_id}", "claim_index": 0, "section": "outcome_measures", "item_title": om.action_name,
                        "claim": f"Ranking rationale: {om.rank_rationale} Safety metrics: {om.safety_metrics} Efficacy metrics: {om.efficacy_metrics} "
                                 f"Preclinical testing: {om.preclinical_testing}", "kind": "outcome_measure", "citations": cites})
    vr = verify_payload(payload, ledger, verifier_llm, effort, "D_verify_synth", vctx) if payload else None
    # summary lines must be supported; conditional actions and outcome measures are reasoning (not_checkable allowed)
    ok_kinds = {"supported", "not_checkable"}
    ok = {v.item_id for v in (vr.verdicts if vr else []) if v.verdict in ok_kinds and not (v.item_id.startswith("SYN-summary") and v.verdict != "supported")}
    for sid, line in lines.items():
        if sid not in ok:
            events.append({"target": sid, "action": "fallback", "reason": "summary line not verified"})
            line.text, line.item_ids = fallback_summary(line.tier, items), []
    kept_missing = []
    for n, m in enumerate(s.missing_information):
        if f"SYN-missing-{n}" in ok:
            kept_missing.append(m)
        else:
            events.append({"target": f"SYN-missing-{n}", "action": "removed", "reason": "conditional action not verified"})
    s.missing_information = kept_missing
    kept_om = []
    for om in s.outcome_measures:
        if f"SYN-om-{om.item_id}" in ok:
            kept_om.append(om)
        else:
            events.append({"target": f"SYN-om-{om.item_id}", "action": "removed", "reason": "outcome measure not verified"})
    for k, om in enumerate(sorted(kept_om, key=lambda o: o.rank), start=1):
        om.rank = k
    s.outcome_measures = kept_om
    if any(TIER_OF[i.section] == 3 for i in items) and not kept_om:
        events.append({"target": "outcome_measures", "action": "none_verified", "reason": "Tier-3 candidates exist but no outcome measure was verified"})
    return s, (vr.coverage if vr else {"expected": 0, "missing_after_reverification": []}), events
