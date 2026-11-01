"""Deterministic quality control: structure, submission schema, citation links,
and a *development heuristic* scorecard (NOT the official hidden score)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .ledger import EvidenceLedger
from .template import TITLE_PREFIX, iter_headings, required_headings

PLACEHOLDER_MARKERS = ("[HIGHEST-VALUE", "URL_OR_ACCESSION", "[THERAPY NAME", "[STRATEGY", "[COMPOUND NAME", "[SYMPTOM OR", "[AGENT, DOSE",
                       "[RANK]", "[UNKNOWN CLINICAL VARIABLE]", "NCT00000000", "AITX-XXXXX", "HOW TO USE THIS TEMPLATE")
MAX_FILE_BYTES = 5 * 1024 * 1024


@dataclass
class QCResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_report_structure(markdown: str, case_id: str) -> QCResult:
    errors, warnings = [], []
    first = next((ln for ln in markdown.splitlines() if ln.strip()), "")
    if first.strip() != f"{TITLE_PREFIX}{case_id}":
        errors.append(f"First line must be '{TITLE_PREFIX}{case_id}', got {first[:80]!r}")
    heads = iter_headings(markdown)
    req = list(required_headings())
    found = [(l, t) for l, t in heads if (l, t) in set(req)]
    for h in req:
        n = found.count(h)
        if n == 0:
            errors.append(f"Missing required heading: {'#' * h[0]} {h[1]}")
        elif n > 1:
            errors.append(f"Required heading repeated {n}x: {'#' * h[0]} {h[1]}")
    dedup = list(dict.fromkeys(found))
    if not errors and dedup != req:
        errors.append("Required headings are out of order")
    h1 = [t for l, t in heads if l == 1]
    if len(h1) != 1:
        errors.append(f"Expected exactly one H1 title, found {len(h1)}")
    for m in PLACEHOLDER_MARKERS:
        if m in markdown:
            errors.append(f"Template placeholder text left in report: {m}")
    if "<!--" in markdown:
        errors.append("HTML comment (template guidance) left in report")
    raw_keys = re.findall(r"\[((?:[A-Z][A-Za-z]+:[^\]\s]+)|NCT\d{8})\](?!\()", markdown)
    if raw_keys:
        errors.append(f"Unresolved citation keys in report: {raw_keys[:5]}")
    # empty sections
    sections = re.split(r"^#{2,3} .+$", markdown, flags=re.M)
    if any(not s.strip(" \n-") for s in sections[1:]):
        warnings.append("At least one section appears empty")
    return QCResult(not errors, errors, warnings)


def check_links_against_ledger(markdown: str, ledger: EvidenceLedger) -> QCResult:
    urls = re.findall(r"\]\((https?://[^)\s]+)\)", markdown)
    known = {r.url for r in ledger.records()} | {f"https://clinicaltrials.gov/study/{k}" for k in ledger.keys() if k.startswith("NCT")}
    unknown = sorted({u for u in urls if u not in known})
    return QCResult(not unknown, [f"Link not from evidence ledger: {u}" for u in unknown])


def validate_submission(data: Any, expected_ids: list[str] | None = None) -> QCResult:
    """Mirror of the organizers' upload checks: valid JSON array, exactly one
    report per expected id, every required heading in order."""
    errors, warnings = [], []
    if isinstance(data, (str, bytes)):
        if len(data) > MAX_FILE_BYTES:
            errors.append("File exceeds 5 MB")
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            return QCResult(False, [f"Invalid JSON: {e}"])
    if not isinstance(data, list):
        return QCResult(False, ["Top-level JSON must be an array"])
    ids = []
    for i, obj in enumerate(data):
        if not isinstance(obj, dict):
            errors.append(f"Entry {i} is not an object")
            continue
        keys = set(obj)
        if keys != {"id", "report_markdown"}:
            errors.append(f"Entry {i} must have exactly keys id, report_markdown; has {sorted(keys)}")
        if not isinstance(obj.get("id"), str) or not isinstance(obj.get("report_markdown"), str):
            errors.append(f"Entry {i}: id and report_markdown must be strings")
            continue
        ids.append(obj["id"])
        r = check_report_structure(obj["report_markdown"], obj["id"])
        errors += [f"{obj['id']}: {e}" for e in r.errors]
        warnings += [f"{obj['id']}: {w}" for w in r.warnings]
        if "MOCK" in obj["report_markdown"][:400]:
            errors.append(f"{obj['id']}: mock output must never be submitted")
    if len(ids) != len(set(ids)):
        errors.append("Duplicate ids")
    if expected_ids is not None:
        if sorted(ids) != sorted(expected_ids):
            errors.append(f"IDs {sorted(ids)} do not match expected {sorted(expected_ids)}")
    return QCResult(not errors, errors, warnings)


def heuristic_scorecard(items: list[Any], verdicts: list[Any], ledger: EvidenceLedger, markdown: str) -> dict[str, Any]:
    """Development heuristic proxies for the five official dimensions.
    NOT the official score; used only for regression tracking."""
    n_claims = len(verdicts)
    sup = sum(1 for v in verdicts if v.verdict in ("supported", "not_checkable"))
    partial = sum(1 for v in verdicts if v.verdict == "partially_supported")
    bad = sum(1 for v in verdicts if v.verdict in ("unsupported", "contradicted"))
    secs = {getattr(i.section, "value", str(i.section)) for i in items}
    cond = sum(1 for i in items if i.conditional_action.strip())
    srcs = {ledger.get(k).source_type for k in ledger.keys() if ledger.get(k)}
    links = re.findall(r"\]\((https?://[^)\s]+)\)", markdown)
    return {
        "label": "DEVELOPMENT HEURISTIC - not the official AI-Tx score",
        "claims_checked": n_claims,
        "claims_supported_rate": round(sup / n_claims, 3) if n_claims else None,
        "claims_partially_supported": partial,
        "claims_unsupported_or_contradicted": bad,
        "sections_with_items": sorted(secs),
        "n_items": len(items),
        "items_with_conditional_action": cond,
        "evidence_source_types": sorted(srcs),
        "n_links": len(links),
        "n_unique_links": len(set(links)),
        "report_chars": len(markdown),
    }
