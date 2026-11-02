#!/usr/bin/env python
"""Supervised B_full runner: Argon-driven, rail-guarded self-repair (PAID, gated).

Runs one case through the B_full pipeline. If the run fails with a CODE error
(stage_incomplete + an exception signature, or an in-process exception whose
text carries one), it asks gemini-4-argon — through the API, with its own small
capped budget — to propose a minimal fix. The proposal is applied ONLY under
hard rails, re-verified by the full offline suite, and committed; a proposal
that breaks any offline test is reverted automatically. Budget/refusal aborts
and submittable results never trigger the fixer.

HARD RAILS (enforced in code, not just the prompt):
  * only EXISTING, GIT-TRACKED files under src/aitx/ may be modified, and the
    resolved path must stay inside src/aitx (junctions/symlinks cannot escape);
  * the git tree must be clean (no tracked modifications) before any edit, so a
    revert can never destroy the operator's own work;
  * every applied proposal must pass the full offline suite, else git-reverted;
  * accepted fixes are committed with an explicit pathspec; a further paid run happens only while
    a cycle remains within the budget — with a single cycle the fix is committed but not re-run,
    and the outcome says so (fix_committed_but_not_rerun);
  * a hard session cap (--max-total-cost-usd, DEFAULT 12.00) bounds pipeline
    runs AND fixer calls together; a crashed run (no result object) is charged
    conservatively at its full cycle budget so the cap can never under-count;
  * at most --max-fix-cycles (clamped 1..2) fix proposals.

No paid request is sent without --authorize-paid. Patient artifacts are written
to outputs/internal/ (git-ignored); every cycle's audit is kept in a per-cycle
directory so nothing is overwritten. Checkpoint/resume is intentionally not part
of this tool: an aborted case restarts from scratch.
"""
from __future__ import annotations

import argparse
import json
import math
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
DEFAULT_SESSION_CAP_USD = 12.0


class FixFile(BaseModel):
    path: str
    content: str


class FixProposal(BaseModel):
    diagnosis: str
    files: list[FixFile] = Field(default_factory=list)
    summary: str = ""


def should_fix(blockers: list[dict] | None, err: str | None = None) -> bool:
    """Only code-level stage failures trigger the fixer; budget/refusal never does."""
    for b in blockers or []:
        if b.get("code") == "stage_incomplete":
            detail = b.get("detail", "")
            if CODE_ERROR.search(detail) and not any(s in detail for s in STOP_ERRORS):
                return True
    return bool(err) and bool(CODE_ERROR.search(err or "")) and not any(s in (err or "") for s in STOP_ERRORS)


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
    src_root = (root / "src" / "aitx").resolve()
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
            continue
        if not target.is_file():
            problems.append(f"target is not a regular file: {f.path!r}")
            continue
        try:
            resolved = target.resolve()
        except OSError:
            problems.append(f"target cannot be resolved: {f.path!r}")
            continue
        if not resolved.is_relative_to(src_root):
            problems.append(f"target resolves outside src/aitx (junction/symlink): {f.path!r}")
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


