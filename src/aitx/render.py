"""Deterministic Markdown renderer for the official template.

Headings, order, separators and citation links are produced by code; the
item text comes from verified pipeline items. Citation URLs come exclusively
from the evidence ledger.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from .ledger import KEY_RE, EvidenceLedger, normalize_key
from .rules import item_citations
from .schemas import ActionItem, Section, Synthesis
from .template import SECTION_HEADING, title_for

EVIDENCE_LABEL = {
    "regulatory_approval": "regulatory approval / drug label",
    "guideline_or_expert_review": "guideline or expert review",
    "human_same_variant": "human data, same variant",
    "human_same_gene": "human data, same gene",
    "human_related_disease_or_mechanism": "human data, related disease/mechanism",
    "preclinical_same_gene": "preclinical data, same gene",
    "preclinical_related": "preclinical data, related pathway",
    "mechanistic_inference": "mechanistic inference",
    "trial_registry_record": "trial registry record",
    "none": "reasoning only",
}

TIER_SECTIONS = {
    1: (Section.EXISTING_TARGETED, Section.SUPPORTIVE, Section.CONTRAINDICATIONS),
    2: (Section.TRIALS, Section.OFF_LABEL),
    3: (Section.BESPOKE, Section.REPURPOSING),
}
TIER_TITLES = {1: "Tier 1: Established Actions", 2: "Tier 2: Emerging Actions", 3: "Tier 3: Hypothesized Actions"}

SOURCE_NAMES = {
    "search_trials": "ClinicalTrials.gov", "get_trial": "ClinicalTrials.gov", "pubmed_search": "PubMed", "europepmc_search": "Europe PMC",
    "fda_label_search": "FDA drug labels (openFDA/DailyMed)", "genereviews_find": "GeneReviews", "genereviews_read": "GeneReviews",
    "clinvar_lookup": "ClinVar",
}


_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"(?<![(\[])\bhttps?://[^\s)\]]+")


def sanitize_model_text(text: str, ledger: EvidenceLedger) -> str:
    """Remove links/URLs written by a model unless they point to a ledger record (keeps the link label only)."""
    known = {r.url for r in ledger.records()}

    def md(m: re.Match) -> str:
        return m.group(0) if m.group(2) in known else m.group(1)

    text = _MD_LINK.sub(md, text)
    return _BARE_URL.sub(lambda m: m.group(0) if m.group(0) in known else "", text)


def link_citations(text: str, ledger: EvidenceLedger) -> str:
    text = sanitize_model_text(text, ledger)

    def sub(m: re.Match) -> str:
        r = ledger.get(normalize_key(m.group(1)))
        if r is None:
            return ""
        return f"[{r.citation_label()}]({r.url})"

    out = KEY_RE.sub(sub, text)
    out = re.sub(r"\]\(([^)]+)\)\s*\[", r"](\1); [", out)  # separate adjacent links
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _sources_line(item: ActionItem, ledger: EvidenceLedger) -> str:
    inline = set(re.findall(KEY_RE, item.statement + item.human_precedent))
    inline = {normalize_key(k) for k in inline}
    extra = [k for k in item_citations(item) if k not in inline and ledger.get(k)]
    if not extra:
        return ""
    links = "; ".join(f"[{ledger.get(k).citation_label()}]({ledger.get(k).url})" for k in extra[:6])
    return f" Source: {links}."


def _ensure_period(s: str) -> str:
    s = s.strip()
    return s if not s or s[-1] in ".!?)" else s + "."


STATUS_NOTE = {
    "hypothesis": " *Hypothesis: mechanistic inference built on the verified facts cited; not an established benefit.*",
    "composed_from_verified_claims": " *Statement composed only from individually verified claims.*",
}


def render_item(item: ActionItem, ledger: EvidenceLedger, accessed: dict[str, str], status: str = "") -> str:
    return _render_item(item, ledger, accessed) + STATUS_NOTE.get(status, "")


def _render_item(item: ActionItem, ledger: EvidenceLedger, accessed: dict[str, str]) -> str:
    stmt = _ensure_period(link_citations(item.statement, ledger))
    cond = f" {_ensure_period(link_citations(item.conditional_action, ledger))}" if item.conditional_action.strip() else ""
    ev = f" *Evidence level: {EVIDENCE_LABEL.get(item.evidence_level, item.evidence_level)}.*"
    src = _sources_line(item, ledger)
    title = item.title.strip().rstrip(":")
    if item.section == Section.TRIALS:
        from .retrieval.clinicaltrials import STATUS_LABEL, status_class

        rec = ledger.get(item.nct_id)
        status = (rec.metadata.get("overall_status") if rec else "") or "unknown"
        date = (rec.accessed_at[:10] if rec else accessed.get("trials", ""))
        label = STATUS_LABEL.get(status_class(status), status.replace("_", " ").lower())
        verification = (rec.metadata.get("status_verification") if rec else "") or ""
        check = (f"live status re-checked {rec.metadata.get('status_checked_at', '')[:10]}" if verification == "live_ok"
                 else "LIVE RE-CHECK FAILED — current status unverified" if verification == "failed"
                 else f"ClinicalTrials.gov record retrieved {date}")
        if rec is not None:
            head = f"- **[{item.nct_id}: {sanitize_model_text(rec.title, ledger)}]({rec.url})**:"
        else:  # never link a registry record that was not retrieved in this run
            head = f"- **{item.nct_id}: {sanitize_model_text(title, ledger)}** (registry record not retrieved in this run):"
            check = "not verified"
        parts = [f" Status: {label} ({check}).", f" {stmt}"]
        if item.criteria_met:
            parts.append(" Criteria met on available data: " + "; ".join(item.criteria_met) + ".")
        if item.criteria_unassessable:
            parts.append(" Cannot be assessed from the case: " + "; ".join(item.criteria_unassessable) + ".")
        if item.potential_exclusions:
            parts.append(" Potential exclusions: " + "; ".join(item.potential_exclusions) + ".")
        return head + "".join(parts) + cond + ev + src
    if item.section == Section.BESPOKE:
        verdict = item.bespoke_verdict or "UNCERTAIN"
        return f"- **{title}:** **Verdict:** {verdict}. {stmt}{cond}{ev}{src}"
    if item.section == Section.OFF_LABEL:
        prec = item.human_precedent.strip()
        prec_txt = f" Human treatment precedent: {_ensure_period(link_citations(prec, ledger))}" if prec else ""
        return f"- **{title}:** {stmt}{prec_txt}{cond}{ev}{src}"
    if item.section == Section.REPURPOSING:
        tail = "" if re.search(r"human (treatment )?precedent", stmt, re.I) else " " + precedent_scope(ledger, item)
        return f"- **{title}:** {stmt}{tail}{cond}{ev}{src}"
    return f"- **{title}:** {stmt}{cond}{ev}{src}"


def precedent_scope(ledger: EvidenceLedger, item: ActionItem | None = None) -> str:
    """Search-scoped, dated statement about human treatment precedent. Never asserts biological absence.
    With `item`, only literature queries that name this candidate's intervention count: a gene-level search that
    never mentioned the compound is not evidence that the compound lacks human precedent."""
    qs = ledger.searches_for("pubmed_search", "europepmc_search")
    if item is not None:
        from .rules import distinctive_terms

        terms = distinctive_terms(item.title)
        qs = [q for q in qs if any(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", json.dumps(q.args, default=str).lower()) for t in terms)]
        if not qs:
            return "Human use of this candidate was not specifically searched in this run, so human treatment precedent was not assessed."
    ok = [q for q in qs if not q.error]
    cov_fail = human_report_coverage_gap(ledger)
    if not qs:
        return "Human treatment precedent was not specifically searched in this run." + cov_fail
    if not ok:
        return "Human treatment precedent could not be assessed because literature retrieval failed in this run." + cov_fail
    srcs = ", ".join(sorted({SOURCE_NAMES.get(q.tool, q.tool) for q in ok}))
    date = max(q.timestamp[:10] for q in ok)
    trunc = [q for q in ok if q.total_available is not None and q.total_available > q.n_results]
    cover = (f"; {len(trunc)} of {len(ok)} queries returned more hits than were reviewed (only the first "
             f"{min(q.n_results for q in trunc)}-{max(q.n_results for q in trunc)} ranked results per query)" if trunc else "")
    failed = f" {len(qs) - len(ok)} of {len(qs)} literature queries failed, so this search is incomplete." if len(ok) < len(qs) else ""
    return (f"No human treatment precedent in this gene/disease/variant context was identified in the literature searched in this run "
            f"({srcs}, accessed {date}{cover}); this does not establish that none exists.{failed}{cov_fail}")


def human_report_coverage_gap(ledger: EvidenceLedger) -> str:
    """Sentence stating that the mandatory gene-level search for human treatment reports failed or was incomplete
    (and was not successfully repeated later in the run); '' otherwise."""
    qs = [q for q in ledger.searches_for("pubmed_search") if q.args.get("coverage_family") == "human_treatment_reports"]
    if not qs:
        return ""
    if any(not q.error for q in qs):  # a complete run of the query exists
        return ""
    return (" The mandatory gene-level literature search for human treatment reports failed in this run, so published human "
            "treatment reports may have been missed.")


def _negative_statement(section: Section, ledger: EvidenceLedger, negatives: dict[Section, list[str]]) -> str:
    tools = {
        Section.EXISTING_TARGETED: ("fda_label_search", "genereviews_read", "pubmed_search"),
        Section.SUPPORTIVE: ("genereviews_read", "pubmed_search"),
        Section.CONTRAINDICATIONS: ("genereviews_read", "fda_label_search"),
        Section.TRIALS: ("search_trials",),
        Section.OFF_LABEL: ("pubmed_search", "europepmc_search"),
        Section.BESPOKE: ("pubmed_search",),
        Section.REPURPOSING: ("pubmed_search", "europepmc_search"),
    }[section]
    qs = ledger.searches_for(*tools)
    ok_q = [q for q in qs if not q.error]
    # every source with ANY failed query is named (a failed query is not offset by other successful queries of that source)
    failed = sorted({SOURCE_NAMES.get(q.tool, q.tool) for q in qs if q.error})
    fully_failed = sorted(set(failed) - {SOURCE_NAMES.get(q.tool, q.tool) for q in ok_q})
    srcs = sorted({SOURCE_NAMES.get(q.tool, q.tool) for q in ok_q})
    date = max((q.timestamp[:10] for q in qs), default="")
    base = {
        Section.EXISTING_TARGETED: "No approved or guideline-supported therapy directed at the underlying molecular defect was identified in the searched sources.",
        Section.SUPPORTIVE: "No disease-specific supportive-management recommendation could be verified in the searched sources.",
        Section.CONTRAINDICATIONS: "No variant- or disease-specific contraindication could be verified beyond standard prescribing information.",
        Section.TRIALS: "No actively recruiting clinical trial relevant to this patient was identified in ClinicalTrials.gov.",
        Section.OFF_LABEL: "No therapy with published human treatment precedent in this gene, disease, or variant context was identified in the searched literature.",
        Section.BESPOKE: "No DNA- or RNA-directed strategy could be assessed with sufficient evidence in this run.",
        Section.REPURPOSING: "No repurposing candidate with an adequate mechanistic rationale was identified.",
    }[section]
    trunc = any(q.total_available is not None and q.total_available > q.n_results for q in ok_q)
    searched = (f" Searched: {', '.join(srcs)} (accessed {date})" + ("; at least one search reviewed only the first page of a larger result set" if trunc else "")
                + "; absence from these sources does not establish that none exists." if srcs else "")
    fail = ""
    if fully_failed:
        fail = f" Retrieval from {', '.join(fully_failed)} failed during this run, so no conclusion can be drawn from that source."
    partly = sorted(set(failed) - set(fully_failed))
    if partly:
        n = {src: sum(1 for q in qs if q.error and SOURCE_NAMES.get(q.tool, q.tool) == src) for src in partly}
        fail += " " + "; ".join(f"{k} of the {src} queries failed" for src, k in n.items()) + ", so these searches are incomplete."
    if section in (Section.OFF_LABEL, Section.REPURPOSING, Section.BESPOKE):
        fail += human_report_coverage_gap(ledger)
    if not srcs and not failed:
        searched = " No dedicated search was recorded for this section in this run."
    # free-text negative notes from the researchers are unverified model text: kept in the audit, never rendered
    extra = ""
    return f"- {base}{extra}{searched}{fail}"


def render_report(case_id: str, items: list[ActionItem], synthesis: Synthesis, ledger: EvidenceLedger,
                  negatives: dict[Section, list[str]] | None = None, evidence_date: str = "", statement_status: dict[str, str] | None = None) -> str:
    statement_status = statement_status or {}
    negatives = negatives or {}
    by_sec: dict[Section, list[ActionItem]] = defaultdict(list)
    for it in items:
        by_sec[it.section].append(it)
    for v in by_sec.values():
        v.sort(key=lambda x: (x.priority, x.item_id))
    accessed = {"trials": max((q.timestamp[:10] for q in ledger.searches_for("search_trials", "get_trial")), default="")}
    L: list[str] = [title_for(case_id), "", "## Summary", ""]
    lines = {s.tier: s for s in synthesis.summary_lines}
    for tier in (1, 2, 3):
        s = lines.get(tier)
        txt = link_citations(s.text, ledger) if s else "No qualifying action identified at this tier."
        L.append(f"- **Tier {tier}:** {_ensure_period(txt)}")
    L.append("")
    if synthesis.missing_information:
        if len(synthesis.missing_information) == 1:
            L.append(f"**Missing information:** {_ensure_period(link_citations(synthesis.missing_information[0], ledger))}")
        else:
            L.append("**Missing information:**")
            L.append("")
            L += [f"- {_ensure_period(link_citations(m, ledger))}" for m in synthesis.missing_information]
        L.append("")
    if evidence_date:
        L.append(f"*Evidence retrieved {evidence_date} from live public records (PubMed, ClinicalTrials.gov, ClinVar, FDA labels, GeneReviews, ClinGen, gnomAD, UniProt). "
                 f"This report concerns a hypothetical patient and requires review by the treating team.*")
        L.append("")
    for tier in (1, 2, 3):
        L += ["---", "", f"## {TIER_TITLES[tier]}", ""]
        for sec in TIER_SECTIONS[tier]:
            L += [f"### {SECTION_HEADING[sec]}", ""]
            its = by_sec.get(sec, [])
            if its:
                L += [render_item(it, ledger, accessed, statement_status.get(it.item_id, "")) for it in its]
            else:
                L.append(_negative_statement(sec, ledger, negatives))
            L.append("")
        if tier == 3:
            L += ["### Outcome Measures & Ranking", ""]
            if synthesis.outcome_measures:
                for om in sorted(synthesis.outcome_measures, key=lambda o: o.rank):
                    src = "; ".join(f"[{ledger.get(k).citation_label()}]({ledger.get(k).url})" for k in om.citations if ledger.get(k))
                    L.append(f"- **{om.rank}. {om.action_name.strip()}:** {_ensure_period(link_citations(om.rank_rationale, ledger))}")
                    L.append(f"  - **Safety metrics:** {_ensure_period(link_citations(om.safety_metrics, ledger))}")
                    L.append(f"  - **Efficacy metrics:** {_ensure_period(link_citations(om.efficacy_metrics, ledger))}")
                    L.append(f"  - **Preclinical testing:** {_ensure_period(link_citations(om.preclinical_testing, ledger))}" + (f" Source: {src}." if src else ""))
            elif by_sec.get(Section.BESPOKE) or by_sec.get(Section.REPURPOSING):
                # never contradict the Tier-3 items above: rank them deterministically by verified evidence level
                t3 = [i for i in by_sec.get(Section.BESPOKE, []) + by_sec.get(Section.REPURPOSING, []) if i.bespoke_verdict != "NOT AMENABLE"]
                order = ["regulatory_approval", "guideline_or_expert_review", "human_same_variant", "human_same_gene", "human_related_disease_or_mechanism",
                         "preclinical_same_gene", "preclinical_related", "mechanistic_inference", "trial_registry_record", "none"]
                t3.sort(key=lambda i: (order.index(i.evidence_level) if i.evidence_level in order else 99, i.priority, i.item_id))
                if not t3:
                    L.append("- No hypothesized action is recommended (all assessed bespoke strategies were judged not amenable); outcome measures and ranking are therefore not applicable.")
                for n, i in enumerate(t3, start=1):
                    L.append(f"- **{n}. {i.title.strip().rstrip(':')}:** ranked by verified evidence level ({EVIDENCE_LABEL.get(i.evidence_level, i.evidence_level)}).")
                    L.append("  - **Safety metrics:** Not verified in this run; must be defined (modality-specific) before any preclinical or clinical use.")
                    L.append("  - **Efficacy metrics:** Not verified in this run; objective, age-appropriate measures must be specified.")
                    L.append("  - **Preclinical testing:** Required before any use; specific studies were not verified in this run.")
            else:
                L.append("- No hypothesized action is recommended with sufficient rationale; outcome measures and ranking are therefore not applicable.")
            L.append("")
    return "\n".join(L).rstrip() + "\n"
