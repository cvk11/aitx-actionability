"""Offline rails for the supervised self-repair runner (no network, no cost, no real repo files touched)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_supervised as sup  # noqa: E402
from aitx.config import get_settings  # noqa: E402


class FakeGit:
    def __init__(self):
        self.reverted: list[str] = []
        self.committed: list[tuple] = []

    def revert(self, paths):
        self.reverted.extend(paths)

    def commit(self, paths, summary):
        self.committed.append((list(paths), summary))


def _res(submittable, blockers=()):
    return type("CaseResult", (), {"submittable": submittable, "blockers": list(blockers)})()


CODE_FAIL = [{"code": "stage_incomplete", "detail": "StructuredOutputError: B_emerging:r1: missing or invalid billing metadata"}]


class Fixer:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = []

    def structured(self, **kw):
        self.calls.append(kw)
        assert kw["stage"] == "supervisor:fix"
        return self.proposal


def _tmp_repo(tmp_path) -> tuple[Path, Path]:
    src = tmp_path / "src" / "aitx" / "llm"
    src.mkdir(parents=True)
    target = src / "gemini_client.py"
    target.write_text("original", encoding="utf-8")
    return tmp_path, target


def test_should_fix_only_code_stage_failures():
    assert sup.should_fix(CODE_FAIL)
    assert not sup.should_fix([{"code": "stage_incomplete", "detail": "BudgetExceeded: request not sent"}])
    assert not sup.should_fix([{"code": "stage_incomplete", "detail": "ModelRefusal in D_verify"}])
    assert not sup.should_fix([{"code": "mock_output", "detail": "x"}])


def test_validate_proposal_rails(tmp_path):
    root, target = _tmp_repo(tmp_path)
    ok = sup.FixProposal(diagnosis="d", files=[sup.FixFile(path="src/aitx/llm/gemini_client.py", content="# y")])
    assert sup.validate_proposal(ok, root) == []
    bad = sup.FixProposal(diagnosis="d", files=[
        sup.FixFile(path="tests/test_units.py", content="x"),
        sup.FixFile(path="../outside.py", content="x"),
        sup.FixFile(path="src/aitx/new_file.py", content="x"),
        sup.FixFile(path="prompts/research.txt", content="x"),
    ])
    assert len(sup.validate_proposal(bad, root)) == 4


def test_traceback_files_strip_absolute_prefixes():
    tb = 'File "C:/aitx-argon-test/src/aitx/llm/gemini_client.py", line 100, in _send'
    assert sup.traceback_files(tb) == ["src/aitx/llm/gemini_client.py"]
    assert sup.traceback_files('File "C:/x/tests/test_units.py", line 1') == []  # tests are never fixable


def test_cycle_accepts_commits_and_reruns(tmp_path):
    root, target = _tmp_repo(tmp_path)
    git = FakeGit()
    runs = {"n": 0}

    def run_case(case):
        runs["n"] += 1
        return _res(submittable=runs["n"] > 1, blockers=[] if runs["n"] > 1 else CODE_FAIL), None

    s = sup.Supervisor(None, root=root, fixer_llm=Fixer(
        sup.FixProposal(diagnosis="d", summary="s", files=[sup.FixFile(path="src/aitx/llm/gemini_client.py", content="# fixed")])),
        run_case=run_case, run_suite=lambda: True, git=git)
    report = s.run({"id": "T"}, authorize_paid=True, max_cycles=2)
    assert report["outcome"] == "submittable" and runs["n"] == 2
    assert git.committed == [(["src/aitx/llm/gemini_client.py"], "s")]
    assert target.read_text(encoding="utf-8") == "# fixed"


def test_cycle_reverts_when_suite_fails(tmp_path):
    root, target = _tmp_repo(tmp_path)
    git = FakeGit()
    s = sup.Supervisor(None, root=root, fixer_llm=Fixer(
        sup.FixProposal(diagnosis="d", summary="s", files=[sup.FixFile(path="src/aitx/llm/gemini_client.py", content="# bad")])),
        run_case=lambda case: (_res(submittable=False, blockers=CODE_FAIL), None), run_suite=lambda: False, git=git)
    report = s.run({"id": "T"}, authorize_paid=True, max_cycles=2)
    assert report["outcome"] == "reverted_after_test_failure"
    assert git.reverted == ["src/aitx/llm/gemini_client.py"] and not git.committed


def test_cycle_stops_without_paid_fixer_authorization(tmp_path):
    root, _ = _tmp_repo(tmp_path)
    git = FakeGit()
    s = sup.Supervisor(None, root=root, fixer_llm=Fixer(sup.FixProposal(diagnosis="d")),
                       run_case=lambda case: (_res(submittable=False, blockers=CODE_FAIL), None), run_suite=lambda: True, git=git)
    report = s.run({"id": "T"}, authorize_paid=False, max_cycles=2)
    assert report["outcome"] == "stopped_without_paid_fixer"
    assert not git.committed and not git.reverted


def test_proposal_hitting_forbidden_area_is_rejected(tmp_path):
    root, _ = _tmp_repo(tmp_path)
    git = FakeGit()
    s = sup.Supervisor(None, root=root, fixer_llm=Fixer(
        sup.FixProposal(diagnosis="d", summary="s", files=[sup.FixFile(path="tests/test_units.py", content="x")])),
        run_case=lambda case: (_res(submittable=False, blockers=CODE_FAIL), None), run_suite=lambda: True, git=git)
    report = s.run({"id": "T"}, authorize_paid=True, max_cycles=2)
    assert report["outcome"] == "proposal_rejected"
    assert not git.committed


def _res2(submittable, blockers=(), cost=0.0):
    r = _res(submittable, blockers)
    r.audit = {"cost": {"total_usd": cost}}
    return r


def test_session_budget_stops_before_the_next_cycle(tmp_path):
    root, _ = _tmp_repo(tmp_path)
    git = FakeGit()
    runs = {"n": 0}

    def run_case(case):
        runs["n"] += 1
        return _res2(submittable=False, blockers=CODE_FAIL, cost=0.6), None

    s = sup.Supervisor(get_settings(), root=root, fixer_llm=Fixer(
        sup.FixProposal(diagnosis="d", summary="s", files=[sup.FixFile(path="src/aitx/llm/gemini_client.py", content="# x")])),
        run_case=run_case, run_suite=lambda: True, git=git, total_budget=0.5)
    report = s.run({"id": "T"}, authorize_paid=True, max_cycles=2)
    assert report["outcome"] == "total_budget_reached"
    assert runs["n"] == 1 and report["spent_usd"] == pytest.approx(0.6)
    assert len(git.committed) == 1  # the accepted fix is still recorded


def test_per_cycle_budget_shrinks_to_remaining(tmp_path):
    root, _ = _tmp_repo(tmp_path)
    git = FakeGit()
    budgets = []

    class _Sup(sup.Supervisor):
        def run_case(self, case):
            budgets.append(self.current_settings.max_cost_usd_per_case)
            return _res2(submittable=False, blockers=CODE_FAIL, cost=0.2), None

    s = _Sup(get_settings(max_cost_usd_per_case=12.0), root=root, fixer_llm=Fixer(
        sup.FixProposal(diagnosis="d", summary="s", files=[sup.FixFile(path="src/aitx/llm/gemini_client.py", content="# x")])),
        run_suite=lambda: True, git=git, total_budget=1.0)
    report = s.run({"id": "T"}, authorize_paid=True, max_cycles=3)
    assert budgets == [1.0, 0.8, 0.6]  # each cycle only gets what the session cap leaves
    assert report["outcome"] == "max_fix_cycles_exhausted"
