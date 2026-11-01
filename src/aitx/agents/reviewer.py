"""Agent E — optional independent review (Gemini, or a second Claude call) and
evidence-grounded adjudication by Claude. The reviewer never edits the report:
accepted issues become revision problems; `needs_research` issues trigger one
bounded follow-up research loop whose output is verified like everything else."""

from __future__ import annotations

import json
from typing import Any

from .. import prompts
from ..ledger import EvidenceLedger
from ..schemas import ActionItem, Adjudication, ReviewReport
from .context import CaseContext, items_json


def review(ctx: CaseContext, reviewer: Any, items: list[ActionItem], ledger: EvidenceLedger) -> ReviewReport:
    system = prompts.render("reviewer", PATIENT=ctx.patient_json(),
                            INTERPRETATION=json.dumps(ctx.interpretation.model_dump() if ctx.interpretation else {}, indent=1)[:12000],
                            ITEMS=items_json(items), EVIDENCE=ledger.digest(max_chars_per_record=500, max_total=50000))
    return reviewer.structured(stage=f"E_review_{getattr(reviewer, 'name', 'llm')}", system=system,
                               user="Return the review issues as JSON.", output_model=ReviewReport)


def adjudicate(report: ReviewReport, llm: Any, items: list[ActionItem], ledger: EvidenceLedger) -> Adjudication:
    if not report.issues:
        return Adjudication(decisions=[])
    issues = [{"issue_index": i, **iss.model_dump()} for i, iss in enumerate(report.issues)]
    system = prompts.render("adjudicator", ISSUES=json.dumps(issues, indent=1), ITEMS=items_json(items),
                            EVIDENCE=ledger.digest(max_chars_per_record=600, max_total=50000))
    return llm.structured(stage="E_adjudicate", system=system, user="Adjudicate every issue.", output_model=Adjudication)