def fixer_context(failure: str) -> dict[str, Any]:
    """Failure text (incl. any traceback) plus the content of the source files it names (bounded)."""
    files: dict[str, str] = {}
    paths = traceback_files(failure) or DEFAULT_CONTEXT_FILES
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
        except Exception as e:  # e.g. a QC/link failure raised outside the pipeline's own handler
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
            fixer_budget = max(0.0, min(self.settings.max_fix_cost_usd if self.settings is not None else 1.0,
                                        self.total_budget - self.spent))
        else:
            fixer_budget = self.settings.max_fix_cost_usd if self.settings is not None else 1.0
        fixer = self.fixer or GeminiLLM(get_settings(max_tokens=8192, max_attempts=2, effort="low"),
                                        CostTracker(fixer_budget))
        ctx = fixer_context(failure)
        user = json.dumps({"task": "Diagnose the failure and propose the smallest safe fix under the hard rules.",
                           "context": ctx}, indent=1)
        try:
            return fixer.structured(stage="supervisor:fix", system=SYSTEM, user=user, output_model=FixProposal, effort="low")
        finally:
            # account the fixer's spend even when the response was billed but unparseable (fixer_error)
            cost = getattr(fixer, "cost", None)
            if cost is not None:
                self.spent += getattr(cost, "total_usd", 0.0)

    def _stop(self, report: dict[str, Any], outcome: str) -> dict[str, Any]:
        report["outcome"] = outcome
        report["spent_usd"] = round(self.spent, 4)
        return report

    def run(self, case: dict, authorize_paid: bool, max_cycles: int = 2) -> dict[str, Any]:
        report: dict[str, Any] = {"case_id": case.get("id"), "cycles": self.cycles}
        case_dir = self.root / "outputs" / "internal" / "supervised" / str(case.get("id"))
        for cycle in range(max_cycles):
            entry: dict[str, Any] = {"cycle": cycle}
            if self.total_budget is not None:
                remaining = self.total_budget - self.spent
                if remaining <= 0.05:
                    entry["outcome"] = "total_budget_reached"
                    self.cycles.append(entry)
                    return self._stop(report, "total_budget_reached")
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
                try:
                    from aitx.pipeline import save_audit

                    p = save_audit(res, case_dir / f"cycle{cycle}")
                    entry["artifacts"] = str(p)
                except Exception as e:
                    entry["artifact_save_error"] = f"{type(e).__name__}: {e}"
            else:
                # a crashed run leaves no cost object: charge the full cycle budget conservatively so
                # the session cap can never under-count real provider spend
                charge = self.current_settings.max_cost_usd_per_case if self.current_settings is not None else 0.0
                self.spent += charge
                entry["run_cost_usd"] = round(charge, 4)
                entry["conservative_charge"] = True
            entry["spent_usd"] = round(self.spent, 4)
            blockers = getattr(res, "blockers", None) if res is not None else None
            entry["blockers"] = blockers
            if res is not None and res.submittable:
                entry["outcome"] = "submittable"
                self.cycles.append(entry)
                report["result"] = res
                return self._stop(report, "submittable")
            if self.total_budget is not None and self.spent >= self.total_budget - 0.05:
                # post-charge re-check: an overshoot-settled bill (thinking tokens) can push spent past
                # the cap within a cycle; stop before any fixer/retry instead of waiting for the next cycle
                entry["outcome"] = "session_cap_reached"
                self.cycles.append(entry)
                return self._stop(report, "session_cap_reached")
            if not authorize_paid:
                entry["outcome"] = "not_authorized_to_fix"
                self.cycles.append(entry)
                return self._stop(report, "stopped_without_paid_fixer")
            if not should_fix(blockers, err):
                entry["outcome"] = "failure_is_not_a_code_error"
                self.cycles.append(entry)
                return self._stop(report, "not_fixable")
            if self._git.tracked_changes():
                # never apply a fix onto the operator's own uncommitted edits: a revert could destroy them
                entry["outcome"] = "dirty_tree_refusing_fix"
                self.cycles.append(entry)
                return self._stop(report, "dirty_tree_refusing_fix")
            failure = err or json.dumps(blockers)[:6000]
            try:
                prop = self.propose_fix(failure)
            except Exception as e:
                entry["outcome"] = "fixer_error"
                entry["fixer_error"] = f"{type(e).__name__}: {e}"
                self.cycles.append(entry)
                return self._stop(report, "fixer_error")
            entry["diagnosis"] = prop.diagnosis[:2000]
            entry["summary"] = prop.summary[:500]
            problems = validate_proposal(prop, self.root)
            touched = []
            for f in prop.files:
                # canonicalize through resolve(): on Windows this returns the on-disk case, so the
                # write and the git revert can never disagree about the pathspec (case-sensitive git)
                resolved = (self.root / f.path).resolve()
                touched.append(resolved.relative_to(self.root.resolve()).as_posix())
                if not self._git.is_tracked(touched[-1]):
                    problems.append(f"file is not tracked by git (not revertable): {f.path!r}")
            if problems:
                entry["outcome"] = "proposal_rejected"
                entry["rail_violations"] = problems
                self.cycles.append(entry)
                return self._stop(report, "proposal_rejected")
            entry["files"] = touched
            try:
                for f, p in zip(prop.files, touched):
                    (self.root / p).write_text(f.content, encoding="utf-8")
            except Exception as e:
                self._git.revert(touched)  # tracked files only: restore whatever was already written
                entry["outcome"] = "apply_error_reverted"
                entry["apply_error"] = f"{type(e).__name__}: {e}"
                self.cycles.append(entry)
                return self._stop(report, "apply_error_reverted")
            if not self.run_suite():
                self._git.revert(touched)
                entry["outcome"] = "reverted_after_test_failure"
                entry["suite_output_tail"] = getattr(self, "last_suite_output", "")[-800:]
                self.cycles.append(entry)
                return self._stop(report, "reverted_after_test_failure")
            try:
                self._git.commit(touched, prop.summary[:140])
                entry["outcome"] = "fix_accepted_and_committed"
            except Exception as e:
                self._git.revert(touched)
                entry["outcome"] = "commit_failed_reverted"
                entry["commit_error"] = f"{type(e).__name__}: {e}"
                self.cycles.append(entry)
                return self._stop(report, "commit_failed_reverted")
            self.cycles.append(entry)
        if self.cycles and self.cycles[-1].get("outcome") == "fix_accepted_and_committed":
            return self._stop(report, "fix_committed_but_not_rerun")
        return self._stop(report, "max_fix_cycles_exhausted")


