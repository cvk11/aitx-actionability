"""Shared case context passed between stages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..clinical import ClinicalFacts
from ..schemas import ActionItem, Interpretation, PatientCase


@dataclass
class CaseContext:
    case: PatientCase
    clinical: ClinicalFacts
    dossier: dict[str, Any] = field(default_factory=dict)
    interpretation: Interpretation | None = None

    def patient_json(self) -> str:
        return self.case.model_dump_json(indent=1)

    def context_block(self) -> str:
        interp = self.interpretation.model_dump() if self.interpretation else {}
        compact_dossier = {
            "computed_variant_features": [v.get("computed_features") for v in self.dossier.get("variants", [])],
            "variantvalidator": [{k: (v.get("variantvalidator") or {}).get(k) for k in ("hgvs_transcript_variant", "predicted_protein", "genomic_grch38", "warnings")}
                                 for v in self.dossier.get("variants", [])],
            "protein_notation_consistent": [v.get("protein_notation_consistent") for v in self.dossier.get("variants", [])],
            "ledger_keys_from_bootstrap": self.dossier.get("bootstrap_keys", []),
            "retrieval_status": self.dossier.get("retrieval_status", {}),
        }
        return (
            f"PATIENT (official input):\n{self.patient_json()}\n\n"
            f"PARSED CLINICAL FACTS: age_years={self.clinical.age_years} (from '{self.clinical.age_text}'), sex={self.clinical.sex}\n\n"
            f"DETERMINISTIC VARIANT DOSSIER:\n{json.dumps(compact_dossier, indent=1, default=str)[:12000]}\n\n"
            f"INTERPRETATION (stage A):\n{json.dumps(interp, indent=1)[:14000]}"
        )


def items_json(items: list[ActionItem], max_chars: int = 40000) -> str:
    return json.dumps([i.model_dump(mode="json") for i in items], indent=1)[:max_chars]
