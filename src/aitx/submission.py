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
        # independent completion checks (not only the issue list): a result object that skipped stages,
        # verification or synthesis checking must never be submitted, whatever its blocker list says
        a = r.audit
        from .schemas import ActionItem
        from .verification_contract import contract_errors, text_digest
        final_items = [ActionItem.model_validate(i) for i in a.get("final_items", [])]
        problems += [f"{r.output.id}: {e}" for e in contract_errors(final_items, a.get("item_contracts", {}))]
        final_verification = a.get("stages", {}).get("final_item_verification")
        if final_verification is None:
            problems.append(f"{r.output.id}: final full-item verification not recorded")
        else:
            decisions = {v["item_id"]: v for v in final_verification.get("verdicts", []) if v["claim_index"] == -1}
            for it in final_items:
                decision = decisions.get(it.item_id, {})
                contract = a.get("item_contracts", {}).get(it.item_id, {})
                allowed = ("supported", "not_checkable") if contract.get("status") == "hypothesis" else ("supported",)
                if (decision.get("verdict") not in allowed or decision.get("overgeneralization")
                        or final_verification.get("inputs", {}).get(it.item_id) != contract.get("digest")):
                    problems.append(f"{r.output.id}: {it.item_id}: final decision does not verify the rendered item")
            if final_verification.get("coverage", {}).get("missing_after_reverification"):
                problems.append(f"{r.output.id}: final verification incomplete")
        if a.get("rendered_sha256") != text_digest(r.output.report_markdown):
            problems.append(f"{r.output.id}: rendered Markdown changed after final QC")
        if not a.get("stages_completed"):
            problems.append(f"{r.output.id}: mandatory stages not recorded as completed")
        if a.get("mode") == "baseline":
            problems.append(f"{r.output.id}: baseline comparison arm is not submittable")
        elif a.get("verify_claims", True):
            rounds = a.get("stages", {}).get("D_verification_rounds") or []
            if not rounds:
                problems.append(f"{r.output.id}: no claim-verification round recorded")
            if any(x.get("coverage", {}).get("missing_after_reverification") for x in rounds):
                problems.append(f"{r.output.id}: claim verification incomplete")
            if "F_synthesis_verification" not in a.get("stages", {}):
                problems.append(f"{r.output.id}: synthesis verification not recorded")
        else:
            problems.append(f"{r.output.id}: claim verification disabled")
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