class _Git:
    def __init__(self, root: Path):
        self.root = root
        self.commands: list[list[str]] = []

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)

    def tracked_changes(self) -> list[str]:
        r = self._run(["status", "--porcelain", "--untracked-files=no"])
        return [line[3:] for line in (r.stdout or "").splitlines() if line.strip()]

    def is_tracked(self, path: str) -> bool:
        r = self._run(["ls-files", "--error-unmatch", "--", path])
        return r.returncode == 0

    def revert(self, paths: list[str]) -> None:
        self.commands.append(["checkout", "--", *paths])
        self._run(["checkout", "--", *paths])

    def commit(self, paths: list[str], summary: str) -> None:
        self.commands.append(["commit", summary])
        self._run(["add", "--", *paths])
        r = self._run(["commit", "-m", f"supervised fix: {summary}", "--", *paths])
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "commit failed")[:200])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Supervised B_full run with Argon-driven, rail-guarded self-repair")
    ap.add_argument("--case-id", required=True)
    ap.add_argument("--authorize-paid", action="store_true", help="allow the paid fixer calls (pipeline itself is paid regardless)")
    ap.add_argument("--max-fix-cycles", type=int, default=1)
    ap.add_argument("--max-fix-cost-usd", type=float, default=1.0)
    ap.add_argument("--max-total-cost-usd", type=float, default=DEFAULT_SESSION_CAP_USD,
                    help="hard session cap across pipeline runs AND fixer calls (default 12.00)")
    args = ap.parse_args(argv)
    args.max_fix_cycles = max(1, min(2, args.max_fix_cycles))
    if not (math.isfinite(args.max_total_cost_usd) and args.max_total_cost_usd > 0):
        print("--max-total-cost-usd must be a positive finite number", file=sys.stderr)
        return 2
    if not (math.isfinite(args.max_fix_cost_usd) and args.max_fix_cost_usd > 0):
        print("--max-fix-cost-usd must be a positive finite number", file=sys.stderr)
        return 2
    s = get_settings(max_fix_cost_usd=args.max_fix_cost_usd)
    setup_logging(s.log_level)
    cases = [c for c in load_cases() if c["id"] == args.case_id]
    if not cases:
        print(f"unknown case id {args.case_id}", file=sys.stderr)
        return 2
    print(f"Supervised B_full run for {args.case_id} — paid pipeline; fixer paid only with --authorize-paid "
          f"(fixer cap ${args.max_fix_cost_usd:.2f}/cycle, {args.max_fix_cycles} cycle(s), "
          f"session cap ${args.max_total_cost_usd:.2f}; application reservation guard, not a provider-enforced cap).")
    sup = Supervisor(s, total_budget=args.max_total_cost_usd)
    try:
        report = sup.run(cases[0], authorize_paid=args.authorize_paid, max_cycles=args.max_fix_cycles)
    except Exception as e:
        report = {"case_id": args.case_id, "cycles": sup.cycles, "outcome": "unexpected_error",
                  "unexpected_error": f"{type(e).__name__}: {e}"}
    report.setdefault("spent_usd", round(sup.spent, 4))
    out = ROOT / "outputs" / "internal" / f"{args.case_id}.supervised.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"outcome": report.get("outcome"), "cycles": len(report.get("cycles", [])),
                      "spent_usd": report.get("spent_usd"), "report": str(out)}, indent=1))
    return 0 if report.get("outcome") == "submittable" else 1


if __name__ == "__main__":
    sys.exit(main())
