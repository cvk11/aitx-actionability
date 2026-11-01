"""ClinicalTrials.gov API v2 (live records; short cache TTL)."""

from __future__ import annotations

import re
from typing import Any

from ..http import HttpClient
from ..ledger import EvidenceLedger, EvidenceRecord

BASE = "https://clinicaltrials.gov/api/v2/studies"
OPEN_STATUSES = ("RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION")
EXPANDED_ACCESS_OPEN = ("AVAILABLE",)
FIELDS = ",".join([
    "NCTId", "BriefTitle", "OfficialTitle", "OverallStatus", "StatusVerifiedDate", "LastUpdatePostDate", "Phase", "StudyType",
    "Condition", "Keyword", "InterventionName", "InterventionType", "MinimumAge", "MaximumAge", "Sex", "HealthyVolunteers",
    "EligibilityCriteria", "LocationCountry", "LocationFacility", "LocationStatus", "BriefSummary", "EnrollmentCount",
    "LeadSponsorName", "HasExpandedAccess",
])


def _age_to_years(s: str | None) -> float | None:
    if not s:
        return None
    m = re.match(r"(\d+(?:\.\d+)?)\s*(Year|Month|Week|Day|Hour|Minute)s?", s, re.I)
    if not m:
        return None
    n = float(m.group(1))
    f = {"year": 1, "month": 1 / 12, "week": 1 / 52.18, "day": 1 / 365.25, "hour": 1 / 8766, "minute": 1 / 525960}[m.group(2).lower()]
    return round(n * f, 4)


def _flatten(study: dict[str, Any]) -> dict[str, Any]:
    ps = study.get("protocolSection", {})
    idm, st, de = ps.get("identificationModule", {}), ps.get("statusModule", {}), ps.get("designModule", {})
    el, cm, ai = ps.get("eligibilityModule", {}), ps.get("conditionsModule", {}), ps.get("armsInterventionsModule", {})
    cl, ds, sp = ps.get("contactsLocationsModule", {}), ps.get("descriptionModule", {}), ps.get("sponsorCollaboratorsModule", {})
    locs = cl.get("locations", []) or []
    countries = sorted({l.get("country") for l in locs if l.get("country")})
    return {
        "nct_id": idm.get("nctId"),
        "title": idm.get("briefTitle"),
        "official_title": idm.get("officialTitle", ""),
        "overall_status": st.get("overallStatus"),
        "status_verified": st.get("statusVerifiedDate"),
        "last_update": (st.get("lastUpdatePostDateStruct") or {}).get("date"),
        "has_expanded_access": (st.get("expandedAccessInfo") or {}).get("hasExpandedAccess"),
        "study_type": de.get("studyType"),
        "phases": de.get("phases", []),
        "conditions": cm.get("conditions", []),
        "keywords": cm.get("keywords", []),
        "interventions": [f"{i.get('type', '')}: {i.get('name', '')}" for i in ai.get("interventions", []) or []],
        "min_age": el.get("minimumAge"),
        "max_age": el.get("maximumAge"),
        "min_age_years": _age_to_years(el.get("minimumAge")),
        "max_age_years": _age_to_years(el.get("maximumAge")),
        "sex": el.get("sex"),
        "eligibility_criteria": el.get("eligibilityCriteria", ""),
        "countries": countries,
        "n_sites": len(locs),
        "recruiting_sites": sum(1 for l in locs if l.get("status") == "RECRUITING"),
        "brief_summary": ds.get("briefSummary", ""),
        "sponsor": (sp.get("leadSponsor") or {}).get("name", ""),
        "enrollment": (de.get("enrollmentInfo") or {}).get("count"),
    }


