"""Deterministic parsing of the free-text clinical context (age, sex)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_UNIT_YEARS = {"year": 1.0, "yr": 1.0, "y": 1.0, "month": 1 / 12, "mo": 1 / 12, "week": 1 / 52.18, "wk": 1 / 52.18, "day": 1 / 365.25}

AGE_RE = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*[- ]?\s*(?P<unit>years?|yrs?|y|months?|mos?|weeks?|wks?|days?)[- ]?(?:old|of age)?",
    re.IGNORECASE,
)
AGED_RE = re.compile(r"\baged?\s+(?P<n>\d+(?:\.\d+)?)\b", re.IGNORECASE)


@dataclass
class ClinicalFacts:
    age_years: float | None
    age_text: str
    sex: str  # male | female | unknown


def parse_age(text: str) -> tuple[float | None, str]:
    for m in AGE_RE.finditer(text):
        unit = m.group("unit").lower().rstrip("s")
        unit = {"yr": "yr", "mo": "mo", "wk": "wk"}.get(unit, unit)
        factor = _UNIT_YEARS.get(unit)
        if factor is None:
            continue
        # avoid "onset at 10 years of age" being taken as current age when a
        # "<n>-year-old" form also exists
        return round(float(m.group("n")) * factor, 3), m.group(0)
    m = AGED_RE.search(text)
    if m:
        return float(m.group("n")), m.group(0)
    if re.search(r"\b(newborn|neonate)\b", text, re.I):
        return 0.0, "newborn"
    return None, ""


def parse_current_age(text: str) -> tuple[float | None, str]:
    """Prefer explicit '<n>-year-old' / '<n> year old' descriptors for current age."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[- ]\s*(year|month|week|day)s?\s*[- ]\s*old", text, re.I)
    if m:
        unit = m.group(2).lower()
        return round(float(m.group(1)) * _UNIT_YEARS[unit], 3), m.group(0)
    return parse_age(text)


def parse_sex(text: str) -> str:
    t = text.lower()
    f = re.search(r"\b(female|girl|woman)\b", t)
    m = re.search(r"\b(male|boy|man)\b", t)
    if f and not m:
        return "female"
    if m and not f:
        return "male"
    if f and m:  # e.g. "male ... his female sibling": first explicit descriptor wins
        return "female" if f.start() < m.start() else "male"
    if re.search(r"\b(she)\b", t) and not re.search(r"\bhe\b", t):
        return "female"
    if re.search(r"\bhe\b", t) and not re.search(r"\bshe\b", t):
        return "male"
    return "unknown"


def parse_clinical_context(text: str) -> ClinicalFacts:
    age, age_text = parse_current_age(text or "")
    return ClinicalFacts(age, age_text, parse_sex(text or ""))
