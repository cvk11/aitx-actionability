"""Repeated-inference robustness analysis.

Adapted from the methodology of the author's radiology study
(github.com/cevikbilgin-rad/llm-radiology-uncertainty: 09_stochasticity_test.py,
scripts/reproduce_robustness.py): repeat the identical locked pipeline N times
on the same input and quantify agreement. Ordinal kappa is replaced by
set-agreement metrics suited to report content:

* per-section action-set Jaccard (normalised titles), pairwise and all-run
* tier-placement agreement for actions present in >1 run
* bespoke verdict agreement (AMENABLE / NOT AMENABLE / UNCERTAIN)
* cited-source Jaccard
* "identical across all runs" proportion per section (analogue of the
  study's "all three runs identical" statistic)
"""

from __future__ import annotations

import itertools
import re
from collections import Counter, defaultdict
from typing import Any

STOP = {"the", "a", "an", "of", "for", "and", "or", "with", "in", "to", "therapy", "treatment", "based"}


def norm(title: str) -> str:
    return " ".join(w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in STOP)


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def analyse_runs(audits: list[dict[str, Any]]) -> dict[str, Any]:
    if len(audits) < 2:
        raise ValueError("need at least two runs")
    runs = [a["final_items"] for a in audits]
    sections = sorted({i["section"] for r in runs for i in r})
    per_section: dict[str, Any] = {}
    for sec in sections:
        sets = [{norm(i["title"]) for i in r if i["section"] == sec} for r in runs]
        pair = [jaccard(x, y) for x, y in itertools.combinations(sets, 2)]
        inter, union = set.intersection(*sets), set.union(*sets)
        per_section[sec] = {"mean_pairwise_jaccard": round(sum(pair) / len(pair), 3), "all_run_jaccard": round(jaccard(inter, union), 3),
                            "identical_across_runs": all(s == sets[0] for s in sets), "core_actions": sorted(inter),
                            "unstable_actions": sorted(union - inter)}
    placements: dict[str, list[str]] = defaultdict(list)
    verdicts: dict[str, list[str]] = defaultdict(list)
    for r in runs:
        for i in r:
            placements[norm(i["title"])].append(i["section"])
            if i["section"] == "bespoke_therapies":
                verdicts[norm(i["title"])].append(i.get("bespoke_verdict", ""))
    multi = {k: v for k, v in placements.items() if len(v) > 1}
    tier_agree = sum(1 for v in multi.values() if len(set(v)) == 1)
    vmulti = {k: v for k, v in verdicts.items() if len(v) > 1}
    cites = [{c for i in r for cl in i["claims"] for c in cl["citations"]} for r in runs]
    cpair = [jaccard(x, y) for x, y in itertools.combinations(cites, 2)]
    return {
        "n_runs": len(runs),
        "per_section": per_section,
        "sections_identical_across_runs": sum(1 for v in per_section.values() if v["identical_across_runs"]),
        "n_sections": len(per_section),
        "tier_placement_agreement": {"actions_in_multiple_runs": len(multi), "same_section_every_time": tier_agree,
                                     "unstable": {k: Counter(v) for k, v in multi.items() if len(set(v)) > 1}},
        "bespoke_verdict_agreement": {"strategies": len(vmulti), "consistent": sum(1 for v in vmulti.values() if len(set(v)) == 1)},
        "citation_mean_pairwise_jaccard": round(sum(cpair) / len(cpair), 3),
        "cost_usd": [a.get("cost", {}).get("total_usd") for a in audits],
    }
