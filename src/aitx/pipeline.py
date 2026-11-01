"""End-to-end pipeline: one generalizable path for every patient.

input validation -> deterministic bootstrap (variant model, ClinVar, ClinGen,
gnomAD, UniProt, GeneReviews, trials) -> A interpret -> B/C research (3 parallel
agentic loops) -> deterministic rules -> D verify (deterministic + LLM) ->
[E independent review + adjudication + follow-up research] -> bounded revision
loop -> F synthesis -> deterministic render -> structural QC.
"""

from __future__ import annotations

import json
import logging
import platform
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from . import prompts
from .agents import interpreter, researcher, reviewer as rev, synthesizer, verifier
from .agents.context import CaseContext
from .clinical import parse_clinical_context
from .config import Settings, get_settings
from .http import HttpClient
from .ledger import EvidenceLedger
from .llm.base import BudgetExceeded, CostTracker, ModelRefusal
from .qc import check_links_against_ledger, check_report_structure, heuristic_scorecard
from .render import render_report
from .retrieval.registry import ToolBox
from .rules import apply_citation_rules, apply_section_rules, apply_verdicts, find_cross_section_duplicates
from .schemas import ActionItem, ClaimVerdict, PatientCase, ReportOutput, Section, Synthesis

log = logging.getLogger(__name__)
MOCK_BANNER = "> **MOCK OUTPUT — generated with the offline mock LLM; not genuine research and never to be submitted.**"


@dataclass
class CaseResult:
    output: ReportOutput
    audit: dict[str, Any]
    is_mock: bool = False
    unresolved: list[str] = field(default_factory=list)


