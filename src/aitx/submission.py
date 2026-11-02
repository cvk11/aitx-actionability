"""Submission assembly gate (B8): only complete, fully verified, non-mock reports may enter aitx_submission.json."""

from __future__ import annotations

from typing import Any

from .qc import validate_submission


class SubmissionBlocked(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("submission blocked:\n- " + "\n- ".join(problems))
        self.problems = problems


def assemble_submission(results: list[Any], expected_ids: list[str], *, allow_mock: bool = False) -> list[dict[str, str]]:
    """Return the submission array or raise SubmissionBlocked.

    Requires for every case: all mandatory stages completed, complete verification decisions, valid ledger
    links, structural QC, no critical issue (budget/refusal/incomplete verification/unverified trial status),
    and real (non-mock) inference. `allow_mock` exists only for tests of the other gates."""
    problems: list[str] = []
    for r in results:
        blockers = [b for b in r.blockers if not (allow_mock and b["code"] == "mock_output")]
        problems += [f"{r.output.id}: [{b['code']}] {b['detail']}" for b in blockers]
        qc = r.audit.get("qc", {})
        if not qc.get("structure", {}).get("ok") or not qc.get("links", {}).get("ok"):
            problems.append(f"{r.output.id}: structural or citation-link QC failed")
    out = [r.output.model_dump() for r in results]
    v = validate_submission(out, expected_ids)
    if not allow_mock or not v.ok:
        errs = [e for e in v.errors if not (allow_mock and "mock output" in e)]
        problems += errs
    if problems:
        raise SubmissionBlocked(problems)
    return out
