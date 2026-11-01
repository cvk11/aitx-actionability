"""Live smoke tests against free public APIs (no credentials, no cost).
Run explicitly:  pytest -m live
Uses CFTR p.Phe508del (the organizers' public format example), not the challenge cases."""
from __future__ import annotations

import pytest

from aitx.config import get_settings
from aitx.http import HttpClient
from aitx.ledger import EvidenceLedger
from aitx.retrieval import clinicaltrials, drugs, europepmc, ncbi, variant

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def http():
    return HttpClient(get_settings(deployment_mode=True))


def test_pubmed_live(http):
    L = EvidenceLedger()
    r = ncbi.pubmed_search(http, L, "CFTR[tiab] AND elexacaftor[tiab]", 3)
    assert r["total_count"] > 10
    a = ncbi.pubmed_fetch_abstracts(http, L, [r["results"][0]["key"]])
    assert a["articles"] and a["articles"][0]["abstract"]


def test_variant_live(http):
    L = EvidenceLedger()
    a = variant.annotate_variant(http, L, "CFTR", "NM_000492.4", "c.1521_1523del", "p.Phe508del")
    assert a["computed_features"]["consequence_class"] == "inframe_del"
    assert a["computed_features"]["exon_number"] == 11  # CFTR F508del lies in exon 11 (legacy exon 10) of NM_000492.4
    assert a.get("exon_consistent_with_vep") is True


def test_trials_and_labels_live(http):
    L = EvidenceLedger()
    r = clinicaltrials.search_trials(http, L, condition="cystic fibrosis", page_size=5)
    assert r["studies"] and all(s["overall_status"] in clinicaltrials.OPEN_STATUSES + clinicaltrials.EXPANDED_ACCESS_OPEN for s in r["studies"])
    lab = drugs.fda_label_search(http, L, drug="ivacaftor")
    assert lab["results"] and "cystic fibrosis" in lab["results"][0]["sections"].get("indications_and_usage", "").lower()


def test_clinvar_and_epmc_live(http):
    L = EvidenceLedger()
    c = ncbi.clinvar_lookup(http, L, "CFTR", "c.1521_1523del", "NM_000492.4")
    assert any("athogenic" in (x["classification"] or "") for x in c["records"])
    e = europepmc.europepmc_search(http, L, '"CFTR" AND "ivacaftor"', 3)
    assert e["results"]
