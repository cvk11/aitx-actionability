"""Official report template handling.

Required headings are *derived from the official template file* that is stored
verbatim in ``data/official/``; the constant below is only a cross-check and a
fallback for deployments that ship without the data directory.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .config import PROJECT_ROOT
from .schemas import Section

TEMPLATE_PATH = PROJECT_ROOT / "data" / "official" / "aitxchallenge_report_template.md"

TITLE_PREFIX = "# Therapeutic Actionability Report for Patient "

# (level, text) in required order, excluding the H1 title (patient specific).
EXPECTED_HEADINGS: tuple[tuple[int, str], ...] = (
    (2, "Summary"),
    (2, "Tier 1: Established Actions"),
    (3, "Existing Targeted Therapies"),
    (3, "Supportive Management"),
    (3, "Contraindications & Precautions"),
    (2, "Tier 2: Emerging Actions"),
    (3, "Clinical Trial Eligibility"),
    (3, "Off-label or Expanded-Access Therapies"),
    (2, "Tier 3: Hypothesized Actions"),
    (3, "Bespoke Therapies"),
    (3, "Drug Repurposing"),
    (3, "Outcome Measures & Ranking"),
)

SECTION_HEADING = {
    Section.EXISTING_TARGETED: "Existing Targeted Therapies",
    Section.SUPPORTIVE: "Supportive Management",
    Section.CONTRAINDICATIONS: "Contraindications & Precautions",
    Section.TRIALS: "Clinical Trial Eligibility",
    Section.OFF_LABEL: "Off-label or Expanded-Access Therapies",
    Section.BESPOKE: "Bespoke Therapies",
    Section.REPURPOSING: "Drug Repurposing",
}

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_RE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)


def iter_headings(markdown: str) -> list[tuple[int, str]]:
    """ATX headings outside HTML comments and fenced code blocks."""
    text = _COMMENT_RE.sub("", markdown)
    text = _FENCE_RE.sub("", text)
    out: list[tuple[int, str]] = []
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2).strip()))
    return out


@lru_cache(maxsize=1)
def required_headings(path: Path = TEMPLATE_PATH) -> tuple[tuple[int, str], ...]:
    """Headings required by the official template (H2/H3, ordered)."""
    if not path.exists():
        return EXPECTED_HEADINGS
    heads = [(lvl, txt) for lvl, txt in iter_headings(path.read_text(encoding="utf-8")) if lvl in (2, 3)]
    return tuple(heads)


def title_for(case_id: str) -> str:
    return f"{TITLE_PREFIX}{case_id}"
