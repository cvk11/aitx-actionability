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
        if r.action == "move" and r.new_section:
            upd["section"] = Section(r.new_section)
            if upd["section"] == Section.REPURPOSING:
                upd["human_precedent"] = ""
        out.append(it.model_copy(update=upd))
    return out, log


def judge(ctx: CaseContext, llm: Any, markdown: str, ledger: EvidenceLedger) -> JudgeReport:
    system = prompts.render("judge", PATIENT=ctx.patient_json(), REPORT=markdown, EVIDENCE=ledger.digest(max_chars_per_record=300, max_total=30000))
    return llm.structured(stage="Z_judge", system=system, user="Score the report.", output_model=JudgeReport)
