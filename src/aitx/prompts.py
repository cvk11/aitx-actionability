"""Version-controlled prompt templates (``prompts/*.md``) with SHA-256 provenance.

Placeholders use ``{{NAME}}`` and are filled by plain string replacement, so
JSON or braces inside inputs can never break formatting. Unfilled placeholders
raise, preventing silently incomplete prompts.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from .config import PROJECT_ROOT

PROMPT_DIR = PROJECT_ROOT / "prompts"
_PH = re.compile(r"\{\{([A-Z_]+)\}\}")


@lru_cache(maxsize=None)
def raw(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def sha256(name: str) -> str:
    return hashlib.sha256(raw(name).encode("utf-8")).hexdigest()


def render(name: str, **values: str) -> str:
    text = raw(name)
    if "{{COMMON_RULES}}" in text:
        values.setdefault("COMMON_RULES", raw("common_rules"))
    missing = sorted(set(_PH.findall(text)) - set(values))
    if missing:
        raise KeyError(f"unfilled placeholders in prompt {name}: {missing}")
    # single pass: inserted values are never re-scanned for placeholders
    return _PH.sub(lambda m: values[m.group(1)], text)


def manifest() -> dict[str, str]:
    return {p.stem: sha256(p.stem) for p in sorted(PROMPT_DIR.glob("*.md"))}
