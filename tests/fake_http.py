"""Offline fake for every external biomedical API (httpx.MockTransport).

Uses a fictional gene GENEX on a fictional transcript NM_999999.1, so tests never
depend on (or leak answers for) the official challenge cases.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx

PUBMED_XML = """<?xml version="1.0"?><PubmedArticleSet>
<PubmedArticle><MedlineCitation><PMID>11111111</PMID><Article><Journal><JournalIssue><PubDate><Year>2022</Year></PubDate></JournalIssue><Title>J Test Neurol</Title></Journal>
<ArticleTitle>Drug Q treatment in a child with GENEX-related epilepsy: a case report.</ArticleTitle>
<Abstract><AbstractText Label="CASE">A 6-year-old girl with a de novo GENEX missense variant and refractory seizures was treated with drug Q; seizure frequency decreased by 50% over 12 months.</AbstractText></Abstract>
<PublicationTypeList><PublicationType>Case Reports</PublicationType></PublicationTypeList></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">11111111</ArticleId><ArticleId IdType="pmc">PMC1111111</ArticleId></ArticleIdList></PubmedData></PubmedArticle>
<PubmedArticle><MedlineCitation><PMID>22222222</PMID><Article><Journal><JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue><Title>Exp Neurobiol</Title></Journal>
<ArticleTitle>Compound R rescues synaptic defects in Genex knockout mice.</ArticleTitle>
<Abstract><AbstractText>In Genex-deficient mice, compound R restored synaptic vesicle release in hippocampal slices.</AbstractText></Abstract>
<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">22222222</ArticleId></ArticleIdList></PubmedData></PubmedArticle>
</PubmedArticleSet>"""

FEATURE_TABLE = """>Feature ref|NM_999999.1|
1\t900\tgene
\t\t\tgene\tGENEX
1\t150\texon
51\t653\tCDS
\t\t\tproduct\tgenex protein
151\t300\texon
301\t480\texon
481\t900\texon
"""

STUDY_OPEN = {"protocolSection": {
    "identificationModule": {"nctId": "NCT00000001", "briefTitle": "Drug Q in Genetic Epilepsies"},
    "statusModule": {"overallStatus": "RECRUITING", "statusVerifiedDate": "2026-09", "lastUpdatePostDateStruct": {"date": "2026-09-30"}},
    "designModule": {"studyType": "INTERVENTIONAL", "phases": ["PHASE2"]},
    "conditionsModule": {"conditions": ["Developmental and Epileptic Encephalopathy"]},
    "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Drug Q"}]},
    "eligibilityModule": {"minimumAge": "2 Years", "maximumAge": "17 Years", "sex": "ALL", "eligibilityCriteria": "Inclusion: genetic DEE; >=4 seizures/month. Exclusion: prior drug Q."},
    "contactsLocationsModule": {"locations": [{"country": "United States", "status": "RECRUITING"}]},
}}
STUDY_CLOSED = {"protocolSection": {
    "identificationModule": {"nctId": "NCT00000002", "briefTitle": "Old completed study"},
    "statusModule": {"overallStatus": "COMPLETED"},
    "designModule": {"studyType": "INTERVENTIONAL"},
    "eligibilityModule": {"minimumAge": "18 Years", "sex": "ALL"},
}}


def _j(obj, status=200):
    return httpx.Response(status, json=obj)


def handler(request: httpx.Request) -> httpx.Response:
    u = urlparse(str(request.url))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    host, path = u.netloc, u.path
    if host == "eutils.ncbi.nlm.nih.gov":
        db = q.get("db")
        if path.endswith("esearch.fcgi"):
            ids = {"pubmed": ["11111111", "22222222"], "clinvar": ["123"], "gene": ["9999"]}.get(db, [])
            n = int(q.get("retmax", 20))  # honour retmax like the real API
            return _j({"esearchresult": {"count": str(len(ids)), "idlist": ids[:n], "querytranslation": q.get("term", "")}})
        if path.endswith("esummary.fcgi"):
            ids = q.get("id", "").split(",")
            if db == "pubmed":
                res = {"uids": [i for i in ids if i in ("11111111", "22222222")]}
                for i in res["uids"]:
                    res[i] = {"title": "t" + i, "pubdate": "2022", "source": "J", "pubtype": ["Case Reports"] if i == "11111111" else ["Journal Article"]}
                for i in ids:
                    if i not in res["uids"]:
                        res[i] = {"error": "cannot get document summary"}
                return _j({"result": res})
            if db == "clinvar":
                return _j({"result": {"uids": ["123"], "123": {"accession": "VCV000000123", "title": "NM_999999.1(GENEX):c.100G>A (p.Asp34Asn)",
                                                              "germline_classification": {"description": "Pathogenic", "review_status": "criteria provided, single submitter",
                                                                                          "last_evaluated": "2024/01/01", "trait_set": [{"trait_name": "GENEX-related epilepsy"}]},
                                                              "molecular_consequence_list": ["missense variant"], "protein_change": "D34N",
                                                              "variation_set": [{"canonical_spdi": "NC_000001.11:999:G:A",
                                                                                 "variation_loc": [{"assembly_name": "GRCh38", "chr": "1", "start": "1000", "stop": "1000"}]}]}}})
            if db == "gene":
                return _j({"result": {"uids": ["9999"], "9999": {"name": "GENEX", "description": "genex protein", "summary": "GENEX encodes a synaptic protein."}}})
        if path.endswith("efetch.fcgi"):
            if db == "pubmed":
                return httpx.Response(200, text=PUBMED_XML)
            if db == "nuccore":
                return httpx.Response(200, text=FEATURE_TABLE)
    if host == "rest.variantvalidator.org":
        return _j({"flag": "gene_variant", "NM_999999.1:c.100G>A": {"hgvs_transcript_variant": "NM_999999.1:c.100G>A",
                   "hgvs_predicted_protein_consequence": {"tlr": "NP_999999.1:p.(Asp34Asn)"}, "gene_symbol": "GENEX",
                   "primary_assembly_loci": {"grch38": {"hgvs_genomic_description": "NC_000001.11:g.1000G>A",
                                                         "vcf": {"chr": "1", "pos": "1000", "ref": "G", "alt": "A"}}}, "validation_warnings": []}, "metadata": {}})
    if host == "rest.ensembl.org":
        return _j([{"most_severe_consequence": "missense_variant", "seq_region_name": "1", "start": 1000, "end": 1000,
                    "transcript_consequences": [{"transcript_id": "NM_999999.1", "consequence_terms": ["missense_variant"], "exon": "2/4", "protein_start": 34,
                                                 "hgvsp": "NP_999999.1:p.Asp34Asn", "polyphen_prediction": "probably_damaging"}]}])
    if host == "clinicaltrials.gov":
        if path.endswith("/NCT00000001"):
            return _j(STUDY_OPEN)
        if path.endswith("/NCT00000002"):
            return _j(STUDY_CLOSED)
        if path.endswith("/studies"):
            return _j({"totalCount": 2, "studies": [STUDY_OPEN, STUDY_CLOSED]})
        return httpx.Response(404, text="not found")
    if host == "search.clinicalgenome.org":
        if "validity" in path:
            return _j({"rows": [{"symbol": "GENEX", "hgnc_id": "HGNC:99999", "disease_name": "GENEX-related epilepsy", "moi": "AD", "classification": "Definitive"}]})
        return _j({"rows": [{"symbol": "GENEX", "hgnc_id": "HGNC:99999", "haplo_assertion": 30, "triplo_assertion": 0}]})
    if host == "gnomad.broadinstitute.org":
        return _j({"data": {"gene": {"gene_id": "ENSG0000099999", "gnomad_constraint": {"pLI": 0.99, "oe_lof": 0.1, "oe_lof_upper": 0.2, "oe_mis": 0.5, "mis_z": 4.0}}}})
    if host == "rest.uniprot.org":
        return _j({"results": [{"primaryAccession": "Q00000", "proteinDescription": {"recommendedName": {"fullName": {"value": "Genex protein"}}},
                                "sequence": {"length": 200}, "comments": [{"commentType": "FUNCTION", "texts": [{"value": "Synaptic vesicle fusion."}]}],
                                "features": [{"type": "Domain", "description": "C2", "location": {"start": {"value": 20}, "end": {"value": 120}}}]}]})
    if host == "api.fda.gov":
        return _j({"results": [{"set_id": "set-q", "effective_time": "20250101", "openfda": {"generic_name": ["DRUG Q"], "brand_name": ["Qbrand"]},
                                "indications_and_usage": ["Drug Q is indicated for seizures."], "contraindications": ["Hypersensitivity."]}]})
    if host == "www.ebi.ac.uk":
        return _j({"hitCount": 1, "resultList": {"result": [{"id": "11111111", "source": "MED", "pmid": "11111111", "pmcid": "PMC1111111", "title": "Drug Q case", "pubYear": "2022", "isOpenAccess": "Y"}]}})
    if host == "api.clinpgx.org":
        return _j({"data": []})
    return httpx.Response(404, text=f"no fake for {request.url}")


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(handler)
