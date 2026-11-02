"""Regression tests for the PubMed / E-utilities retrieval-failure remediation.

Defect (found in the free AITX-40002 preflight on commit 03f2bde): an NCBI ESearch response with HTTP 200 whose body
carried ``esearchresult.ERROR`` ("Search Backend failed ... 502") was read as ``count 0``, recorded as an empty
(searched, nothing found) query and cached on disk for seven days. These tests use the fictional gene GENEX only.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import tenacity

from fake_http import handler as base_handler

from aitx.config import get_settings
from aitx.http import HttpClient, ResponseValidationError
from aitx.ledger import EvidenceLedger, EvidenceRecord
from aitx.retrieval import europepmc, ncbi

NCBI_BACKEND_ERROR = {"header": {"type": "esearch", "version": "0.3"},
                      "esearchresult": {"ERROR": "Search Backend failed: Pubmed 2.0 search API: HTTP request returned 502 status."}}


def _q(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


def _esearch(ids: list[str], count: int | None = None, **extra) -> httpx.Response:
    return httpx.Response(200, json={"esearchresult": {"count": str(len(ids) if count is None else count), "idlist": ids, **extra}})


def _esummary(ids: list[str]) -> httpx.Response:
    return httpx.Response(200, json={"result": {"uids": ids, **{i: {"title": f"title {i}", "pubdate": "2024", "source": "J",
                                                                    "pubtype": ["Journal Article"]} for i in ids}}})


class Scripted:
    """MockTransport handler: a list of responses per matcher, consumed in order (last one repeats); counts calls."""

    def __init__(self, rules: list[tuple], fallback=base_handler):
        self.rules, self.fallback, self.calls = rules, fallback, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        for match, responses in self.rules:
            if match(request):
                r = responses.pop(0) if len(responses) > 1 else responses[0]
                return r(request) if callable(r) else r
        return self.fallback(request)


def _client(rules, tmp_path=None, **kw) -> tuple[HttpClient, Scripted]:
    s = get_settings(deployment_mode=tmp_path is None, **({"cache_dir": tmp_path} if tmp_path is not None else {}), **kw)
    h = Scripted(rules)
    http = HttpClient(s, transport=httpx.MockTransport(h))
    http.validation_backoff_s = 0.0
    return http, h


def is_esearch(term_part: str = "", db: str = "pubmed"):
    return lambda r: r.url.path.endswith("esearch.fcgi") and _q(r).get("db") == db and term_part in _q(r).get("term", "")


@pytest.fixture(autouse=True)
def _no_tenacity_wait(monkeypatch):
    monkeypatch.setattr(HttpClient._do.retry, "wait", tenacity.wait_none())


# ------------------------------------------------------------------ 1. HTTP 200 + ERROR is a failure, never "0 hits"
def test_http200_with_esearch_error_is_failed_retrieval_not_empty():
    http, h = _client([(is_esearch(), [httpx.Response(200, json=NCBI_BACKEND_ERROR)])])
    L = EvidenceLedger()
    with pytest.raises(ResponseValidationError, match="Search Backend failed"):
        ncbi.pubmed_search(http, L, "GENEX[tiab] AND treat*[tiab]")
    assert sum("esearch.fcgi" in c for c in h.calls) == 3  # bounded retry of the invalid body
    q = L.queries[-1]
    assert q.error and "Search Backend failed" in q.error and q.n_results == 0
    st = L.retrieval_status()["pubmed_search"]
    assert st["failed"] == 1 and st["empty"] == 0


def test_transient_esearch_error_then_success_returns_real_hits():
    http, _ = _client([(is_esearch(), [httpx.Response(200, json=NCBI_BACKEND_ERROR), _esearch(["11111111"])])])
    L = EvidenceLedger()
    r = ncbi.pubmed_search(http, L, "GENEX[tiab]")
    assert r["total_count"] == 1 and [x["key"] for x in r["results"]] == ["PMID:11111111"] and r["status"] == "ok"
    assert L.retrieval_status()["pubmed_search"] == {"with_results": 1, "empty": 0, "failed": 0, "truncated": 0, "partial": 0}


# ------------------------------------------------------------------ 2. never cached; poisoned cache entries evicted
def test_backend_error_is_never_cached_on_disk(tmp_path):
    http, _ = _client([(is_esearch(), [httpx.Response(200, json=NCBI_BACKEND_ERROR)])], tmp_path)
    with pytest.raises(ResponseValidationError):
        ncbi.pubmed_search(http, EvidenceLedger(), "GENEX[tiab]")
    cached = [json.loads(p.read_text()) for p in (tmp_path / "http").rglob("*.json")] if (tmp_path / "http").exists() else []
    assert not any("esearch.fcgi" in c["url"] for c in cached)


def test_error_cached_by_an_older_version_is_evicted_and_refetched(tmp_path):
    # 1) simulate the old behaviour: the failed body sits in the disk cache under the real request key
    http_old, _ = _client([(is_esearch(), [httpx.Response(200, json=NCBI_BACKEND_ERROR)])], tmp_path)
    http_old.validation_attempts = 1
    r_old = http_old._do("GET", f"{ncbi.EUTILS}/esearch.fcgi",
                         {"db": "pubmed", "term": "GENEX[tiab]", "retmode": "json", "retmax": 20, "sort": "relevance", "tool": "aitx-actionability"},
                         None, None, "eutils.ncbi.nlm.nih.gov", (200,), True)
    import hashlib

    params = {"db": "pubmed", "term": "GENEX[tiab]", "retmode": "json", "retmax": 20, "sort": "relevance", "tool": "aitx-actionability"}
    key = hashlib.sha256(json.dumps(["GET", f"{ncbi.EUTILS}/esearch.fcgi", sorted(params.items()), None, True], sort_keys=True, default=str).encode()).hexdigest()
    p = http_old._cache_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    import time

    p.write_text(json.dumps({"url": r_old.url, "status": 200, "text": r_old.text, "fetched_at": r_old.fetched_at, "stored": time.time()}))
    # 2) the new client must not serve it: evict, re-fetch, and return the genuine hits
    http, h = _client([(is_esearch(), [_esearch(["11111111"])])], tmp_path)
    L = EvidenceLedger()
    r = ncbi.pubmed_search(http, L, "GENEX[tiab]")
    assert r["total_count"] == 1 and http.cache_evictions == 1
    assert any("esearch.fcgi" in c for c in h.calls)
    assert "ERROR" not in json.loads(p.read_text())["text"]


def test_startup_sweep_removes_error_entries_cached_under_any_key(tmp_path):
    bad = tmp_path / "http" / "ab" / "ab_old_key.json"
    good = tmp_path / "http" / "cd" / "cd_good.json"
    for p, body in ((bad, NCBI_BACKEND_ERROR), (good, {"esearchresult": {"count": "0", "idlist": []}})):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"url": f"{ncbi.EUTILS}/esearch.fcgi?db=pubmed&term=x", "status": 200, "text": json.dumps(body),
                                 "fetched_at": "2026-01-01T00:00:00Z", "stored": 1e12}))
    HttpClient._swept.discard(str(tmp_path))
    http = HttpClient(get_settings(cache_dir=tmp_path), transport=httpx.MockTransport(base_handler))
    assert not bad.exists() and good.exists() and http.cache_evictions == 1


def test_gene_alias_anchors_drop_short_and_family_prefix_aliases():
    gene = lambda r: _q(r).get("db") == "gene" and r.url.path.endswith("esummary.fcgi")
    http, _ = _client([(gene, [httpx.Response(200, json={"result": {"uids": ["9999"], "9999": {
        "name": "GENEX", "otheraliases": "GX4, GXR, GXR1, GENEX-AS, GXPROT2, DIS17"}}})])])
    assert ncbi.gene_aliases(http, "GENEX") == ["GENEX", "GXPROT2", "DIS17"]  # GX4 too short, GXR/GXR1 short, GENEX-AS no digit


# ------------------------------------------------------------------ 3. malformed payloads
@pytest.mark.parametrize("resp", [
    httpx.Response(200, text="<html><body>Service temporarily unavailable</body></html>"),
    httpx.Response(200, json={"header": {"type": "esearch"}}),                                  # no esearchresult
    httpx.Response(200, json={"esearchresult": {"count": "n/a", "idlist": []}}),                 # count not numeric
    httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}}),                              # count missing
    httpx.Response(200, json={"esearchresult": {"count": "12", "idlist": []}}),                  # count > 0 but no ids
    httpx.Response(200, json={"error": "API rate limit exceeded"}),                              # top-level error
])
def test_malformed_or_incomplete_esearch_payload_is_failure(resp):
    http, _ = _client([(is_esearch(), [resp])])
    L = EvidenceLedger()
    with pytest.raises(ResponseValidationError):
        ncbi.pubmed_search(http, L, "GENEX[tiab]")
    assert L.retrieval_status()["pubmed_search"]["failed"] == 1


def test_malformed_esummary_makes_the_search_fail_not_succeed_without_titles():
    http, _ = _client([(is_esearch(), [_esearch(["11111111"])]),
                       (lambda r: r.url.path.endswith("esummary.fcgi"), [httpx.Response(200, json={"header": {}})])])
    L = EvidenceLedger()
    with pytest.raises(ResponseValidationError):
        ncbi.pubmed_search(http, L, "GENEX[tiab]")
    assert L.queries[-1].error


# ------------------------------------------------------------------ 4. temporary server errors
def test_temporary_502_is_retried_then_succeeds():
    http, h = _client([(is_esearch(), [httpx.Response(502, text="Bad Gateway"), _esearch(["11111111"])])])
    r = ncbi.pubmed_search(http, EvidenceLedger(), "GENEX[tiab]")
    assert r["total_count"] == 1 and sum("esearch.fcgi" in c for c in h.calls) == 2


def test_persistent_502_is_a_logged_failure():
    http, _ = _client([(is_esearch(), [httpx.Response(502, text="Bad Gateway")])])
    L = EvidenceLedger()
    with pytest.raises(Exception):
        ncbi.pubmed_search(http, L, "GENEX[tiab]")
    assert L.queries[-1].error.startswith("RetryableHTTPError") and L.retrieval_status()["pubmed_search"]["empty"] == 0


# ------------------------------------------------------------------ 5. genuine empty search stays empty
def test_successful_empty_search_is_recorded_as_genuine_zero(tmp_path):
    warn = {"quotedphrasesnotfound": ['"p.Asp34Asn"[tiab]'], "phrasesignored": []}
    http, _ = _client([(is_esearch(), [_esearch([], 0, warninglist=warn)])], tmp_path)
    L = EvidenceLedger()
    r = ncbi.pubmed_search(http, L, 'GENEX[tiab] AND "p.Asp34Asn"[tiab]')
    assert r["total_count"] == 0 and r["status"] == "empty" and r["warnings"]["quotedphrasesnotfound"]
    assert L.retrieval_status()["pubmed_search"] == {"with_results": 0, "empty": 1, "failed": 0, "truncated": 0, "partial": 0}
    assert any("esearch.fcgi" in json.loads(p.read_text())["url"] for p in (tmp_path / "http").rglob("*.json"))  # valid zero is cacheable


# ------------------------------------------------------------------ 6. coverage: failure, pagination, truncation, partial
def _paged(total: int, fail_page: int | None = None):
    def resp(request):
        q = _q(request)
        start, n = int(q.get("retstart", 0)), int(q["retmax"])
        if fail_page is not None and start // n + 1 == fail_page:
            return httpx.Response(200, json=NCBI_BACKEND_ERROR)
        ids = [str(10_000_000 + i) for i in range(start, min(total, start + n))]
        return _esearch(ids, total)
    return resp


def _esummary_any(request):
    return _esummary([i for i in _q(request)["id"].split(",") if i])


def test_coverage_family_failure_is_a_gap_and_other_families_paginate_completely():
    rules = [(is_esearch("treat*"), [httpx.Response(200, json=NCBI_BACKEND_ERROR)]),
             (is_esearch("inhibitor*"), [_paged(59)]), (is_esearch(), [_paged(3)]),
             (lambda r: r.url.path.endswith("esummary.fcgi"), [_esummary_any])]
    http, _ = _client(rules)
    L = EvidenceLedger()
    cov = ncbi.therapeutic_coverage_search(http, L, "GENEX")
    hum, inh = cov["human_treatment_reports"], cov["inhibiting_modulators"]
    assert hum["status"] == "failed" and hum["total"] is None and "Search Backend failed" in hum["error"] and hum["keys"] == []
    assert inh["status"] == "complete" and inh["total"] == 59 and len(inh["keys"]) == 59 and inh["pages"] == 2 and not inh["truncated"]
    gaps = ncbi.coverage_failures({"GENEX": cov})
    assert [(g["family"], g["status"]) for g in gaps] == [("human_treatment_reports", "failed")]
    qs = [q for q in L.queries if q.args.get("coverage_family") == "inhibiting_modulators"]
    assert len(qs) == 1 and qs[0].n_results == 59 and qs[0].total_available == 59  # one entry; not truncated
    assert L.retrieval_status()["pubmed_search"]["failed"] == 1


def test_coverage_cap_marks_truncation_and_later_page_failure_marks_partial():
    rules = [(is_esearch("potentiat*"), [_paged(130)]), (is_esearch("antisense"), [_paged(80, fail_page=2)]), (is_esearch(), [_paged(0)]),
             (lambda r: r.url.path.endswith("esummary.fcgi"), [_esummary_any])]
    http, _ = _client(rules)
    L = EvidenceLedger()
    cov = ncbi.therapeutic_coverage_search(http, L, "GENEX", max_results=100)
    act, nuc = cov["activating_modulators"], cov["nucleic_acid_therapies"]
    assert act["status"] == "truncated" and act["reviewed"] == 100 and act["total"] == 130 and act["truncated"]
    assert nuc["status"] == "partial" and len(nuc["keys"]) == 50 and nuc["error"] and nuc["truncated"]
    assert cov["human_treatment_reports"]["status"] == "empty" and cov["human_treatment_reports"]["error"] == ""
    st = L.retrieval_status()["pubmed_search"]
    assert st["partial"] == 1 and st["truncated"] == 1 and st["empty"] == 2


def test_failed_family_becomes_explicit_follow_up_and_is_shown_first_to_the_model():
    from aitx.agents.interpreter import dossier_text
    from aitx.agents.researcher import coverage_gaps

    fam = {"query": "GENEX[tiab] AND (treat*[tiab])", "total": None, "keys": [], "titles": {}, "truncated": False, "status": "failed",
           "error": "ResponseValidationError: ESearch backend error"}
    d = {"therapeutic_coverage": {"GENEX": {"human_treatment_reports": fam}}}
    d["coverage_failures"] = ncbi.coverage_failures(d["therapeutic_coverage"])
    reqs, rows = coverage_gaps(d, [])
    assert rows[0]["status"] == "failed" and len(reqs) == 1 and "FAILED" in reqs[0] and "Re-run" in reqs[0]
    txt = dossier_text(d, limit=400)  # even a tiny limit keeps the failure block
    assert "RETRIEVAL FAILURES" in txt and "human_treatment_reports" in txt


# ------------------------------------------------------------------ 7. failures are visible in report wording
def _ledger_with_failed_coverage() -> EvidenceLedger:
    L = EvidenceLedger()
    L.log_query("pubmed_search", {"query": "GENEX[tiab] AND inhibitor*", "coverage_family": "inhibiting_modulators"}, ["PMID:1"], total=1)
    L.log_query("pubmed_search", {"query": "GENEX[tiab] AND treat*", "coverage_family": "human_treatment_reports"}, [],
                error="ResponseValidationError: ESearch backend error")
    return L


def test_negative_statement_names_a_failed_query_even_when_other_queries_of_that_source_succeeded():
    from aitx.render import _negative_statement
    from aitx.schemas import Section

    txt = _negative_statement(Section.OFF_LABEL, _ledger_with_failed_coverage(), {})
    assert "1 of the PubMed queries failed" in txt and "searches are incomplete" in txt
    assert "human treatment reports failed" in txt


def test_precedent_scope_discloses_failed_mandatory_human_report_search():
    from aitx.render import precedent_scope

    assert "human treatment reports failed in this run" in precedent_scope(_ledger_with_failed_coverage())
    L = _ledger_with_failed_coverage()
    L.log_query("pubmed_search", {"query": "GENEX[tiab] AND treat*", "coverage_family": "human_treatment_reports"}, ["PMID:2"], total=1)
    assert "human treatment reports failed" not in precedent_scope(L)  # a later successful re-run clears the gap


# ------------------------------------------------------------------ other E-utilities callers
def test_clinvar_search_failure_is_not_reported_as_no_record():
    http, _ = _client([(is_esearch(db="clinvar"), [httpx.Response(200, json=NCBI_BACKEND_ERROR)])])
    L = EvidenceLedger()
    with pytest.raises(ResponseValidationError):
        ncbi.clinvar_lookup(http, L, "GENEX", "c.100G>A", "NM_999999.1")
    assert L.queries[-1].tool == "clinvar_lookup" and L.queries[-1].error
    assert L.retrieval_status()["clinvar_lookup"]["empty"] == 0


def test_pmid_existence_check_failure_does_not_mark_citations_missing():
    http, _ = _client([(lambda r: r.url.path.endswith("esummary.fcgi"), [httpx.Response(200, json={"error": "backend unavailable"})])])
    with pytest.raises(ResponseValidationError):
        ncbi.pmid_exists(http, ["11111111"])


def test_gene_summary_and_abstract_fetch_failures_raise_and_are_logged():
    http, _ = _client([(is_esearch(db="gene"), [httpx.Response(200, json=NCBI_BACKEND_ERROR)]),
                       (lambda r: r.url.path.endswith("efetch.fcgi") and _q(r).get("db") == "pubmed",
                        [httpx.Response(200, text="<eFetchResult><ERROR>Unable to obtain query</ERROR></eFetchResult>")])])
    L = EvidenceLedger()
    with pytest.raises(ResponseValidationError):
        ncbi.ncbi_gene_summary(http, L, "GENEX")
    with pytest.raises(ResponseValidationError):
        ncbi.pubmed_fetch_abstracts(http, L, ["11111111"])
    assert {q.tool for q in L.failed_queries()} == {"ncbi_gene_summary", "pubmed_fetch_abstracts"}


def test_europepmc_malformed_response_is_logged_failure():
    http, _ = _client([(lambda r: r.url.host == "www.ebi.ac.uk" and r.url.path.endswith("/search"), [httpx.Response(200, json={"version": "6"})])])
    L = EvidenceLedger()
    out = europepmc.europepmc_search(http, L, '"GENEX"')
    assert "error" in out and L.queries[-1].error and L.retrieval_status()["europepmc_search"]["failed"] == 1


def test_gnomad_graphql_errors_with_http200_are_not_cached(tmp_path):
    from aitx.retrieval import variant

    http, _ = _client([(lambda r: r.url.host == "gnomad.broadinstitute.org", [httpx.Response(200, json={"errors": [{"message": "timeout"}]})])], tmp_path)
    with pytest.raises(ResponseValidationError):
        variant.gnomad_constraint(http, "GENEX")
    assert not (tmp_path / "http").exists() or not list((tmp_path / "http").rglob("*.json"))


# ------------------------------------------------------------------ 8. variant context (no unrelated "D34N" matches)
OTHER_PROTEIN_XML = """<?xml version="1.0"?><article><front><article-meta><article-id pub-id-type="pmid">33333333</article-id>
<title-group><article-title>Proteome of mouse heart chambers</article-title></title-group></article-meta></front><body>
<sec><title>Results</title><p>GENEX expression was higher in atria.</p><p>The OTHERP D34N mutant lost activity.</p></sec></body></article>"""
SAME_GENE_XML = """<?xml version="1.0"?><article><front><article-meta><article-id pub-id-type="pmid">44444444</article-id>
<title-group><article-title>GENEX variants in ataxia</article-title></title-group></article-meta></front><body>
<sec><title>Results</title><table-wrap><label>Table 2</label><caption><p>GENEX variants and phenotypes</p></caption><table>
<tr><th>Patient</th><th>Variant</th></tr><tr><td>P3</td><td>c.100G>A (p.Asp34Asn)</td></tr></table></table-wrap></sec></body></article>"""


def test_unrelated_notation_match_is_not_variant_evidence():
    epmc = {"hitCount": 2, "resultList": {"result": [
        {"id": "33333333", "source": "MED", "pmid": "33333333", "pmcid": "PMC3333333", "title": "Proteome of mouse heart chambers", "isOpenAccess": "Y"},
        {"id": "44444444", "source": "MED", "pmid": "44444444", "pmcid": "PMC4444444", "title": "GENEX variants in ataxia", "isOpenAccess": "Y"}]}}
    rules = [(lambda r: r.url.host == "www.ebi.ac.uk" and r.url.path.endswith("/search"), [httpx.Response(200, json=epmc)]),
             (lambda r: r.url.path.endswith("PMC3333333/fullTextXML"), [httpx.Response(200, text=OTHER_PROTEIN_XML)]),
             (lambda r: r.url.path.endswith("PMC4444444/fullTextXML"), [httpx.Response(200, text=SAME_GENE_XML)]),
             (is_esearch(), [_esearch([], 0)])]
    http, _ = _client(rules)
    L = EvidenceLedger()
    out = europepmc.variant_literature_search(http, L, "GENEX", ["D34N", "c.100G>A", "p.Asp34Asn"], gene_aliases=["GENEX"])
    assert [h["key"] for h in out["europepmc"]] == ["PMID:44444444"]
    assert [(h["key"], h["variant_context"]["status"]) for h in out["europepmc_unconfirmed"]] == [("PMID:33333333", "notation_without_gene_context")]
    assert L.get("PMID:33333333").metadata["variant_context"] == "notation_without_gene_context"
    assert out["status"] == "ok" and "PMID:33333333" not in L.searches_for("variant_literature_search")[-1].result_keys


def test_variant_literature_reports_partial_failure_instead_of_empty():
    rules = [(lambda r: r.url.host == "www.ebi.ac.uk" and r.url.path.endswith("/search"), [httpx.Response(200, json={"hitCount": 0, "resultList": {"result": []}})]),
             (is_esearch(), [httpx.Response(200, json=NCBI_BACKEND_ERROR)])]
    http, _ = _client(rules)
    L = EvidenceLedger()
    out = europepmc.variant_literature_search(http, L, "GENEX", ["D34N"], gene_aliases=["GENEX"])
    assert out["status"] == "partial" and "pubmed" in out["failures"]
    assert L.searches_for("variant_literature_search")[-1].error


# ------------------------------------------------------------------ 9. end to end: the failure reaches the audit and follow-up
def test_pipeline_surfaces_failed_mandatory_coverage(settings, test_case):
    from mock_handlers import HANDLERS

    from aitx.llm.mock import MockLLM
    from aitx.pipeline import Pipeline
    from aitx.schemas import SectionResearchOutput

    seen = []

    def followup(ctx):
        seen.append(ctx["system"])
        return SectionResearchOutput(items=[], candidate_handoffs=[], searched_but_not_found=["re-run failed"], open_questions=[])

    rules = [(is_esearch("treat*[tiab] OR therap*"), [httpx.Response(200, json=NCBI_BACKEND_ERROR)])]
    h = Scripted(rules)
    http = HttpClient(settings, transport=httpx.MockTransport(h))
    http.validation_backoff_s = 0.0
    res = Pipeline(MockLLM(HANDLERS | {"E_followup": followup}), settings, http=http, parallel=False).run(test_case)
    rows = {r["family"]: r for r in res.audit["stages"]["therapeutic_coverage"]}
    assert rows["human_treatment_reports"]["status"] == "failed"
    assert any(i["code"] == "coverage_query_failed" and "human_treatment_reports" in i["detail"] for i in res.audit["issues"])
    assert seen and "[coverage:GENEX:human_treatment_reports]" in seen[0] and "FAILED" in seen[0]


# ------------------------------------------------------------------ 10. Europe PMC upstream 500: actionable failure + short negative cache
# First real B_full run: the fullTextXML endpoint returned HTTP 500 for specific articles (upstream error, not a
# 404, not rate limiting); the model re-requested the same articles and burned retry time. The failure must stay a
# failure (never absence) but repeated requests must fail fast and tell the model what to do instead.
def _epmc500():
    return _client([(lambda r: r.url.path.endswith("fullTextXML"), [httpx.Response(500, json={"status": 500})])])


def test_europepmc_fulltext_500_is_actionable_and_negatively_cached():
    http, h = _epmc500()
    L = EvidenceLedger()
    europepmc._FULLTEXT_UPSTREAM_FAIL.clear()
    try:
        out1 = europepmc.europepmc_fulltext_excerpts(http, L, "PMC1234567", ["dose"])
        assert isinstance(out1, dict) and "upstream" in out1["error"] and "PMC1234567" in out1["error"]
        assert "retrieval failure" in out1["error"]  # never phrased as absence of content
        assert L.retrieval_status()["europepmc_fulltext_excerpts"]["failed"] == 1
        n = len(h.calls)
        out2 = europepmc.europepmc_fulltext_excerpts(http, L, "PMC1234567", ["dose"])
        assert "upstream" in out2["error"]
        assert len(h.calls) == n  # negative cache: no further HTTP attempt
        assert L.retrieval_status()["europepmc_fulltext_excerpts"]["failed"] == 2
    finally:
        europepmc._FULLTEXT_UPSTREAM_FAIL.clear()


def test_load_fulltext_500_marks_failed_and_uses_the_same_cache():
    http, h = _epmc500()
    L = EvidenceLedger()
    rec = EvidenceRecord("PMCID:PMC1234567", "europepmc", "title", "https://example.org/x", content="abstract")
    L.add(rec)
    europepmc._FULLTEXT_UPSTREAM_FAIL.clear()
    try:
        assert europepmc.load_fulltext(http, L, "PMCID:PMC1234567", "1234567") == "failed"
        assert rec.metadata["fulltext_status"] == "failed"
        n = len(h.calls)
        assert europepmc.load_fulltext(http, L, "PMCID:PMC1234567", "1234567") == "failed"
        assert len(h.calls) == n  # negative cache
    finally:
        europepmc._FULLTEXT_UPSTREAM_FAIL.clear()
