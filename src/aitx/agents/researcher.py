"""Agents B+C — section-group evidence researchers (agentic retrieval loops).

Merged by design: retrieval and therapeutic-strategy reasoning for a section
happen in the same loop, so the model can re-query when evidence is thin.
Three researchers (Tier 1 / Tier 2 / Tier 3) run in parallel with
section-specific instructions, matching the organizers' advice that each
section asks different questions of different sources.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import prompts
from ..retrieval.registry import TOOL_NAMES, ToolBox
from ..schemas import ActionItem, Section, SectionResearchOutput
from .context import CaseContext, items_json

log = logging.getLogger(__name__)

RESEARCHERS: dict[str, tuple[str, tuple[Section, ...]]] = {
    "B_established": ("research_established", (Section.EXISTING_TARGETED, Section.SUPPORTIVE, Section.CONTRAINDICATIONS)),
    "B_emerging": ("research_emerging", (Section.TRIALS, Section.OFF_LABEL)),
    "C_hypothesized": ("research_hypothesized", (Section.BESPOKE, Section.REPURPOSING)),
}


def run_researchers(ctx: CaseContext, llm: Any, toolbox: ToolBox, max_rounds: int, parallel: bool = True) -> dict[str, SectionResearchOutput]:
    ctx_block = ctx.context_block()

    def one(stage: str) -> tuple[str, SectionResearchOutput]:
        prompt_name, sections = RESEARCHERS[stage]
        system = prompts.render(prompt_name, CONTEXT=ctx_block)
        user = (f"Research the sections {[s.value for s in sections]} for this patient now. "
                "Use the tools iteratively; return the final JSON when the evidence is sufficient or the tool budget is reached.")
        out = llm.agent(stage=stage, system=system, user=user, output_model=SectionResearchOutput, toolbox=toolbox,
                        tool_names=TOOL_NAMES, max_rounds=max_rounds)
        return stage, out

    stages = list(RESEARCHERS)
    if parallel:
        with ThreadPoolExecutor(max_workers=len(stages)) as ex:
            results = dict(ex.map(one, stages))
    else:
        results = dict(one(s) for s in stages)
    return results


def unique_id(base: str, seen: set[str]) -> str:
    """Deterministic globally unique item id (base, base-2, base-3, ...)."""
    base = (base or "item").strip() or "item"
    cand, n = base, 2
    while cand in seen:
        cand, n = f"{base}-{n}", n + 1
    seen.add(cand)
    return cand


def collect_handoffs(outputs: dict[str, SectionResearchOutput]) -> list[dict[str, Any]]:
    out = []
    for stage, o in outputs.items():
        for n, h in enumerate(getattr(o, "candidate_handoffs", []) or [], start=1):
            from ..schemas import CandidateHandoff

            h = h if isinstance(h, CandidateHandoff) else CandidateHandoff.model_validate(h)
            out.append({"handoff_id": f"{stage}-h{n}", "from_stage": stage, "intervention": h.intervention,
                        "proposed_section": h.proposed_section.value, "reason": h.reason, "citations": h.citations})
    return out


def coverage_gaps(dossier: dict[str, Any], items: list[ActionItem], per_family: int = 8) -> tuple[list[str], list[dict[str, Any]]]:
    """Mandatory-coverage check: articles returned by the therapeutic-coverage queries that no candidate cites.
    Returns (follow-up requests, audit rows). Failed families are reported as gaps, never as absence."""
    from ..rules import item_citations

    cited = {k for it in items for k in item_citations(it)}
    requests, rows = [], []
    for gene, fams in (dossier.get("therapeutic_coverage") or {}).items():
        for fam, f in fams.items():
            unused = [k for k in f.get("keys", []) if k not in cited]
            rows.append({"gene": gene, "family": fam, "total": f.get("total"), "reviewed": len(f.get("keys", [])),
                         "cited": len(f.get("keys", [])) - len(unused), "unused": unused, "truncated": f.get("truncated"), "error": f.get("error", "")})
            if unused:
                lst = "; ".join(f"{k} ({(f.get('titles') or {}).get(k, '')[:90]})" for k in unused[:per_family])
                requests.append(f"[coverage:{gene}:{fam}] Articles from the mandatory query '{f.get('query')}' that no candidate used: {lst}. "
                                "Assess each for this patient (intervention, direction of effect vs the variant mechanism, species, variant); "
                                "propose a candidate only if verified evidence supports it, otherwise list it in searched_but_not_found with the reason.")
    return requests, rows


def merge_items(outputs: dict[str, SectionResearchOutput]) -> tuple[list[ActionItem], dict[Section, list[str]], list[str]]:
    items: list[ActionItem] = []
    seen: set[str] = set()
    negatives: dict[Section, list[str]] = {}
    open_q: list[str] = []
    for stage, out in outputs.items():
        _, owned = RESEARCHERS.get(stage, ("", tuple(Section)))
        for it in out.items:
            iid = unique_id(it.item_id.strip() or f"{stage}-item", seen)
            items.append(it.model_copy(update={"item_id": iid}))
        # negative findings are attached to the first owned section they mention, else to all owned sections' notes
        for neg in out.searched_but_not_found:
            target = next((s for s in owned if s.value.split("_")[0] in neg.lower()), owned[0] if owned else None)
            if target is not None:
                negatives.setdefault(target, []).append(neg)
        open_q += out.open_questions
    return items, negatives, open_q


def followup(ctx: CaseContext, llm: Any, toolbox: ToolBox, requests: list[str], items: list[ActionItem], max_rounds: int) -> SectionResearchOutput:
    system = prompts.render("research_followup", REQUESTS="\n".join(f"- {r}" for r in requests), CONTEXT=ctx.context_block(), ITEMS=items_json(items, 20000))
    return llm.agent(stage="E_followup", system=system, user="Investigate the requests now and return the final JSON.",
                     output_model=SectionResearchOutput, toolbox=toolbox, tool_names=TOOL_NAMES, max_rounds=max_rounds)
