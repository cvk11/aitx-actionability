from aitx.robustness import analyse_runs


def _audit(titles_sections, cost=1.0):
    return {"final_items": [{"title": t, "section": s, "bespoke_verdict": v, "claims": [{"citations": [c]}]} for t, s, v, c in titles_sections],
            "cost": {"total_usd": cost}}


def test_analyse_runs():
    a = _audit([("Drug Q", "off_label_expanded_access", "", "PMID:1"), ("ASO", "bespoke_therapies", "UNCERTAIN", "COMPUTED:x")])
    b = _audit([("drug q therapy", "drug_repurposing", "", "PMID:1"), ("ASO", "bespoke_therapies", "UNCERTAIN", "COMPUTED:x")])
    r = analyse_runs([a, b])
    assert r["per_section"]["bespoke_therapies"]["identical_across_runs"]
    assert r["tier_placement_agreement"]["actions_in_multiple_runs"] == 2
    assert r["tier_placement_agreement"]["same_section_every_time"] == 1  # Drug Q moved tiers between runs
    assert r["bespoke_verdict_agreement"] == {"strategies": 1, "consistent": 1}
    assert r["citation_mean_pairwise_jaccard"] == 1.0
