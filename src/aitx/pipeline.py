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
from .llm.base import BudgetExceeded, CostTracker, ModelRefusal, StructuredOutputError
from .qc import check_links_against_ledger, check_report_structure, heuristic_scorecard
from .render import render_report
from .retrieval.registry import ToolBox
from .rules import apply_citation_rules, apply_section_rules, apply_verdicts, compose_from_verified_claims, find_cross_section_duplicates
from .schemas import ActionItem, ClaimVerdict, PatientCase, ReportOutput, Section, Synthesis
from .verification_contract import contract_errors, item_digest, item_text, text_digest

log = logging.getLogger(__name__)
MOCK_BANNER = "> **MOCK OUTPUT — generated with the offline mock LLM; not genuine research and never to be submitted.**"


OK_STATUS = ("supported", "not_checkable", "hypothesis", "composed_from_verified_claims")


@dataclass
class CaseResult:
    output: ReportOutput
    audit: dict[str, Any]
    is_mock: bool = False
    unresolved: list[str] = field(default_factory=list)

    @property
    def issues(self) -> list[dict[str, str]]:
        return self.audit.get("issues", [])

    @property
    def blockers(self) -> list[dict[str, str]]:
        """Critical issues: a report with any blocker must never enter the submission file."""
        return [i for i in self.issues if i["severity"] == "critical"]

    @property
    def submittable(self) -> bool:
        return not self.blockers


