"""Data models.

* Official input / output schemas (verified against
  https://aitxchallenge.org/#input-format and the four official case files).
* Internal structured outputs exchanged between pipeline stages.

Structured-output models avoid optional/nullable fields: empty strings and
empty lists mean "not applicable". This keeps the JSON schemas compatible with
the Claude structured-outputs subset and makes downstream code simpler.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# Official I/O
# --------------------------------------------------------------------------

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    gene: str = Field(min_length=1, description="HGNC gene symbol")
    transcript: str = Field(min_length=1, description="RefSeq or Ensembl transcript identifier")
    variant_cdna: str = Field(min_length=1, description="cDNA HGVS notation")
    variant_protein: str = Field(default="", description="Protein HGVS notation")
    zygosity: str = Field(min_length=1, description='e.g. "heterozygous", "homozygous", "hemizygous"')


class Patient(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    genotype: list[Variant] = Field(min_length=1)
    clinical_context: str = ""


class PatientCase(BaseModel):
    """One input task instance. Unknown top-level keys are tolerated (ignored)
    so that a validator payload with extra metadata does not crash the endpoint,
    but `id` and `patient` are mandatory."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    patient: Patient

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not ID_PATTERN.match(v):
            raise ValueError(f"Invalid id {v!r}")
        return v


class ReportOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    report_markdown: str


# --------------------------------------------------------------------------
# Report sections (names mirror the official template headings)
# --------------------------------------------------------------------------


class Section(str, Enum):
    EXISTING_TARGETED = "existing_targeted_therapies"
    SUPPORTIVE = "supportive_management"
    CONTRAINDICATIONS = "contraindications_precautions"
    TRIALS = "clinical_trial_eligibility"
    OFF_LABEL = "off_label_expanded_access"
    BESPOKE = "bespoke_therapies"
    REPURPOSING = "drug_repurposing"


SECTION_TIER = {
    Section.EXISTING_TARGETED: 1,
    Section.SUPPORTIVE: 1,
    Section.CONTRAINDICATIONS: 1,
    Section.TRIALS: 2,
    Section.OFF_LABEL: 2,
    Section.BESPOKE: 3,
    Section.REPURPOSING: 3,
}

EvidenceLevel = Literal[
    "regulatory_approval",  # label/approval for this indication
    "guideline_or_expert_review",  # GeneReviews / consensus guideline
    "human_same_variant",
    "human_same_gene",
    "human_related_disease_or_mechanism",
    "preclinical_same_gene",
    "preclinical_related",
    "mechanistic_inference",
    "trial_registry_record",
    "none",
]

ClaimKind = Literal[
    "molecular_fact",
    "disease_fact",
    "treatment_effect_human",
    "treatment_effect_preclinical",
    "regulatory_status",
    "trial_status_or_eligibility",
    "safety_or_contraindication",
    "mechanistic_reasoning",
    "patient_applicability",
]


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="One atomic, checkable statement.")
    citations: list[str] = Field(description="Evidence-ledger keys that support this claim, e.g. PMID:123456. Empty only for explicit reasoning.")
    kind: ClaimKind


class ActionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(description="Short unique slug, e.g. 'supportive-seizures-1'.")
    section: Section
    title: str = Field(description="Therapy, strategy, agent, or trial name (bold label in the report).")
    statement: str = Field(
        description="Report text for this item. Cite inline with ledger keys in square brackets, e.g. [PMID:123456] or [NCT01234567]."
    )
    conditional_action: str = Field(description="'If X, then Y; otherwise Z.' when missing data changes the action; else ''.")
    evidence_level: EvidenceLevel
    priority: int = Field(description="1 = highest priority within its section.")
    claims: list[Claim]
    missing_information: list[str] = Field(description="Patient data that would change this item.")
    # Section-specific fields (empty when not applicable)
    bespoke_verdict: Literal["AMENABLE", "NOT AMENABLE", "UNCERTAIN", ""] = ""
    nct_id: str = ""
    criteria_met: list[str] = Field(default_factory=list)
    criteria_unassessable: list[str] = Field(default_factory=list)
    potential_exclusions: list[str] = Field(default_factory=list)
    human_precedent: str = Field(default="", description="Off-label: who received it, with what outcome. Repurposing: ''.")


class SectionResearchOutput(BaseModel):
    """Output of one section-group researcher."""

    model_config = ConfigDict(extra="forbid")

    items: list[ActionItem]
    searched_but_not_found: list[str] = Field(
        description="Explicit negative findings, e.g. 'No FDA-approved therapy targeting <GENE> was found in openFDA labels or GeneReviews.'"
    )
    open_questions: list[str]


# --------------------------------------------------------------------------
# Agent A — interpretation
# --------------------------------------------------------------------------


class VariantInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gene: str
    hgvs_c: str
    hgvs_p: str
    consequence: str = Field(description="e.g. missense, frameshift with predicted NMD, canonical splice ...")
    variant_specific_evidence: str = Field(description="What is known about THIS variant (ClinVar, functional studies), with citations.")
    gene_level_mechanism: str = Field(description="Established disease mechanism(s) for the gene, with citations.")
    variant_mechanism_assessment: str = Field(
        description="Best-supported mechanism for this variant and why; never inferred solely from other variants."
    )
    mechanism_label: Literal[
        "loss_of_function",
        "haploinsufficiency",
        "dominant_negative",
        "gain_of_function",
        "biallelic_loss_of_function",
        "mixed_or_allele_specific",
        "uncertain",
    ]
    mechanism_confidence: Literal["established_for_variant", "supported_by_gene_and_variant_class", "inferred", "uncertain"]
    citations: list[str]


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disease_name: str
    inheritance: str
    variant_hgvs: list[str] = Field(description="transcript:c. notations (from the input) explaining this diagnosis")
    genotype_consistency: str


class Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnoses: list[Diagnosis] = Field(description="One entry per distinct genetic diagnosis (several monogenic conditions are possible).")
    disease_name: str = Field(description="Primary diagnosis (first entry of `diagnoses`).")
    disease_identifiers: list[str] = Field(description="OMIM/MONDO/ORPHA identifiers only if retrieved.")
    inheritance: str
    genotype_consistency: str = Field(description="Does zygosity/inheritance fit the disease? Flag inconsistencies.")
    variants: list[VariantInterpretation]
    phenotype_summary: str
    patient_age_years: float = Field(description="-1 if unknown.")
    patient_sex: Literal["male", "female", "unknown"]
    key_missing_clinical_information: list[str]
    research_plan: list[str] = Field(description="Targeted questions per report section for the evidence researchers.")
    citations: list[str]


# --------------------------------------------------------------------------
# Agent D — verification
# --------------------------------------------------------------------------

Verdict = Literal["supported", "partially_supported", "unsupported", "contradicted", "not_checkable"]


class ClaimVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    claim_index: int
    verdict: Verdict
    evidence_type: Literal[
        "direct_human_same_variant",
        "direct_human_same_gene",
        "direct_human_same_disease",
        "human_other_context",
        "preclinical",
        "mechanistic_inference",
        "none",
    ]
    overgeneralization: str = Field(description="Describe wrong transfer from another gene/variant/disease/age group, else ''.")
    abstract_only: bool = Field(description="True if only an abstract/metadata was available for checking.")
    rationale: str
    suggested_revision: str = Field(description="Corrected/qualified wording when partially supported; '' otherwise.")


class VerificationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdicts: list[ClaimVerdict]


class ItemRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    action: Literal["keep", "revise", "move", "drop"]
    new_section: Section | Literal[""] = ""
    revised_statement: str = ""
    revised_title: str = ""
    reason: str


class RevisionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revisions: list[ItemRevision]


# --------------------------------------------------------------------------
# Agent E — independent review (Gemini) and adjudication
# --------------------------------------------------------------------------

IssueType = Literal[
    "missed_intervention",
    "unsupported_claim",
    "tier_misplacement",
    "missed_contraindication",
    "trial_eligibility_error",
    "contradiction",
    "important_omission",
]


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_type: IssueType
    target_item_id: str = Field(description="Existing item id, or '' for omissions.")
    description: str
    suggested_fix: str
    cited_keys: list[str] = Field(description="Ledger keys or external identifiers (PMID/NCT) the reviewer relies on.")
    severity: Literal["high", "medium", "low"]


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[ReviewIssue]


class AdjudicationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_index: int
    decision: Literal["accept", "reject", "needs_research"]
    rationale: str
    research_query: str = Field(description="Focused research request when decision=needs_research, else ''.")


class Adjudication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[AdjudicationDecision]


# --------------------------------------------------------------------------
# Agent F — prioritisation / summary / outcome measures
# --------------------------------------------------------------------------


class SummaryLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Literal[1, 2, 3]
    text: str = Field(description="Highest-value action for this tier, stated as something the care team can do; cite ledger keys inline.")
    item_ids: list[str]


class OutcomeMeasure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int
    item_id: str
    action_name: str
    rank_rationale: str
    safety_metrics: str
    efficacy_metrics: str
    preclinical_testing: str
    citations: list[str]


class Synthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_lines: list[SummaryLine]
    missing_information: list[str] = Field(description="Each as 'X would change the recommendations: if A, then B; otherwise C.'")
    duplicate_item_ids_to_drop: list[str] = Field(description="Items repeating an action already placed in a better-fitting section.")
    outcome_measures: list[OutcomeMeasure]
    calibration_notes: list[str]


# --------------------------------------------------------------------------
# Development heuristic judge (NOT the official score)
# --------------------------------------------------------------------------


class DimensionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: Literal["factual_accuracy", "completeness", "evidence_justification", "prioritization_calibration", "clinical_utility"]
    score_1_to_5: int
    main_problems: list[str]


class JudgeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scores: list[DimensionScore]
    specific_fixable_issues: list[str]
