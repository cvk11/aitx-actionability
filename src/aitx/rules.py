"""Deterministic, generalizable report rules (no LLM).

These implement the official template's tier definitions and the challenge's
citation requirements as code, so they hold regardless of model behaviour.
Every change is recorded as a `RuleEvent` in the audit trail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ledger import EvidenceLedger, extract_keys, find_bracket_tokens
from .retrieval.clinicaltrials import EXPANDED_ACCESS_OPEN, OPEN_STATUSES, eligibility_blocks
from .schemas import ActionItem, ClaimVerdict, Section

HUMAN_PUBTYPES = {"Case Reports", "Clinical Trial", "Clinical Study", "Clinical Trial, Phase I", "Clinical Trial, Phase II",
                  "Clinical Trial, Phase III", "Randomized Controlled Trial", "Observational Study", "Multicenter Study", "Comparative Study"}
GUIDELINE_SOURCES = {"fda_label", "genereviews"}
LITERATURE_SOURCES = {"pubmed", "europepmc"}
# published human treatment reports may be cited from the literature, GeneReviews or an allow-listed journal page
PRECEDENT_SOURCES = LITERATURE_SOURCES | {"genereviews", "web"}
HUMAN_PRECEDENT_TYPES = {"direct_human_same_variant", "direct_human_same_gene", "direct_human_same_disease"}
HUMAN_APPLICABLE = {"same_variant", "same_gene_same_mechanism", "same_disease_other_gene"}
REASONING_KINDS = {"mechanistic_reasoning", "patient_applicability"}
OK_VERDICTS = {"supported", "not_checkable"}
TIER3 = {Section.BESPOKE, Section.REPURPOSING}
OVERCLAIM = re.compile(r"\b(will|would)\s+(improve|benefit|respond|help|work|be effective)|\bis (effective|beneficial) (for|in) (this|the) patient|"
                       r"\bproven (to|effective)|\bestablished (benefit|efficacy)|\bcures?\b|\bguarantee", re.I)
THIS_VARIANT = re.compile(r"\b(this|the patient'?s|the index) variant\b", re.I)
# Interventions whose rationale depends on the DIRECTION of the variant's functional effect: a potentiator is only
# rational for reduced function and an inhibitor only for increased function. Proposing one commits to a GoF/LoF
# assumption, so the item must rest on evidence about this variant or this variant's established mechanism.
DIRECTIONAL = re.compile(
    r"\b(potentiat\w*|activat\w*|agonist\w*|positive modulat\w*|enhanc\w*|"
    r"inhibit\w*|antagonist\w*|blocker\w*|negative modulat\w*|suppress\w*|"
    r"knock[- ]?down|silenc\w*|allele[- ](?:specific|selective))\b", re.I)
MECHANISM_APPLICABLE = {"same_variant", "same_gene_same_mechanism"}
STRENGTH = ["mechanistic_inference", "preclinical_related", "preclinical_same_gene", "human_related_disease_or_mechanism", "human_same_gene",
            "human_same_variant", "guideline_or_expert_review", "regulatory_approval"]
# claim kinds that carry an item's core assertion: trials/contraindications without a supported core claim are dropped
REQUIRED_KINDS = {
    Section.TRIALS: {"trial_status_or_eligibility"},
    Section.CONTRAINDICATIONS: {"safety_or_contraindication"},
}


@dataclass
class RuleEvent:
    item_id: str
    rule: str
    action: str  # removed_citation | moved | dropped | flagged | set_field
    detail: str


@dataclass
class RuleResult:
    items: list[ActionItem]
    events: list[RuleEvent] = field(default_factory=list)
    problems: dict[str, list[str]] = field(default_factory=dict)  # item_id -> problems requiring LLM revision
    # item_id -> verdict of the FULL rendered statement: supported | not_checkable | partially_supported | unsupported |
    # contradicted | unverified | needs_reverification (only "supported"/"not_checkable" may be rendered)
    statement_status: dict[str, str] = field(default_factory=dict)
    # claim verdicts re-indexed to the kept claims (for later re-application)
    reindexed: list[ClaimVerdict] = field(default_factory=list)

    def problem(self, item_id: str, text: str) -> None:
        self.problems.setdefault(item_id, []).append(text)


def _strip_unknown_citations(text: str, ledger: EvidenceLedger) -> tuple[str, list[str]]:
    removed = []
    for tok in set(find_bracket_tokens(text)):
        if tok not in ledger:
            removed.append(tok)
            text = re.sub(r"\s*\[" + re.escape(tok) + r"\]", "", text)
    return text, removed


def item_citations(item: ActionItem) -> list[str]:
    # All present and future claim-bearing fields contribute sources to the
    # full-item verifier, not just the three historically rendered text fields.
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
    keys = [k for value in strings(item.model_dump(mode="json")) for k in extract_keys(value)]
    for c in item.claims:
        keys += c.citations
    seen, out = set(), []
    for k in keys:
        from .ledger import normalize_key

        k = normalize_key(k)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _is_human_record(ledger: EvidenceLedger, key: str) -> bool:
    """Deterministic PRE-filter only: a literature record whose publication type denotes human clinical data.

    Words such as "patient" are deliberately NOT used (a mouse study can say "no patients received the drug").
    Whether the record really documents the intervention given to patients in the same gene/disease/variant
    context, with an outcome, is decided by claim-level verification (`apply_verdicts`)."""
    r = ledger.get(key)
    if r is None or r.source_type not in LITERATURE_SOURCES:
        return False
    return bool(set(r.metadata.get("pub_types") or []) & HUMAN_PUBTYPES)


def apply_citation_rules(items: list[ActionItem], ledger: EvidenceLedger) -> RuleResult:
    res = RuleResult(items=[])
    for it in items:
        it = it.model_copy(deep=True)
        it.statement, rem = _strip_unknown_citations(it.statement, ledger)
        it.human_precedent, rem2 = _strip_unknown_citations(it.human_precedent, ledger)
        it.conditional_action, rem3 = _strip_unknown_citations(it.conditional_action, ledger)
        for tok in rem + rem2 + rem3:
            res.events.append(RuleEvent(it.item_id, "citation_not_in_ledger", "removed_citation", tok))
        for c in it.claims:
            bad = [k for k in c.citations if k not in ledger]
            if bad:
                c.citations = [k for k in c.citations if k in ledger]
                res.events.append(RuleEvent(it.item_id, "claim_citation_not_in_ledger", "removed_citation", ",".join(bad)))
        for k in item_citations(it):
            r = ledger.get(k)
            if r is not None and r.metadata.get("retracted"):
                it.statement = re.sub(r"\s*\[" + re.escape(k) + r"\]", "", it.statement)
                for c in it.claims:
                    c.citations = [x for x in c.citations if x != k]
                res.events.append(RuleEvent(it.item_id, "retracted_source", "removed_citation", k))
                res.problem(it.item_id, f"Cited source {k} is retracted; remove dependent statements.")
        if not item_citations(it):
            res.problem(it.item_id, "Item has no valid ledger citation; support it with retrieved evidence or drop it.")
        res.items.append(it)
    return res


_GENE_PATTERNS = [
    re.compile(r"\b([A-Z][A-Z0-9]{1,9})(?:-related|-associated|\s+gene\b|\s+(?:pathogenic\s+)?(?:variants?|mutations?)\b)"),
    re.compile(r"(?:variants?|mutations?|deficiency|haploinsufficiency)\s+(?:in|of)\s+(?:the\s+)?([A-Z][A-Z0-9]{1,9})\b"),
]
_NOT_GENES = {"DNA", "RNA", "MRI", "EEG", "ECG", "IQ", "ALS", "SMA", "DMD", "CNS", "HIV", "COVID", "FDA", "NIH", "US", "EU", "UK", "LOF", "GOF",
              "ASO", "AAV", "CRISPR", "AND", "OR", "NOT", "DEE", "EE", "HSP", "SCA", "CMT", "HSAN", "MTDNA", "PCR", "WES", "WGS", "ACMG", "VUS"}


def trial_named_genes(text: str) -> set[str]:
    """Gene symbols a trial record explicitly requires/names ('pathogenic variant in X', 'X-related', 'X gene')."""
    out = set()
    for pat in _GENE_PATTERNS:
        for m in pat.finditer(text or ""):
            g = m.group(1)
            if g not in _NOT_GENES and any(ch.isdigit() for ch in g) or (g not in _NOT_GENES and len(g) >= 4):
                out.add(g)
    return out


def apply_section_rules(items: list[ActionItem], ledger: EvidenceLedger, age_years: float | None, sex: str,
                        genes: list[str] | None = None) -> RuleResult:
    res = RuleResult(items=[])
    for it in items:
        it = it.model_copy(deep=True)
        cites = item_citations(it)
        # ---------------- clinical trials
        if it.section == Section.TRIALS:
            nct = it.nct_id.strip().upper() or next((k for k in cites if k.startswith("NCT")), "")
            rec = ledger.get(nct) if nct else None
            if rec is None:
                res.events.append(RuleEvent(it.item_id, "trial_not_retrieved", "dropped", nct or "no NCT id"))
                continue
            it.nct_id = nct
            status = rec.metadata.get("overall_status")
            if status not in OPEN_STATUSES + EXPANDED_ACCESS_OPEN:
                res.events.append(RuleEvent(it.item_id, "trial_not_recruiting", "dropped", f"{nct} status {status}"))
                continue
            if status != "RECRUITING":
                # not actively recruiting: kept only with an explicit status label, ranked below recruiting trials, and never
                # presented as a current enrolment option
                it.priority = max(it.priority, 50)
                note = {"NOT_YET_RECRUITING": "Not yet recruiting: a possible future option only; enrolment is not currently possible",
                        "ENROLLING_BY_INVITATION": "Enrolling by invitation only: not open recruitment"}.get(
                    status, "Expanded access: not a clinical trial; availability must be confirmed with the sponsor")
                it.criteria_unassessable = list(dict.fromkeys(it.criteria_unassessable + [note]))
            if rec.metadata.get("status_verification") == "failed":
                it.criteria_unassessable = list(dict.fromkeys(
                    it.criteria_unassessable + ["Current recruitment status and eligibility could not be re-verified live in this run"]))
            lo, hi = rec.metadata.get("min_age_years"), rec.metadata.get("max_age_years")
            if age_years is not None and ((lo is not None and age_years < lo - 1e-6) or (hi is not None and age_years > hi + 1e-6)):
                res.events.append(RuleEvent(it.item_id, "trial_age_ineligible", "dropped", f"{nct} ages {lo}-{hi}, patient {age_years}"))
                continue
            tsex = (rec.metadata.get("sex") or "ALL").upper()
            if sex in ("male", "female") and tsex in ("MALE", "FEMALE") and tsex.lower() != sex:
                res.events.append(RuleEvent(it.item_id, "trial_sex_ineligible", "dropped", f"{nct} sex {tsex}"))
                continue
            # genotype eligibility: a trial restricted to named genes must include the patient's gene
            blocks = rec.metadata.get("eligibility_blocks")
            if blocks is None:
                blocks = eligibility_blocks(rec.metadata.get("eligibility_criteria") or rec.content)
            global_inclusion = [b["text"] for b in blocks if b["scope"] == "inclusion" and not b["arm"]]
            # An optional mention or a negated requirement is not a mandatory
            # genotype criterion. Do not infer restrictions from title/summary.
            mandatory = [t for t in global_inclusion if not re.search(r"\b(may|optional|no|not|without|exclud\w*|regardless|including|or|e\.g\.)\b", t, re.I)]
            named = trial_named_genes("\n".join(mandatory))
            if genes and named:
                if not named & {g.upper() for g in genes}:
                    res.events.append(RuleEvent(it.item_id, "trial_genotype_ineligible", "dropped", f"{nct} inclusion requires {sorted(named)[:8]}; patient {genes}"))
                    continue
                it.criteria_met = list(dict.fromkeys(it.criteria_met + [f"Genetic diagnosis in {', '.join(sorted(named & {g.upper() for g in genes}))} (named in inclusion criteria; other criteria remain to be assessed)"]))
            ambiguous_genes = bool(trial_named_genes("\n".join(global_inclusion))) and not named
            if not global_inclusion or ambiguous_genes or any(b["scope"] in ("unknown", "general") or b["arm"] for b in blocks):
                it.criteria_unassessable = list(dict.fromkeys(it.criteria_unassessable + [
                    "Eligibility is uncertain: missing/unscoped or arm-specific criteria require registry and sponsor review; no criteria are transferred between arms"]))
            excluded = trial_named_genes("\n".join(b["text"] for b in blocks if b["scope"] == "exclusion"))
            if genes and excluded & {g.upper() for g in genes}:
                it.potential_exclusions = list(dict.fromkeys(it.potential_exclusions + [
                    "Patient gene is mentioned in exclusion criteria; confirm scope and wording with the sponsor before eligibility assessment"]))
            arms = rec.metadata.get("eligibility_arm_headings") or []
            if arms:
                # criteria under an arm/cohort heading belong to that arm only (e.g. healthy-volunteer requirements)
                it.criteria_unassessable = list(dict.fromkeys(it.criteria_unassessable + [
                    f"The registry eligibility text is split by arm/cohort ({', '.join(arms[:4])}); each criterion applies only to its own "
                    "arm, so arm-specific requirements must be attributed to the correct arm before judging eligibility"]))
            if re.search(r"gain[- ]of[- ]function|loss[- ]of[- ]function|dominant[- ]negative|hypomorph", rec.content or "", re.I):
                it.criteria_unassessable = list(dict.fromkeys(it.criteria_unassessable + [
                    "The record refers to a variant functional class (gain/loss of function); this variant's class must be confirmed"]))
            if age_years is None:
                it.criteria_unassessable = list(dict.fromkeys(it.criteria_unassessable + ["Patient age (not stated in case)"]))
            if re.search(r"\b(is|are) eligible\b", it.statement, re.I):
                res.problem(it.item_id, "Statement asserts definite eligibility; rephrase as 'may qualify' with unassessable criteria.")
        # off-label / existing-targeted placement is decided AFTER verification from verified evidence
        # (apply_verdicts); no publication-type pre-filter may exclude genuine human treatment reports here.
        # ---------------- bespoke needs a verdict
        if it.section == Section.BESPOKE and not it.bespoke_verdict:
            it.bespoke_verdict = "UNCERTAIN"
            res.events.append(RuleEvent(it.item_id, "bespoke_missing_verdict", "set_field", "UNCERTAIN"))
        res.items.append(it)
    return res


def _cites_source(c, ledger: EvidenceLedger, kinds: set[str]) -> bool:
    return any((ledger.get(k) is not None and ledger.get(k).source_type in kinds) for k in c.citations)


def apply_verdicts(items: list[ActionItem], verdicts: list[ClaimVerdict], *, ledger: EvidenceLedger | None = None,
                   statements_only: bool = False) -> RuleResult:
    """Apply claim-level verification results — FAIL CLOSED, while preserving verified lower-tier evidence.

    Claim decisions
      supported            -> verified (factual) evidence
      not_checkable        -> accepted ONLY for explicit reasoning kinds (mechanistic_reasoning, patient_applicability);
                              a factual claim that "cannot be checked" is not evidence and is removed
      partially_supported  -> kept for traceability, does not count as support; wording goes to revision (never auto-applied)
      insufficient_source_text -> removed, recorded as NOT ASSESSABLE (e.g. table/full text inaccessible) — not as false
      unsupported/contradicted -> removed
      no decision          -> removed (unverified)
    An item needs at least one SUPPORTED factual claim. The full statement must be supported (Tier 1/2); in Tier 3 a
    not_checkable statement is accepted as an explicit hypothesis on top of verified premises.
    Tier gate (with `ledger`): Off-label needs a supported human-treatment claim (direct_human_* evidence, applicable to
    this patient's variant/gene/disease, same intervention, citing a published report); Existing Targeted needs a
    supported label/guideline claim for the same intervention. Failing items move to the tier their verified evidence
    supports and must be rewritten + re-verified. The evidence level shown in the report is derived from verified
    evidence, not from the researcher's own label."""
    by_item: dict[str, dict[int, ClaimVerdict]] = {}
    for v in verdicts:
        by_item.setdefault(v.item_id, {})[v.claim_index] = v
    res = RuleResult(items=[])
    for it in items:
        it = it.model_copy(deep=True)
        vs = by_item.get(it.item_id, {})
        kept: list = []
        kept_v: list[ClaimVerdict | None] = []
        for i, c in enumerate(it.claims):
            v = vs.get(i)
            if v is None:
                if statements_only:
                    kept.append(c)
                    kept_v.append(None)
                    continue
                res.events.append(RuleEvent(it.item_id, "claim_unverified", "removed_claim", c.text[:160]))
                res.problem(it.item_id, f"Claim could not be verified (no decision): '{c.text[:160]}'.")
                continue
            if v.verdict == "not_checkable" and c.kind not in REASONING_KINDS:
                res.events.append(RuleEvent(it.item_id, "factual_claim_not_checkable", "removed_claim", c.text[:160]))
                res.problem(it.item_id, f"Factual claim could not be checked against any source: '{c.text[:160]}'. Cite a retrieved source or drop it.")
                continue
            if (v.verdict == "supported" and c.kind == "molecular_fact" and THIS_VARIANT.search(c.text)
                    and v.applicability == "same_gene_other_variant_or_mechanism"):
                res.events.append(RuleEvent(it.item_id, "variant_mechanism_transfer", "removed_claim", c.text[:160]))
                res.problem(it.item_id, f"Claim about THIS variant is supported only for a different variant/mechanism: '{c.text[:160]}'. "
                                        "State it as evidence about the other variant, not about the patient's variant.")
                continue
            if v.verdict in OK_VERDICTS:
                kept.append(c)
                kept_v.append(v)
            elif v.verdict == "partially_supported":
                kept.append(c)
                kept_v.append(v)
                hint = f" Verifier-suggested wording (unverified, must be re-checked): '{v.suggested_revision[:200]}'" if v.suggested_revision.strip() else ""
                res.problem(it.item_id, f"Claim only partially supported: '{c.text[:160]}' — {v.rationale[:200]}.{hint}")
            elif v.verdict == "insufficient_source_text":
                res.events.append(RuleEvent(it.item_id, "claim_not_assessable", "removed_claim", f"{c.text[:160]} | {v.rationale[:200]}"))
                res.problem(it.item_id, f"Evidence for '{c.text[:160]}' could not be assessed from the accessible text (not shown to be false). "
                                        "Retrieve the full text/table passage, or restate the item using only verifiable claims.")
            else:
                res.events.append(RuleEvent(it.item_id, f"claim_{v.verdict}", "removed_claim", f"{c.text[:160]} | {v.rationale[:200]}"))
                res.problem(it.item_id, f"Claim {v.verdict}: '{c.text[:160]}' — {v.rationale[:200]}. Remove or correct it in the statement.")
            if v.overgeneralization.strip():
                res.problem(it.item_id, f"Overgeneralisation: {v.overgeneralization[:200]}")
        it.claims = kept
        # -------- full statement
        sv = vs.get(-1)
        status = sv.verdict if sv is not None else "unverified"
        if sv is None:
            res.events.append(RuleEvent(it.item_id, "statement_unverified", "flagged", "no decision for the full statement"))
            res.problem(it.item_id, "Full statement could not be verified (no decision); it cannot be rendered unless re-verified.")
        elif sv.verdict == "not_checkable":
            if it.section in TIER3:
                status = "hypothesis"
            else:
                status = "needs_reverification"
                res.problem(it.item_id, "Tier 1/2 statement could not be checked against sources; rewrite it to state only cited facts.")
        elif sv.verdict == "partially_supported":
            hint = f" Verifier-suggested wording (unverified): '{sv.suggested_revision[:300]}'" if sv.suggested_revision.strip() else ""
            res.problem(it.item_id, f"Full statement partially supported: {sv.rationale[:250]}. Rewrite it to say only what the cited sources support.{hint}")
        elif sv.verdict == "insufficient_source_text":
            res.problem(it.item_id, f"Full statement could not be assessed from accessible text: {sv.rationale[:250]}. Restate it from verifiable claims.")
        elif sv.verdict in ("unsupported", "contradicted"):
            res.problem(it.item_id, f"Full statement {sv.verdict}: {sv.rationale[:250]}. Rewrite it to say only what the cited sources support.")
        if sv is not None and sv.overgeneralization.strip():
            res.problem(it.item_id, f"Overgeneralisation in statement: {sv.overgeneralization[:200]}")
        # direction-of-effect dependency: a potentiator/inhibitor proposal commits to a functional class for THIS
        # variant. Unless a verified claim applies to this variant or its established mechanism, the dependency and
        # the uncertainty must be stated rather than assumed.
        if it.section in TIER3 and DIRECTIONAL.search(f"{it.title} {it.statement}"):
            functional = [v for c, v in zip(kept, kept_v)
                          if c.kind == "molecular_fact" and v is not None and v.verdict == "supported"
                          and v.applicability == "same_variant" and v.functional_evidence == "variant_function"]
            directions = {v.functional_direction for v in functional}
            grounded = bool(functional) and len(directions) == 1 and directions <= {"gain_of_function", "loss_of_function"}
            it.mechanism_qualification = ""
            if not grounded:
                it.mechanism_qualification = (
                    "Conditional experimental research hypothesis only: the variant's functional direction is unresolved or conflicting. "
                    "Variant identity/pathogenicity and findings in other variants do not establish gain or loss of function. "
                    "Direction-dependent intervention requires variant-specific functional testing, including wild-type/mutant "
                    "co-expression where relevant, before therapeutic prioritization; patient benefit is not established.")
                res.events.append(RuleEvent(it.item_id, "mechanism_direction_unestablished", "flagged",
                                            "directional intervention without variant-specific mechanism evidence"))
                res.problem(it.item_id, "This candidate's rationale depends on the direction of the variant's functional effect "
                                        "(e.g. potentiation vs inhibition), but no verified claim establishes that direction for this "
                                        "variant. State the dependency and the uncertainty explicitly, or drop the candidate.")
            elif not all(v.functional_context in ("heterozygous_wild_type_mutant", "patient_derived") for v in functional):
                it.mechanism_qualification = (
                    "Conditional experimental research hypothesis: variant-specific functional direction was measured, but the "
                    "wild-type/mutant context and therapeutic response remain unestablished. Test the patient's allelic context "
                    "before therapeutic prioritization; patient benefit is not established.")
        if it.section != Section.TRIALS and OVERCLAIM.search(it.statement + " " + it.human_precedent):
            status = "needs_reverification" if status in ("supported", "hypothesis") else status
            res.problem(it.item_id, "Statement predicts benefit for this patient or overstates certainty; report only what the evidence shows "
                                    "(a response in a published individual does not show that this patient will respond).")
        ok = [(c, v) for c, v in zip(kept, kept_v) if v is None or v.verdict in OK_VERDICTS]
        factual = [(c, v) for c, v in ok if c.kind not in REASONING_KINDS and (v is None or v.verdict == "supported")]
        recoverable = any(v is not None and v.verdict == "partially_supported" and c.citations
                          and c.kind not in REASONING_KINDS for c, v in zip(kept, kept_v))
        if not factual and not recoverable:
            res.events.append(RuleEvent(it.item_id, "no_verified_factual_claim", "dropped", "only reasoning or unverified claims remain"))
            continue
        req = REQUIRED_KINDS.get(it.section, set())
        if not factual and recoverable:
            status = "pending_revision"
            res.problem(it.item_id, "No fully supported factual premise yet: narrow the partial claims and re-verify within the revision budget.")
        if req and not any(c.kind in req for c, _ in ok) and not recoverable:
            res.events.append(RuleEvent(it.item_id, "core_claims_unsupported", "dropped", f"no supported {sorted(req)} claim remains"))
            continue
        # -------- evidence-based tier gate
        if ledger is not None and not statements_only:
            def human_precedent() -> bool:
                return any(c.kind == "treatment_effect_human" and v is not None and v.verdict == "supported"
                           and v.evidence_type in HUMAN_PRECEDENT_TYPES and v.applicability in HUMAN_APPLICABLE and v.intervention_matches
                           and _cites_source(c, ledger, PRECEDENT_SOURCES) for c, v in ok)

            def demote(target: Section, rule: str, why: str) -> None:
                nonlocal status
                res.events.append(RuleEvent(it.item_id, rule, "moved", f"{it.section.value} -> {target.value}"))
                it.section = target
                if target == Section.REPURPOSING:
                    it.human_precedent = ""
                status = "needs_reverification"
                res.problem(it.item_id, f"Moved to {target.value}: {why} Rewrite the statement for this tier using only the supported claims.")

            if it.section == Section.EXISTING_TARGETED:
                strong = any(c.kind in ("regulatory_status", "treatment_effect_human") and v is not None and v.verdict == "supported"
                             and v.intervention_matches and _cites_source(c, ledger, GUIDELINE_SOURCES) for c, v in ok)
                if not strong:
                    demote(Section.OFF_LABEL if human_precedent() else Section.REPURPOSING, "targeted_without_verified_label_or_guideline",
                           "no verified label/guideline support for this intervention and indication.")
            if it.section == Section.OFF_LABEL and not human_precedent():
                demote(Section.REPURPOSING, "offlabel_without_verified_human_precedent",
                       "no verified human treatment precedent for this intervention in the same gene/disease/variant context.")
            if it.section == Section.REPURPOSING and human_precedent():
                res.problem(it.item_id, "Verified human treatment precedent in this context: consider moving to Off-label (Tier 2).")
            derived = derive_evidence_level(it, ok, ledger)
            if derived != it.evidence_level:
                res.events.append(RuleEvent(it.item_id, "evidence_level_derived", "set_field", f"{it.evidence_level} -> {derived}"))
                it.evidence_level = derived  # type: ignore[assignment]
        res.statement_status[it.item_id] = status
        res.reindexed += [v.model_copy(update={"claim_index": j}) for j, v in enumerate(kept_v) if v is not None]
        if sv is not None:
            res.reindexed.append(sv)
        res.items.append(it)
    return res


def derive_evidence_level(it: ActionItem, ok: list, ledger: EvidenceLedger) -> str:
    """Strongest evidence level actually VERIFIED for the item (shown in the report)."""
    if it.section == Section.TRIALS:
        return "trial_registry_record"
    best = "mechanistic_inference"

    def up(level: str) -> None:
        nonlocal best
        if STRENGTH.index(level) > STRENGTH.index(best):
            best = level

    for c, v in ok:
        if v is None or v.verdict != "supported":
            continue
        if c.kind == "regulatory_status" and _cites_source(c, ledger, {"fda_label"}) and v.intervention_matches:
            up("regulatory_approval")
        elif _cites_source(c, ledger, {"genereviews"}) and c.kind in ("treatment_effect_human", "safety_or_contraindication", "disease_fact"):
            up("guideline_or_expert_review")
        elif c.kind == "treatment_effect_human" and v.evidence_type in HUMAN_PRECEDENT_TYPES and v.intervention_matches:
            up("human_same_variant" if v.applicability == "same_variant" else
               "human_same_gene" if v.applicability in ("same_gene_same_mechanism", "same_gene_other_variant_or_mechanism") else
               "human_related_disease_or_mechanism")
        elif c.kind == "treatment_effect_preclinical" or v.evidence_type == "preclinical":
            up("preclinical_same_gene" if v.applicability in ("same_variant", "same_gene_same_mechanism", "same_gene_other_variant_or_mechanism")
               else "preclinical_related")
    if it.section == Section.SUPPORTIVE and best == "mechanistic_inference":
        best = "human_related_disease_or_mechanism"  # supported standard-of-care facts without guideline source
    return best


def compose_from_verified_claims(it: ActionItem, verdicts: list[ClaimVerdict]) -> ActionItem | None:
    """Last-resort salvage: rebuild a statement ONLY from claims that were individually verified as supported
    (verbatim claim texts + their citations). Used when no supportable free-text wording was found."""
    vs = {v.claim_index: v for v in verdicts if v.item_id == it.item_id}
    parts, claims = [], []
    for i, c in enumerate(it.claims):
        v = vs.get(i)
        if v is not None and v.verdict == "supported" and c.kind not in REASONING_KINDS and c.citations:
            parts.append(c.text.rstrip(". ") + " " + " ".join(f"[{k}]" for k in c.citations) + ".")
            claims.append(c.model_copy(deep=True))
    if not parts:
        return None
    # Construct a fresh item: no unverified auxiliary assertion may hitchhike on
    # verified atoms. Even this reconstruction must pass full-item verification.
    human = " ".join(p for c, p in zip(claims, parts) if c.kind == "treatment_effect_human") if it.section == Section.OFF_LABEL else ""
    return ActionItem(item_id=it.item_id, section=it.section, title=it.title,
                      statement=" ".join(parts), conditional_action="", evidence_level=it.evidence_level,
                      priority=it.priority, claims=claims, missing_information=[], nct_id=it.nct_id,
                      bespoke_verdict="UNCERTAIN" if it.section == Section.BESPOKE else "", human_precedent=human)


def _norm_title(t: str) -> set[str]:
    stop = {"the", "a", "an", "of", "for", "and", "or", "with", "in", "to", "therapy", "treatment", "aso", "based"}
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in stop}



GENERIC_TITLE_WORDS = {"drug", "drugs", "compound", "agent", "therapy", "therapies", "treatment", "supplementation", "oral", "intravenous",
                       "gene", "replacement", "mediated", "targeted", "strategy", "approach", "trial", "study", "management", "standard",
                       "medication", "medications", "inhibitor", "inhibitors", "agonist", "antagonist", "potentiator", "modulator",
                       "knockdown", "editing"}


def distinctive_terms(title: str) -> list[str]:
    """Tokens that identify a candidate's intervention (generic words removed; purely generic titles keep all words)."""
    toks = [t for t in _norm_title(title) if t not in GENERIC_TITLE_WORDS]
    return toks or list(_norm_title(title))

def find_cross_section_duplicates(items: list[ActionItem]) -> list[tuple[str, str]]:
    out = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if a.section == b.section:
                continue
            ta, tb = _norm_title(a.title), _norm_title(b.title)
            if ta and tb and len(ta & tb) / len(ta | tb) >= 0.75:
                out.append((a.item_id, b.item_id))
    return out
