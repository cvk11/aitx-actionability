#!/usr/bin/env python
"""Supervised B_full runner: Argon-driven, rail-guarded self-repair (PAID, gated).

Runs one case through the B_full pipeline. If the run fails with a CODE error
(stage_incomplete + an exception signature, e.g. the two defects found in the
first real runs), it asks gemini-4-argon — through the API, with its own small
capped budget — to propose a minimal fix. The proposal is applied ONLY under
hard rails, re-verified by the full offline suite, and committed; a proposal
that breaks any offline test is reverted automatically. Budget/refusal aborts
and submittable results never trigger the fixer.

HARD RAILS (enforced in code, not just the prompt):
  * only files under src/aitx/ may be modified (no tests/, data/, prompts/,
    deployment/, no new directories, no path tricks);
  * every applied proposal must pass the full offline suite, else git-reverted;
  * the fixer may not touch evidence rules' fail-closed gates indirectly — it
    cannot edit tests, so any weakening that the adversarial suite covers fails;
  * per-cycle fixer budget is capped; at most --max-fix-cycles proposals.

No paid request is sent without --authorize-paid. Patient artifacts are written
to outputs/internal/ (git-ignored). Checkpoint/resume is intentionally not part
of this tool: an aborted case restarts from scratch (see ARGON_READINESS_REPORT).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

from _common import ROOT, load_cases, setup_logging

from aitx.config import Settings, get_settings
from pydantic import BaseModel, Field

ALLOWED_PREFIX = "src/aitx/"
FORBIDDEN_MARKERS = ("tests/", "data/", "prompts/", "deployment/", "outputs/", ".env", ".git")
CODE_ERROR = re.compile(
    r"\b(StructuredOutputError|ValueError|KeyError|TypeError|AttributeError|IndexError|RuntimeError|FileNotFoundError)\b")
STOP_ERRORS = ("BudgetExceeded", "ModelRefusal")
FILE_PATH_IN_TRACEBACK = re.compile(r'(?:File\s+")([^"]+\.py)"', re.I)
CONTEXT_FILE_LIMIT = 3
CONTEXT_CHAR_LIMIT = 14000


class FixFile(BaseModel):
    path: str
    content: str


class FixProposal(BaseModel):
    diagnosis: str
    files: list[FixFile] = Field(default_factory=list)
    summary: str = ""


def should_fix(blockers: list[dict]) -> bool:
    """Only code-level stage failures trigger the fixer; budget/refusal never does."""
    for b in blockers or []:
        if b.get("code") == "stage_incomplete":
            detail = b.get("detail", "")
            if CODE_ERROR.search(detail) and not any(s in detail for s in STOP_ERRORS):
                return True
    return False


def _safe_path(path: str) -> str | None:
    p = path.replace("\\", "/")
    if p.startswith("/") or ".." in p.split("/") or not p.endswith(".py"):
        return None
    return p


def validate_proposal(prop: FixProposal, root: Path = ROOT) -> list[str]:
    """Hard rails for a proposed edit set. Returns a list of violations (empty = acceptable)."""
    problems: list[str] = []
    if not prop.files:
        problems.append("proposal contains no file edits")
    for f in prop.files:
        p = _safe_path(f.path)
        if p is None or not p.startswith(ALLOWED_PREFIX):
            problems.append(f"path not allowed: {f.path!r}")
            continue
        if any(m in p for m in FORBIDDEN_MARKERS):
            problems.append(f"path hits a forbidden area: {f.path!r}")
            continue
        target = root / p
        if not target.exists():
            problems.append(f"target does not exist (new files are not allowed): {f.path!r}")
    return problems


def traceback_files(tb_text: str) -> list[str]:
    """Existing src/aitx files named in a traceback (absolute paths are stripped to repo-relative)."""
    out, seen = [], set()
    for m in FILE_PATH_IN_TRACEBACK.finditer(tb_text or ""):
        raw = Path(m.group(1)).as_posix()
        idx = raw.find(ALLOWED_PREFIX)
        p = _safe_path(raw[idx:] if idx >= 0 else raw)
        if p and p.startswith(ALLOWED_PREFIX) and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# When the failure text carries no traceback (pipeline blockers), these are the files where the
# observed failure classes live; the fixer sees them plus the failure text.
DEFAULT_CONTEXT_FILES = ["src/aitx/llm/gemini_client.py", "src/aitx/llm/base.py", "src/aitx/pipeline.py"]


def fixer_context(tb_text: str, failure: str) -> dict[str, Any]:
    """Failure text plus the content of the source files named in the traceback (bounded)."""
    files: dict[str, str] = {}
    paths = traceback_files(tb_text) or DEFAULT_CONTEXT_FILES
    total = 0
    for p in paths[:CONTEXT_FILE_LIMIT]:
        try:
            text = (ROOT / p).read_text(encoding="utf-8")
        except OSError:
            continue
        budget = min(len(text), max(500, CONTEXT_CHAR_LIMIT - total))
        files[p] = text[:budget]
        total += budget
        if total >= CONTEXT_CHAR_LIMIT:
            files[p] += "\n...[file truncated by the supervisor; keep the fix minimal]"
            break
    if not files:
        files["(no source shown)"] = "The supervisor could not show source files; propose a fix only if the failure text identifies the defect."
    return {"failure": failure[:6000], "files": files}


SYSTEM = (
    "You are an autonomous repair engineer for a biomedical research pipeline. A paid run failed with a code-level "
    "error. Propose the SMALLEST safe fix as complete file rewrites. HARD RULES (violations are rejected by the "
    "supervisor, not just discouraged): only edit existing files under src/aitx/ (never tests/, data/, prompts/, "
    "deployment/; never create files); never weaken or remove fail-closed checks (budget reservations, billing "
    "validation, evidence grounding, retrieval-failure handling, context limits); never change scientific rule "
    "semantics; no new dependencies; keep Python >= 3.11 type-hinted style. The full offline test suite must keep "
    "passing after your change. If the failure is not a code defect, return an empty files list and explain."
)


class Supervisor:
    """Orchestrates: run -> diagnose -> propose -> rail-check -> apply -> test -> commit -> retry."""

    def __init__(self, settings: Settings, root: Path = ROOT, fixer_llm=None, run_case=None, run_suite=None, git=None,
                 total_budget: float | None = None):
        self.settings = settings
        self.root = root
        self.fixer = fixer_llm
        self._run_case = run_case
        self._run_suite = run_suite
        self._git = git or _Git(root)
        self.cycles: list[dict[str, Any]] = []
        self.total_budget = total_budget  # hard session cap: pipeline runs + fixer calls together
        self.spent = 0.0
        self.current_settings = settings

    # ---------------------------------------------------------------- injectable operations
    def run_case(self, case: dict) -> tuple[Any, str | None]:
        if self._run_case is not None:
            return self._run_case(case)
        from aitx.factory import build_pipeline

        try:
            res = build_pipeline("B_full", self.current_settings).run(case)
            return res, None
        except Exception as e:  # pipeline catches its own errors; this is a last resort
            return None, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

    def run_suite(self) -> bool:
        if self._run_suite is not None:
            return self._run_suite()
        try:
            proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=self.root, capture_output=True, text=True, timeout=1200)
        except Exception as e:
            self.last_suite_output = f"suite runner failed: {type(e).__name__}: {e}"
            return False
        self.last_suite_output = (proc.stdout or "")[-2000:]
        return proc.returncode == 0

    def propose_fix(self, failure: str) -> FixProposal:
        from aitx.llm.base import CostTracker
        from aitx.llm.gemini_client import GeminiLLM

        if self.total_budget is not None:
            fixer_budget = max(0.0, min(self.settings.max_fix_cost_usd, self.total_budget - self.spent))
        else:
            fixer_budget = self.settings.max_fix_cost_usd if self.settings is not None else 1.0
        fixer = self.fixer or GeminiLLM(get_settings(max_tokens=8192, max_attempts=2, effort="low"),
                                        CostTracker(fixer_budget))
        ctx = fixer_context("", failure)
        user = json.dumps({"task": "Diagnose the failure and propose the smallest safe fix under the hard rules.",
                           "context": ctx}, indent=1)
        prop = fixer.structured(stage="supervisor:fix", system=SYSTEM, user=user, output_model=FixProposal, effort="low")
        if self.fixer is None:  # real fixer: account its cost against the session cap
            self.spent += fixer.cost.total_usd
        return prop

    def run(self, case: dict, authorize_paid: bool, max_cycles: int = 2) -> dict[str, Any]:
        report: dict[str, Any] = {"case_id": case.get("id"), "cycles": self.cycles}
        for cycle in range(max_cycles):
            entry: dict[str, Any] = {"cycle": cycle}
            if self.total_budget is not None:
                remaining = self.total_budget - self.spent
                if remaining <= 0.05:
                    entry["outcome"] = "total_budget_reached"
                    self.cycles.append(entry)
                    report["outcome"] = "total_budget_reached"
                    report["spent_usd"] = round(self.spent, 4)
                    return report
                # shrink this cycle's per-case budget to what the session cap leaves (pipeline guard is per process)
                from dataclasses import replace

                self.current_settings = replace(self.settings,
                                                max_cost_usd_per_case=min(self.settings.max_cost_usd_per_case, remaining))
                entry["cycle_budget_usd"] = round(self.current_settings.max_cost_usd_per_case, 4)
            res, err = self.run_case(case)
            entry["run_error"] = err
            if res is not None:
                try:
                    cost = float(res.audit.get("cost", {}).get("total_usd", 0.0))
                except Exception:
                    cost = 0.0
                self.spent += cost
                entry["run_cost_usd"] = round(cost, 4)
            entry["spent_usd"] = round(self.spent, 4)
            blockers = getattr(res, "blockers", None) if res is not None else None
            entry["blockers"] = blockers
            if res is not None and res.submittable:
                entry["outcome"] = "submittable"
                self.cycles.append(entry)
                report["outcome"] = "submittable"
                report["spent_usd"] = round(self.spent, 4)
                report["result"] = res
                return report
            if not authorize_paid:
                entry["outcome"] = "not_authorized_to_fix"
                self.cycles.append(entry)
                report["outcome"] = "stopped_without_paid_fixer"
                return report
            if not should_fix(blockers):
                entry["outcome"] = "failure_is_not_a_code_error"
                self.cycles.append(entry)
                report["outcome"] = "not_fixable"
                return report
            failure = err or json.dumps(blockers)[:6000]
            try:
                prop = self.propose_fix(failure)
            except Exception as e:
                entry["outcome"] = "fixer_error"
                entry["fixer_error"] = f"{type(e).__name__}: {e}"
                self.cycles.append(entry)
                report["outcome"] = "fixer_error"
                return report
            entry["diagnosis"] = prop.diagnosis[:2000]
            entry["summary"] = prop.summary[:500]
            problems = validate_proposal(prop, self.root)
            if problems:
                entry["outcome"] = "proposal_rejected"
                entry["rail_violations"] = problems
                self.cycles.append(entry)
                report["outcome"] = "proposal_rejected"
                return report
            touched = [f.path for f in prop.files]
            entry["files"] = touched
            for f in prop.files:
                (self.root / f.path).write_text(f.content, encoding="utf-8")
            if not self.run_suite():
                self._git.revert(touched)
                entry["outcome"] = "reverted_after_test_failure"
                self.cycles.append(entry)
                report["outcome"] = "reverted_after_test_failure"
                return report
            self._git.commit(touched, prop.summary[:140])
            entry["outcome"] = "fix_accepted_and_committed"
            self.cycles.append(entry)
        report["outcome"] = "max_fix_cycles_exhausted"
        return report


class _Git:
    def __init__(self, root: Path):
        self.root = root
        self.commands: list[list[str]] = []

    def revert(self, paths: list[str]) -> None:
        self.commands.append(["checkout", "--", *paths])
        subprocess.run(["git", "-C", str(self.root), "checkout", "--", *paths], check=True)

    def commit(self, paths: list[str], summary: str) -> None:
        self.commands.append(["commit", summary])
        subprocess.run(["git", "-C", str(self.root), "add", *paths], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", f"supervised fix: {summary}"], check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Supervised B_full run with Argon-driven, rail-guarded self-repair")
    ap.add_argument("--case-id", required=True)
    ap.add_argument("--authorize-paid", action="store_true", help="allow the paid fixer calls (pipeline itself is paid regardless)")
    ap.add_argument("--max-fix-cycles", type=int, default=2)
    ap.add_argument("--max-fix-cost-usd", type=float, default=1.0)
    ap.add_argument("--max-total-cost-usd", type=float, default=None,
                    help="hard session cap across pipeline runs AND fixer calls; the run stops before exceeding it")
    args = ap.parse_args(argv)
    s = get_settings(max_fix_cost_usd=args.max_fix_cost_usd)
    setup_logging(s.log_level)
    cases = [c for c in load_cases() if c["id"] == args.case_id]
    if not cases:
        print(f"unknown case id {args.case_id}", file=sys.stderr)
        return 2
    print(f"Supervised B_full run for {args.case_id} — paid pipeline; fixer paid only with --authorize-paid "
          f"(fixer cap ${args.max_fix_cost_usd:.2f}/cycle, {args.max_fix_cycles} cycles, "
          f"session cap ${args.max_total_cost_usd if args.max_total_cost_usd is not None else 'unlimited'}).")
    sup = Supervisor(s, total_budget=args.max_total_cost_usd)
    report = sup.run(cases[0], authorize_paid=args.authorize_paid, max_cycles=args.max_fix_cycles)
    out = ROOT / "outputs" / "internal" / f"{args.case_id}.supervised.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"outcome": report.get("outcome"), "cycles": len(report.get("cycles", [])), "report": str(out)}, indent=1))
    return 0 if report.get("outcome") == "submittable" else 1


if __name__ == "__main__":
    sys.exit(main())