def _register(ledger: EvidenceLedger, f: dict[str, Any], fetched_at: str, tool: str) -> None:
    content = (
        f"{f['title']}. Status: {f['overall_status']} (verified {f['status_verified']}; last update {f['last_update']}). "
        f"Type: {f['study_type']} {', '.join(f['phases'] or [])}. Conditions: {', '.join(f['conditions'])}. "
        f"Interventions: {'; '.join(f['interventions'])}. Ages: {f['min_age']} to {f['max_age']}; sex: {f['sex']}. "
        f"Countries: {', '.join(f['countries'])}. Sponsor: {f['sponsor']}.\nSummary: {f['brief_summary'][:1200]}\n"
        f"Eligibility:\n{f['eligibility_criteria'][:5000]}"
    )
    rec = ledger.add(EvidenceRecord(
        key=f["nct_id"], source_type="clinicaltrials", title=f["title"] or f["nct_id"],
        url=f"https://clinicaltrials.gov/study/{f['nct_id']}", content=content, content_scope="structured_record",
        retrieved_by=tool, accessed_at=fetched_at,
        metadata={k: f[k] for k in ("overall_status", "status_verified", "last_update", "min_age_years", "max_age_years", "sex",
                                     "study_type", "phases", "countries", "conditions", "interventions", "has_expanded_access")}
        | {"status": f["overall_status"]},
    ))
    rec.verified_exists = True


def search_trials(
    http: HttpClient, ledger: EvidenceLedger, condition: str = "", term: str = "", intervention: str = "",
    open_only: bool = True, page_size: int = 25,
) -> dict[str, Any]:
    params: dict[str, Any] = {"fields": FIELDS, "pageSize": max(1, min(int(page_size), 50)), "countTotal": "true", "format": "json"}
    if condition:
        params["query.cond"] = condition
    if term:
        params["query.term"] = term
    if intervention:
        params["query.intr"] = intervention
    if open_only:
        params["filter.overallStatus"] = ",".join(OPEN_STATUSES + EXPANDED_ACCESS_OPEN)
    r = http.request("GET", BASE, params=params)
    if r.status != 200:
        ledger.log_query("search_trials", {"condition": condition, "term": term, "intervention": intervention}, [], error=f"HTTP {r.status}")
        return {"error": f"HTTP {r.status}", "studies": []}
    d = r.json()
    out = []
    for s in d.get("studies", []):
        f = _flatten(s)
        _register(ledger, f, r.fetched_at, "search_trials")
        row = {k: f[k] for k in ("nct_id", "title", "overall_status", "study_type", "phases", "min_age", "max_age")}
        row["conditions"] = f["conditions"][:6] + ([f"... +{len(f['conditions']) - 6} more"] if len(f["conditions"]) > 6 else [])
        row["interventions"] = f["interventions"][:5]
        row["countries"] = f["countries"][:8] + (["..."] if len(f["countries"]) > 8 else [])
        out.append(row)
    ledger.log_query("search_trials", {"condition": condition, "term": term, "intervention": intervention, "open_only": open_only},
                     [o["nct_id"] for o in out])
    return {"total_count": d.get("totalCount"), "studies": out, "accessed_at": r.fetched_at,
            "note": "Use get_trial for full eligibility criteria before asserting eligibility."}


def get_trial(http: HttpClient, ledger: EvidenceLedger, nct_id: str) -> dict[str, Any]:
    nct = nct_id.strip().upper()
    if not re.fullmatch(r"NCT\d{8}", nct):
        return {"error": f"invalid NCT id {nct_id!r}"}
    r = http.request("GET", f"{BASE}/{nct}", params={"format": "json"}, use_cache=False)
    if r.status == 404:
        ledger.log_query("get_trial", {"nct_id": nct}, [], error="not found")
        return {"error": f"{nct} not found in ClinicalTrials.gov"}
    if r.status != 200:
        return {"error": f"HTTP {r.status}"}
    f = _flatten(r.json())
    _register(ledger, f, r.fetched_at, "get_trial")
    ledger.log_query("get_trial", {"nct_id": nct}, [nct])
    return f | {"accessed_at": r.fetched_at}


def trial_status(http: HttpClient, nct_id: str) -> dict[str, Any]:
    """Live status check used by the deterministic verifier (bypasses cache)."""
    r = http.request("GET", f"{BASE}/{nct_id}", params={"format": "json", "fields": FIELDS}, use_cache=False)
    if r.status != 200:
        return {"exists": False, "status": None, "http": r.status}
    f = _flatten(r.json())
    return {"exists": True, "status": f["overall_status"], "min_age_years": f["min_age_years"], "max_age_years": f["max_age_years"],
            "sex": f["sex"], "checked_at": r.fetched_at, "title": f["title"]}
