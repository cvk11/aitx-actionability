from __future__ import annotations

import json

from aitx.ledger import EvidenceLedger
from aitx.retrieval import clinicaltrials, ncbi, variant
from aitx.retrieval.registry import ToolBox
from aitx.retrieval.web import fetch_url


def test_pubmed_flow(fake_http):
    L = EvidenceLedger()
    r = ncbi.pubmed_search(fake_http, L, "GENEX")
    assert r["total_count"] == 2 and L.get("PMID:11111111").content_scope == "metadata"
    a = ncbi.pubmed_fetch_abstracts(fake_http, L, ["11111111", "PMID:22222222", "99999999"])
    assert {x["key"] for x in a["articles"]} == {"PMID:11111111", "PMID:22222222"} and a["not_found"] == ["99999999"]
    rec = L.get("PMID:11111111")
    assert rec.content_scope == "abstract" and "Case Reports" in rec.metadata["pub_types"] and rec.metadata["pmcid"] == "PMC1111111"
    ex = ncbi.pmid_exists(fake_http, ["11111111", "99999999"])
    assert ex["11111111"]["exists"] and not ex["99999999"]["exists"]


def test_trials(fake_http):
    L = EvidenceLedger()
    r = clinicaltrials.search_trials(fake_http, L, term="GENEX")
    assert {s["nct_id"] for s in r["studies"]} == {"NCT00000001", "NCT00000002"}
    assert L.get("NCT00000001").metadata["min_age_years"] == 2 and L.get("NCT00000001").metadata["max_age_years"] == 17
    assert clinicaltrials.get_trial(fake_http, L, "NCT00000099").get("error")
    assert clinicaltrials.get_trial(fake_http, L, "bad").get("error")
    assert clinicaltrials.trial_status(fake_http, "NCT00000002")["status"] == "COMPLETED"


def test_annotate_variant(fake_http):
    L = EvidenceLedger()
    a = variant.annotate_variant(fake_http, L, "GENEX", "NM_999999.1", "c.100G>A", "p.Asp34Asn")
    # fixture: c.100 -> tx 150 = last base of exon 1, while the fake VEP says exon 2 -> mismatch must be flagged
    assert a["computed_features"]["exon_number"] == 1 and a["protein_notation_consistent"] is True
    assert a["exon_consistent_with_vep"] is False and a["warnings"]
    assert "COMPUTED:GENEX:c.100G>A" in L and "REFSEQ:NM_999999.1" in L
    g = variant.gene_profile(fake_http, L, "GENEX", 34)
    assert g["gnomad_constraint"]["pLI"] == 0.99 and g["uniprot"]["features_at_position"]


def test_toolbox_errors_are_reported_not_raised(fake_http, settings):
    tb = ToolBox(fake_http, EvidenceLedger(), settings)
    txt, err = tb.execute("get_trial", {"nct_id": "NCT00000099"})
    assert err and "not found" in txt
    txt, err = tb.execute("nonexistent", {})
    assert err
    txt, err = tb.execute("pubmed_search", {"query": "x"})  # missing arg
    assert err and "bad arguments" in txt


def test_genereviews_fixture(fake_http, settings):
    L = EvidenceLedger()
    tb = ToolBox(fake_http, L, settings)
    f = json.loads(tb.execute("genereviews_find", {"gene": "GENEX", "extra_terms": []})[0])
    assert f["chapters_for_gene"][0]["nbk"] == "NBK9999"
    r = json.loads(tb.execute("genereviews_read", {"chapter": "NBK9999", "sections": []})[0])
    keys = {s["key"] for s in r["sections"]}
    assert "GENEREVIEWS:NBK9999#Treatment_of_Manifestations" in keys
    rec = L.get("GENEREVIEWS:NBK9999#Treatment_of_Manifestations")
    assert rec.url.endswith("#genex.Treatment_of_Manifestations") and rec.metadata["cited_pmids"] == ["11111111"]


def test_fetch_url_allowlist(fake_http):
    assert "allow-list" in fetch_url(fake_http, EvidenceLedger(), "https://example.com/x")["error"]