def add_issue(audit: dict[str, Any], severity: str, code: str, detail: str) -> None:
    """severity: critical (blocks submission) | warning | info."""
    audit.setdefault("issues", []).append({"severity": severity, "code": code, "detail": detail[:500]})
    if severity != "info":
        audit.setdefault("unresolved", []).append(f"[{severity}:{code}] {detail[:300]}")


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
                                 "genereviews_snapshot": toolbox.gr.manifest, "stages": {}, "rule_events": [], "unresolved": [], "issues": []}
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
        vstate: dict[str, Any] = {"status": {}, "verdicts": {}, "all": verdicts}
        try:
            # ---- A
            ctx.interpretation = interpreter.interpret(ctx, self.llm, toolbox, max_rounds=min(8, S.max_tool_rounds))
            audit["stages"]["A_interpretation"] = ctx.interpretation.model_dump(mode="json")
            age = ctx.interpretation.patient_age_years if ctx.interpretation.patient_age_years >= 0 else ctx.clinical.age_years
            age = None if (age is None or age < 0) else age
            sex = ctx.interpretation.patient_sex

            # ---- B/C
            outs = researcher.run_researchers(ctx, self.llm, toolbox, S.max_tool_rounds, parallel=self.parallel)
            audit["stages"]["BC_research_raw"] = {k: v.model_dump(mode="json") for k, v in outs.items()}
            items, negatives, open_q = researcher.merge_items(outs)
            handoffs = researcher.collect_handoffs(outs)
            audit["stages"]["open_questions"] = open_q
            audit["stages"]["candidate_handoffs"] = handoffs
            vstate["vctx"] = verifier.VerifyContext.from_case(ctx)
            genes = [v.gene for v in case.patient.genotype]

            # ---- deterministic rules (pass 1)
            items, problems = self._rules(items, ledger, age, sex, audit, genes)

            # ---- D verification (deterministic, then claim + statement entailment; fail closed)
            det = verifier.deterministic_checks(items, ledger, self.http)
            items, problems2 = self._rules(items, ledger, age, sex, audit, genes)  # live trial data may have changed
            _merge(problems, problems2)
            audit["stages"]["D_verification"] = {"deterministic": det}
            if self.verify_claims:
                items = self._verify(items, ledger, vstate, problems, audit, "initial")
            else:
                add_issue(audit, "critical", "verification_disabled", "claim verification disabled (ablation arm): report is not submittable")

            # ---- candidate handoffs + open questions are researched, never silently dropped
            cov_requests, cov_rows = researcher.coverage_gaps(ctx.dossier or {}, items)
            audit["stages"]["therapeutic_coverage"] = cov_rows
            for r in cov_rows:
                if r["error"]:
                    add_issue(audit, "warning", "coverage_query_failed", f"{r['gene']} {r['family']}: mandatory coverage query {r.get('status', 'failed')} "
                                                                          f"({r['error'][:160]}); coverage gap, not evidence of absence")
                elif r.get("status") == "truncated":
                    add_issue(audit, "info", "coverage_query_truncated", f"{r['gene']} {r['family']}: {r['reviewed']} of {r['total']} hits reviewed")
            if handoffs or open_q or cov_requests:
                items, problems = self._handoffs(ctx, items, ledger, toolbox, age, sex, genes, problems, audit, vstate, handoffs, open_q,
                                                 cov_requests)

            # ---- E optional independent review
            if self.reviewer_llm is not None:
                items, problems = self._review(ctx, items, ledger, toolbox, age, sex, problems, audit, vstate, genes)

            # ---- bounded revision loop; every changed item is re-verified against its evidence
            for r in range(S.max_revisions):
                for a_id, b_id in find_cross_section_duplicates(items):
                    problems.setdefault(b_id, []).append(f"Possible duplicate of item {a_id} in another section; keep the action only in the best-fitting section.")
                if self.verify_claims:
                    for i in items:
                        st = vstate["status"].get(i.item_id, "unverified")
                        if st not in OK_STATUS and not problems.get(i.item_id):
                            problems[i.item_id] = [f"Statement status is '{st}'; rewrite it to say only what the cited sources support."]
                problems = {k: v for k, v in problems.items() if any(i.item_id == k for i in items)}
                if not problems:
                    break
                plan = synthesizer.revise(self.llm, items, problems, ledger, r)
                items, rlog = synthesizer.apply_revisions(items, plan, ledger)
                audit["stages"].setdefault("revisions", []).append({"round": r, "problems": problems, "applied": rlog})
                changed = {x["item_id"] for x in rlog if x["action"] in ("revise", "move")}
                items, problems = self._rules(items, ledger, age, sex, audit, genes)
                if changed and self.verify_claims:
                    ch = [i for i in items if i.item_id in changed]
                    kept = {i.item_id: i for i in self._verify(ch, ledger, vstate, problems, audit, f"revision{r}")}
                    items = [kept[i.item_id] if i.item_id in changed else i for i in items if i.item_id not in changed or i.item_id in kept]

            # ---- final verification gate: only items whose FULL statement is verified are rendered
            if self.verify_claims:
                salvaged = {}
                for i in items:
                    if vstate["status"].get(i.item_id, "unverified") not in OK_STATUS and i.section != Section.TRIALS:
                        comp = compose_from_verified_claims(i, vstate["verdicts"].get(i.item_id, []))
                        if comp is not None:
                            salvaged[i.item_id] = comp
                            add_issue(audit, "info", "statement_composed_from_verified_claims",
                                      f"{i.item_id}: rebuilt from verified atoms; requires fresh full-item verification")
                items = [salvaged.get(i.item_id, i) for i in items]
                items = self._final_gate(items, ledger, vstate, problems, audit)
            for k, v in problems.items():
                if any(i.item_id == k for i in items):
                    for p in v:
                        add_issue(audit, "warning", "unresolved_item_problem", f"{k}: {p}")

            # ---- F synthesis (then verified; unverifiable texts fall back / are removed)
            synthesis = synthesizer.synthesize(ctx, self.llm, items, ledger)
            if synthesis.duplicate_item_ids_to_drop:
                items = [i for i in items if i.item_id not in set(synthesis.duplicate_item_ids_to_drop)]
            if self.verify_claims:
                synthesis, scov, sev = synthesizer.verify_synthesis(synthesis, items, ledger, self.verifier_llm, S.verifier_effort, vstate.get("vctx"))
                audit["stages"]["F_synthesis_verification"] = {"coverage": scov, "events": sev}
                if scov.get("missing_after_reverification"):
                    add_issue(audit, "critical", "synthesis_verification_incomplete", f"no decision for {scov['missing_after_reverification']}")
            audit["stages"]["F_synthesis"] = synthesis.model_dump(mode="json")
            audit["stages_completed"] = True
        except (BudgetExceeded, ModelRefusal, StructuredOutputError) as e:
            add_issue(audit, "critical", "stage_incomplete", f"{type(e).__name__}: {e}; report rendered from items verified so far")
            log.error("%s", e)
            if self.verify_claims:  # never render items whose statement was not verified
                items = [i for i in items if not contract_errors([i], audit.get("item_contracts", {}))]
                synthesis = Synthesis(summary_lines=[], missing_information=[], duplicate_item_ids_to_drop=[], outcome_measures=[], calibration_notes=[])

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
            audit["stages_completed"] = True
        except (BudgetExceeded, ModelRefusal, StructuredOutputError) as e:
            add_issue(audit, "critical", "stage_incomplete", f"{type(e).__name__}: {e}")
        add_issue(audit, "critical", "comparison_arm_only", "baseline arm A applies no verification: research comparison only, never submittable")
        return self._finish(case, ctx, ledger, toolbox, audit, t0, items, synthesis, negatives, [])

    def _finish(self, case, ctx, ledger, toolbox, audit, t0, items, synthesis, negatives, verdicts) -> CaseResult:
        # ---- render + QC (deterministic)
        contracts = audit.get("item_contracts", {})
        if self.verify_claims and self.mode == "full":
            errors = contract_errors(items, contracts)
            if errors:
                add_issue(audit, "critical", "final_item_verification_failed", "; ".join(errors))
                items = [i for i in items if not contract_errors([i], contracts)]
        evidence_date = time.strftime("%Y-%m-%d", time.gmtime())
        md = render_report(case.id, items, synthesis, ledger, negatives, evidence_date, statement_status=audit.get("statement_status", {}),
                           verification_contracts=contracts if self.verify_claims and self.mode == "full" else None)
        if self.is_mock:
            lines = md.split("\n", 1)
            md = lines[0] + "\n\n" + MOCK_BANNER + "\n" + (lines[1] if len(lines) > 1 else "")
        qc = check_report_structure(md, case.id)
        links = check_links_against_ledger(md, ledger)
        audit["qc"] = {"structure": qc.__dict__, "links": links.__dict__}
        if not qc.ok:
            raise RuntimeError(f"Structural QC failed for {case.id}: {qc.errors}")
        if not links.ok:  # fail closed: the renderer sanitizes model text, so this indicates a defect
            raise RuntimeError(f"Citation link check failed for {case.id}: {links.errors[:5]}")
        if self.is_mock:
            add_issue(audit, "critical", "mock_output", "generated with the mock LLM")
        if not audit.get("stages_completed"):
            if not any(i["code"] == "stage_incomplete" for i in audit.get("issues", [])):
                add_issue(audit, "critical", "stage_incomplete", "pipeline did not complete all mandatory stages")
        for it in items:
            if it.section == Section.TRIALS and (ledger.get(it.nct_id) and ledger.get(it.nct_id).metadata.get("status_verification") != "live_ok"):
                add_issue(audit, "critical", "trial_status_unverified", f"{it.nct_id}: current status not re-verified live in this run")
        if self.run_judge and not self.is_mock:
            try:
                audit["judge"] = synthesizer.judge(ctx, self.llm, md, ledger).model_dump(mode="json")
            except Exception as e:  # pragma: no cover
                audit["judge_error"] = str(e)
        audit["final_items"] = [i.model_dump(mode="json") for i in items]
        audit["rendered_sha256"] = text_digest(md)
        audit["heuristic_scorecard"] = heuristic_scorecard(items, verdicts, ledger, md)
        audit["cost"] = self.cost.summary()
        audit["tool_calls"] = toolbox.calls
        audit["http_requests"] = self.http.request_count
        audit["retrieval_status"] = ledger.retrieval_status()
        audit["ledger"] = ledger.to_dict()
        audit["seconds"] = round(time.time() - t0, 1)
        res = CaseResult(ReportOutput(id=case.id, report_markdown=md), audit, self.is_mock, audit["unresolved"])
        audit["submittable"] = res.submittable
        audit["blockers"] = res.blockers
        return res

    # ------------------------------------------------------------ helpers
    def _final_gate(self, items, ledger, vstate, problems, audit):
        """Fresh final decisions, then exact content binding after all mutations.

        Recovery is over: partial atoms cannot enter the final item. Reconstructed
        and revised items get the same gate; no status string bypasses it.
        """
        from .rules import OVERCLAIM, REASONING_KINDS, TIER3

        ready = []
        for it in items:
            vs = {v.claim_index: v for v in vstate["verdicts"].get(it.item_id, [])}
            # Composed items already contain only supported claims; verify their
            # fresh indices below instead of borrowing old index provenance.
            composed = any(x["code"] == "statement_composed_from_verified_claims" and x["detail"].startswith(it.item_id + ":")
                           for x in audit["issues"])
            claims = it.claims if composed else [c for n, c in enumerate(it.claims) if n in vs and
                         (vs[n].verdict == "supported" or (vs[n].verdict == "not_checkable" and c.kind in REASONING_KINDS))]
            if not any(c.kind not in REASONING_KINDS and c.citations for c in claims):
                add_issue(audit, "info", "item_dropped_no_verified_premise", it.item_id)
                continue
            ready.append(it.model_copy(update={"claims": claims}))
        ready = self._verify(ready, ledger, vstate, problems, audit, "final_candidates")
        candidates = []
        for it in ready:
            if vstate["status"].get(it.item_id) not in ("supported", "hypothesis"):
                continue
            vs = {v.claim_index: v for v in vstate["verdicts"].get(it.item_id, [])}
            claims = [c for n, c in enumerate(it.claims) if n in vs and
                      (vs[n].verdict == "supported" or (vs[n].verdict == "not_checkable" and c.kind in REASONING_KINDS))]
            if any(c.kind not in REASONING_KINDS and c.citations for c in claims):
                candidates.append(it.model_copy(update={"claims": claims}))
        ready = candidates
        # apply_verdicts can change tier, evidence level or mechanism qualifiers.
        # Verify the exact final object once more, with no subsequent mutation.
        vr = verifier.llm_verify(ready, ledger, self.verifier_llm, self.settings.verifier_effort,
                                 statements_only=True, vctx=vstate.get("vctx"))
        rows = {v.item_id: v for v in vr.verdicts if v.claim_index == -1}
        audit["stages"]["final_item_verification"] = {
            "coverage": vr.coverage, "inputs": {i.item_id: item_digest(i) for i in ready},
            "verdicts": [v.model_dump(mode="json") for v in vr.verdicts]}
        if not vr.complete:
            add_issue(audit, "critical", "final_verification_incomplete", "Missing final full-item decisions after bounded retry")
        contracts, kept = {}, []
        for it in ready:
            v = rows.get(it.item_id)
            # A reasoning-only full-statement decision cannot certify categorical
            # clinical auxiliary assertions. Those require a supported decision.
            safe_aux = (it.bespoke_verdict in ("", "UNCERTAIN") and not it.human_precedent
                        and not it.criteria_met and not it.potential_exclusions)
            hypothesis = it.section in TIER3 and safe_aux and v is not None and v.verdict == "not_checkable"
            if (v is None or (v.verdict != "supported" and not hypothesis) or v.overgeneralization.strip()
                    or OVERCLAIM.search(item_text(it))):
                add_issue(audit, "info", "item_dropped_statement_not_verified", it.item_id)
                continue
            status = "hypothesis" if hypothesis else "supported"
            contracts[it.item_id] = {"digest": item_digest(it), "status": status}
            vstate["status"][it.item_id] = status
            kept.append(it)
        audit["item_contracts"] = contracts
        return kept

    def _verify(self, items, ledger, vstate, problems, audit, tag, statements_only=False):
        """Verify items (claims + full statements, or statements only) and apply decisions fail-closed.
        Updates vstate (statement status per item; re-indexed claim verdicts) and problems; returns kept items."""
        if not items:  # recorded, so the submission gate can tell "nothing to verify" from "verification skipped"
            audit["stages"].setdefault("D_verification_rounds", []).append({"tag": tag, "coverage": {"expected": 0}, "verdicts": []})
            return []
        vr = verifier.llm_verify(items, ledger, self.verifier_llm, self.settings.verifier_effort, statements_only=statements_only,
                                 vctx=vstate.get("vctx"))
        vstate["all"].extend(vr.verdicts)
        audit["stages"].setdefault("D_verification_rounds", []).append({"tag": tag, "coverage": vr.coverage,
                                                                         "verdicts": [v.model_dump(mode="json") for v in vr.verdicts]})
        if vr.coverage.get("missing_after_reverification"):
            add_issue(audit, "critical", "verification_incomplete",
                      f"{tag}: verifier returned no decision for {vr.coverage['missing_after_reverification']} after bounded re-verification")
        prior = [v for i in items for v in vstate["verdicts"].get(i.item_id, []) if v.claim_index >= 0] if statements_only else []
        res = apply_verdicts(items, prior + vr.verdicts, ledger=ledger)
        audit["rule_events"] += [e.__dict__ for e in res.events]
        _merge(problems, res.problems)
        for i in items:
            vstate["status"][i.item_id] = res.statement_status.get(i.item_id, "dropped")
        audit["statement_status"] = vstate["status"]
        for iid in {v.item_id for v in res.reindexed}:
            vstate["verdicts"][iid] = [v for v in res.reindexed if v.item_id == iid]
        return res.items

    def _rules(self, items, ledger, age, sex, audit, genes=None):
        c = apply_citation_rules(items, ledger)
        s = apply_section_rules(c.items, ledger, age, sex, genes)
        audit["rule_events"] += [e.__dict__ for e in c.events + s.events]
        problems: dict[str, list[str]] = {}
        _merge(problems, c.problems)
        _merge(problems, s.problems)
        return s.items, problems

    def _handoffs(self, ctx, items, ledger, toolbox, age, sex, genes, problems, audit, vstate, handoffs, open_q, cov_requests=()):
        """Research every cross-section handoff, mandatory-coverage gap and open question once (bounded), with provenance;
        verify like all items."""
        requests = ([f"[{h['handoff_id']}] {h['intervention']} -> proposed section {h['proposed_section']}: {h['reason']} "
                     f"(cites: {', '.join(h['citations'])})" for h in handoffs] + list(cov_requests)
                    + [f"[open question] {q}" for q in open_q[:6]])
        if len(requests) > 14:
            add_issue(audit, "warning", "followup_requests_truncated", f"{len(requests) - 14} follow-up requests not researched in this run")
        fu = researcher.followup(ctx, self.llm, toolbox, requests[:14], items, max_rounds=min(10, self.settings.max_tool_rounds))
        seen = {i.item_id for i in items}
        new_items = [it.model_copy(update={"item_id": researcher.unique_id(f"ho-{it.item_id}", seen)}) for it in fu.items]
        new_items, p = self._rules(new_items, ledger, age, sex, audit, genes)
        verifier.deterministic_checks(new_items, ledger, self.http)
        new_items, p2 = self._rules(new_items, ledger, age, sex, audit, genes)
        _merge(problems, p)
        _merge(problems, p2)
        kept = self._verify(new_items, ledger, vstate, problems, audit, "handoffs") if self.verify_claims else new_items
        audit["stages"]["handoff_followup"] = {"requests": requests, "raw": fu.model_dump(mode="json"), "kept": [i.item_id for i in kept],
                                               "not_confirmed": fu.searched_but_not_found}
        for h in handoffs:
            if not any(h["intervention"].lower()[:20] in (i.title + " " + i.statement).lower() for i in kept):
                add_issue(audit, "info", "handoff_not_confirmed", f"{h['handoff_id']} {h['intervention']}: not confirmed by verified evidence")
        return items + kept, problems

    def _review(self, ctx, items, ledger, toolbox, age, sex, problems, audit, vstate, genes=None):
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
            seen = {i.item_id for i in items}
            new_items = [it.model_copy(update={"item_id": researcher.unique_id(f"fu-{it.item_id}", seen)}) for it in fu.items]
            new_items, p = self._rules(new_items, ledger, age, sex, audit, genes)
            verifier.deterministic_checks(new_items, ledger, self.http)
            new_items, p2 = self._rules(new_items, ledger, age, sex, audit, genes)
            _merge(problems, p)
            _merge(problems, p2)
            kept = self._verify(new_items, ledger, vstate, problems, audit, "followup")
            audit["stages"]["E_followup"] = {"raw": fu.model_dump(mode="json"), "kept": [i.item_id for i in kept]}
            items = items + kept
        return items, problems


def _merge(a: dict[str, list[str]], b: dict[str, list[str]]) -> None:
    for k, v in b.items():
        a.setdefault(k, []).extend(v)


def save_audit(result: CaseResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{result.output.id}{'.MOCK' if result.is_mock else ''}.audit.json"
    p.write_text(json.dumps(result.audit | {"unresolved": result.unresolved, "submittable": result.submittable}, indent=1, default=str),
                 encoding="utf-8")
    (out_dir / f"{result.output.id}{'.MOCK' if result.is_mock else ''}.md").write_text(result.output.report_markdown, encoding="utf-8")
    return p
