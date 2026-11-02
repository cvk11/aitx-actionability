"""ARCHIVED — not used. Project decision (2026-11-02): Claude Opus is the only active model; no pipeline
configuration constructs this client and google-genai is not a runtime dependency.

Former optional independent reviewer on Google Gemini (google-genai SDK).

Only `structured()` is implemented: the reviewer receives the draft and the
evidence digest and returns structured issues. It has no tools; any external
identifiers it cites are re-retrieved and verified by our own tools before
they can influence the report.
"""

from __future__ import annotations

import json
import os
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings
from .anthropic_client import _json_from_text
from .base import BudgetExceeded, CallRecord, CostTracker, StructuredOutputError, now

T = TypeVar("T", bound=BaseModel)


class GeminiLLM:
    name = "gemini"

    def __init__(self, settings: Settings, cost: CostTracker, client: Any | None = None) -> None:
        self.settings, self.cost, self.model = settings, cost, settings.gemini_model
        if client is None:
            from google import genai

            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not key:
                raise RuntimeError("GEMINI_API_KEY not set")
            client = genai.Client(api_key=key)
        self.client = client

    def structured(self, *, stage: str, system: str, user: str, output_model: type[T], effort: str | None = None) -> T:
        from google.genai import types

        pin, pout = self.settings.gemini_price_in, self.settings.gemini_price_out
        if pin < 0 or pout < 0:
            # fail closed: without a configured price the budget guard cannot bound this request
            raise BudgetExceeded(f"{stage}: Gemini price not configured (AITX_GEMINI_PRICE_IN/OUT_PER_MTOK); request not sent")
        upper = ((len(system) + len(user)) / 3 * pin + 65536 * pout) / 1e6  # 65,536 = conservative output+thinking bound
        token = self.cost.reserve(stage, upper)
        t0 = now()
        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json", response_schema=output_model,
                                                   max_output_tokens=65536),
            )
        except Exception:
            self.cost.settle(token, CallRecord(stage=stage, requested_model=self.model, served_model="unknown", uncertain_billing=True))
            raise
        um = getattr(resp, "usage_metadata", None)
        it = getattr(um, "prompt_token_count", 0) or 0
        ot = (getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
        self.cost.settle(token, CallRecord(stage=stage, requested_model=self.model, served_model=getattr(resp, "model_version", None) or self.model,
                                           input_tokens=it, output_tokens=ot, cost_usd=(it * pin + ot * pout) / 1e6, seconds=round(now() - t0, 2)))
        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, output_model):
            return parsed
        try:
            return output_model.model_validate(_json_from_text(resp.text or ""))
        except (json.JSONDecodeError, ValidationError) as e:
            raise StructuredOutputError(f"{stage}: invalid Gemini output: {e}") from e

    def agent(self, **kw):  # pragma: no cover - not used
        raise NotImplementedError("Gemini is used only as a tool-less reviewer")
