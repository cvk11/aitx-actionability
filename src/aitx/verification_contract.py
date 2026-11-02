"""Bind verification to the exact final item, including every auxiliary field.

The complete model is intentional: newly added fields cannot silently escape the
verification boundary. Digests detect stale decisions and post-verification edits;
they are integrity checks, not signatures or a substitute for entailment review.
"""
from __future__ import annotations

import hashlib
import json

from .schemas import ActionItem


def item_text(item: ActionItem) -> str:
    return json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def item_digest(item: ActionItem) -> str:
    return text_digest(item_text(item))


def contract_errors(items: list[ActionItem], contracts: dict) -> list[str]:
    errors = []
    for item in items:
        c = contracts.get(item.item_id, {})
        if c.get("digest") != item_digest(item) or c.get("status") not in ("supported", "hypothesis"):
            errors.append(f"{item.item_id}: missing, rejected or stale final item verification")
    return errors