def software_versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in ("anthropic", "httpx", "pydantic", "tenacity", "google-genai", "gradio"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            pass
    return out


class Pipeline:
    def __init__(self, llm: Any, settings: Settings | None = None, *, verifier_llm: Any | None = None, reviewer_llm: Any | None = None,
                 http: HttpClient | None = None, cost: CostTracker | None = None, run_judge: bool = False, parallel: bool = True, verify_claims: bool = True, mode: str = "full") -> None:
        self.settings = settings or get_settings()
        self.llm = llm
        self.verifier_llm = verifier_llm or llm
        self.reviewer_llm = reviewer_llm
        self.http = http or HttpClient(self.settings)
        self.cost = cost or getattr(llm, "cost", None) or CostTracker(self.settings.max_cost_usd_per_case)
        self.run_judge = run_judge
        self.parallel = parallel
        self.verify_claims = verify_claims
        if mode not in ("full", "baseline"):
            raise ValueError(mode)
        self.mode = mode
        self.is_mock = bool(getattr(llm, "is_mock", False))

    # ---------------------------------------------------------------- run
    def run(self, payload: dict[str, Any] | PatientCase) -> CaseResult:
        t0 = time.time()
        case = payload if isinstance(payload, PatientCase) else PatientCase.model_validate(payload)
        ledger = EvidenceLedger()
        toolbox = ToolBox(self.http, ledger, self.settings)
        ctx = CaseContext(case=case, clinical=parse_clinical_context(case.patient.clinical_context))
        audit: dict[str, Any] = {"case_id": case.id, "is_mock": self.is_mock, "mode": self.mode, "verify_claims": self.verify_claims, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "settings": self.settings.public_dict(), "prompts_sha256": prompts.manifest(), "software": software_versions(),
                                 "genereviews_snapshot": toolbox.gr.manifest, "stages": {}, "rule_events": [], "unresolved": []}
        unresolved: list[str] = audit["unresolved"]
        S = self.settings

        if self.mode == "baseline":
            return self._run_baseline(case, ctx, ledger, toolbox, audit, t0)

        # ---- deterministic bootstrap
        ctx.dossier = interpreter.bootstrap(ctx, self.http, ledger, toolbox)
        audit["stages"]["bootstrap"] = {"ledger_records": len(ledger), "errors": ctx.dossier.get("errors", [])}

        synthesis = Synthesis(summary_lines=[], missing_information=[], duplicate_item_ids_to_drop=[], outcome_measures=[], calibration_notes=[])
        items: list[ActionItem] = []
        negatives: dict[Section, list[str]] = {}
        verdicts: list[ClaimVerdict] = []
        try:
            # ---- A
            ctx.interpretation = interpreter.interpret(ctx, self.llm, toolbox, max_rounds=min(8, S.max_tool_rounds))
            audit["stages"]["A_interpretation"] = ctx.interpretation.model_dump(mode="json")
            age = ctx.interpretation.patient_age_years if ctx.interpretation.patient_age_years >= 0 else ctx.clinical.age_years
            sex = ctx.interpretation.patient_sex

            # ---- B/C
            outs = researcher.run_researchers(ctx, self.llm, toolbox, S.max_tool_rounds, parallel=self.parallel)
            audit["stages"]["BC_research_raw"] = {k: v.model_dump(mode="json") for k, v in outs.items()}
            items, negatives, open_q = researcher.merge_items(outs)
            audit["stages"]["open_questions"] = open_q

            # ---- deterministic rules (pass 1)
            items, problems = self._rules(items, ledger, age, sex, audit)

            # ---- D verification
            det = verifier.deterministic_checks(items, ledger, self.http)
            items, problems2 = self._rules(items, ledger, age, sex, audit)  # live trial status may have changed
            _merge(problems, problems2)
            verdicts = verifier.llm_verify(items, ledger, self.verifier_llm, S.verifier_effort) if self.verify_claims else []
            vres = apply_verdicts(items, verdicts)
            items = vres.items
            audit["rule_events"] += [e.__dict__ for e in vres.events]
            _merge(problems, vres.problems)
            audit["stages"]["D_verification"] = {"deterministic": det, "verdicts": [v.model_dump(mode="json") for v in verdicts]}

            # ---- E optional independent review
            if self.reviewer_llm is not None:
                items, problems = self._review(ctx, items, ledger, toolbox, age, sex, problems, audit)

            # ---- bounded revision loop
            for r in range(S.max_revisions):
                dups = find_cross_section_duplicates(items)
                for a, b in dups:
                    problems.setdefault(b, []).append(f"Possible duplicate of item {a} in another section; keep the action only in the best-fitting section.")
                problems = {k: v for k, v in problems.items() if any(i.item_id == k for i in items)}
                if not problems:
                    break
                plan = synthesizer.revise(self.llm, items, problems, ledger, r)
                items, rlog = synthesizer.apply_revisions(items, plan, ledger)
                audit["stages"].setdefault("revisions", []).append({"round": r, "problems": problems, "applied": rlog})
                changed = {x["item_id"] for x in rlog if x["action"] in ("revise", "move")}
                items, problems = self._rules(items, ledger, age, sex, audit)
                if changed and self.verify_claims:
                    sv = verifier.llm_verify([i for i in items if i.item_id in changed], ledger, self.verifier_llm, S.verifier_effort, statements_only=True)
                    verdicts += sv
                    vr = apply_verdicts([i for i in items if i.item_id in changed], sv)
                    keep = {i.item_id: i for i in vr.items}
                    items = [keep.get(i.item_id, i) if i.item_id in changed else i for i in items if i.item_id not in changed or i.item_id in keep]
                    _merge(problems, vr.problems)
            # unresolved statement-level failures after the last round -> drop (conservative)
            final_bad = {k for k, v in problems.items() if any(p.startswith("Full statement") for p in v)}
            if final_bad:
                unresolved += [f"dropped {k}: statement remained unsupported after {S.max_revisions} revision rounds" for k in final_bad]
                items = [i for i in items if i.item_id not in final_bad]
            unresolved += [f"{k}: {p}" for k, v in problems.items() if k not in final_bad for p in v]

            # ---- F synthesis
            synthesis = synthesizer.synthesize(ctx, self.llm, items, ledger)
            if synthesis.duplicate_item_ids_to_drop:
                items = [i for i in items if i.item_id not in set(synthesis.duplicate_item_ids_to_drop)]
            audit["stages"]["F_synthesis"] = synthesis.model_dump(mode="json")
        except BudgetExceeded as e:
            unresolved.append(f"budget exceeded: {e}; report rendered from items verified so far")
            log.error("%s", e)
        except ModelRefusal as e:
            unresolved.append(f"model refusal: {e}")
            log.error("%s", e)

        return self._finish(case, ctx, ledger, toolbox, audit, t0, items, synthesis, negatives, verdicts)

    # ------------------------------------------------- comparison arm A
    def _run_baseline(self, case, ctx, ledger, toolbox, audit, t0) -> CaseResult:
        """Arm A: one Claude agent with the same retrieval tools; no deterministic
        molecular bootstrap, no section rules, no claim verification, no revision.
        Only unresolvable citation keys are stripped (required to render links)."""
        from .agents.researcher import merge_items
        from .retrieval.registry import TOOL_NAMES
        from .schemas import SectionResearchOutput

        synthesis = Synthesis(summary_lines=[], missing_information=[], duplicate_item_ids_to_drop=[], outcome_measures=[], calibration_notes=[])
        items, negatives = [], {}
        try:
            out = self.llm.agent(stage="A_baseline", system=prompts.render("research_baseline", PATIENT=ctx.patient_json()),
                                 user="Research all sections now and return the final JSON.", output_model=SectionResearchOutput,
                                 toolbox=toolbox, tool_names=TOOL_NAMES, max_rounds=self.settings.max_tool_rounds)
            items, negatives, _ = merge_items({"A_baseline": out})
            c = apply_citation_rules(items, ledger)
            items = c.items
            audit["rule_events"] += [e.__dict__ for e in c.events]
            synthesis = synthesizer.synthesize(ctx, self.llm, items, ledger)
        except (BudgetExceeded, ModelRefusal) as e:
            audit["unresolved"].append(str(e))
        return self._finish(case, ctx, ledger, toolbox, audit, t0, items, synthesis, negatives, [])

    def _finish(self, case, ctx, ledger, toolbox, audit, t0, items, synthesis, negatives, verdicts) -> CaseResult:
        unresolved = audit["unresolved"]
        # ---- render + QC (deterministic)
        evidence_date = time.strftime("%Y-%m-%d", time.gmtime())
        md = render_report(case.id, items, synthesis, ledger, negatives, evidence_date)
        if self.is_mock:
            lines = md.split("\n", 1)
            md = lines[0] + "\n\n" + MOCK_BANNER + "\n" + (lines[1] if len(lines) > 1 else "")
        qc = check_report_structure(md, case.id)
        links = check_links_against_ledger(md, ledger)
        audit["qc"] = {"structure": qc.__dict__, "links": links.__dict__}
        if not qc.ok:
            raise RuntimeError(f"Structural QC failed for {case.id}: {qc.errors}")
        if self.run_judge and not self.is_mock:
            try:
                audit["judge"] = synthesizer.judge(ctx, self.llm, md, ledger).model_dump(mode="json")
            except Exception as e:  # pragma: no cover
                audit["judge_error"] = str(e)
        audit["final_items"] = [i.model_dump(mode="json") for i in items]
        audit["heuristic_scorecard"] = heuristic_scorecard(items, verdicts, ledger, md)
        audit["cost"] = self.cost.summary()
        audit["tool_calls"] = toolbox.calls
        audit["http_requests"] = self.http.request_count
        audit["retrieval_status"] = ledger.retrieval_status()
        audit["ledger"] = ledger.to_dict()
        audit["seconds"] = round(time.time() - t0, 1)
        return CaseResult(ReportOutput(id=case.id, report_markdown=md), audit, self.is_mock, unresolved)

    # ------------------------------------------------------------ helpers
    def _rules(self, items, ledger, age, sex, audit):
        c = apply_citation_rules(items, ledger)
        s = apply_section_rules(c.items, ledger, age, sex)
        audit["rule_events"] += [e.__dict__ for e in c.events + s.events]
        problems: dict[str, list[str]] = {}
        _merge(problems, c.problems)
        _merge(problems, s.problems)
        return s.items, problems

    def _review(self, ctx, items, ledger, toolbox, age, sex, problems, audit):
        rep = rev.review(ctx, self.reviewer_llm, items, ledger)
        adj = rev.adjudicate(rep, self.llm, items, ledger)
        decisions = {d.issue_index: d for d in adj.decisions}
        requests = []
        for i, iss in enumerate(rep.issues):
            d = decisions.get(i)
            if d is None or d.decision == "reject":
                continue
            if d.decision == "accept" and iss.target_item_id:
                problems.setdefault(iss.target_item_id, []).append(f"Reviewer issue accepted ({iss.issue_type}): {iss.description} Fix: {iss.suggested_fix}")
            elif d.decision == "needs_research" or (d.decision == "accept" and not iss.target_item_id):
                requests.append(d.research_query or f"{iss.issue_type}: {iss.description} (reviewer cites: {', '.join(iss.cited_keys)})")
        audit["stages"]["E_review"] = {"reviewer": getattr(self.reviewer_llm, "name", "?"), "issues": rep.model_dump(mode="json"),
                                       "adjudication": adj.model_dump(mode="json"), "research_requests": requests}
        if requests:
            fu = researcher.followup(ctx, self.llm, toolbox, requests[:6], items, max_rounds=min(10, self.settings.max_tool_rounds))
            new_items = [it.model_copy(update={"item_id": f"fu-{it.item_id}"}) for it in fu.items]
            new_items, p = self._rules(new_items, ledger, age, sex, audit)
            verifier.deterministic_checks(new_items, ledger, self.http)
            v = verifier.llm_verify(new_items, ledger, self.verifier_llm, self.settings.verifier_effort)
            vr = apply_verdicts(new_items, v)
            audit["stages"]["E_followup"] = {"raw": fu.model_dump(mode="json"), "verdicts": [x.model_dump(mode="json") for x in v],
                                             "kept": [i.item_id for i in vr.items]}
            items = items + vr.items
            _merge(problems, p)
            _merge(problems, vr.problems)
        return items, problems


def _merge(a: dict[str, list[str]], b: dict[str, list[str]]) -> None:
    for k, v in b.items():
        a.setdefault(k, []).extend(v)


def save_audit(result: CaseResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{result.output.id}{'.MOCK' if result.is_mock else ''}.audit.json"
    p.write_text(json.dumps(result.audit | {"unresolved": result.unresolved}, indent=1, default=str), encoding="utf-8")
    (out_dir / f"{result.output.id}{'.MOCK' if result.is_mock else ''}.md").write_text(result.output.report_markdown, encoding="utf-8")
    return p
