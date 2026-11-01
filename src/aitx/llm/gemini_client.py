"""Optional independent reviewer on Google Gemini (google-genai SDK).

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
from .base import CallRecord, CostTracker, StructuredOutputError, now

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

        self.cost.check(stage)
        t0 = now()
        resp = self.client.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json", response_schema=output_model),
        )
        um = getattr(resp, "usage_metadata", None)
        self.cost.record(CallRecord(stage=stage, requested_model=self.model, served_model=getattr(resp, "model_version", None) or self.model,
                                    input_tokens=getattr(um, "prompt_token_count", 0) or 0,
                                    output_tokens=(getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0),
                                    seconds=round(now() - t0, 2)))
        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, output_model):
            return parsed
        try:
            return output_model.model_validate(_json_from_text(resp.text or ""))
        except (json.JSONDecodeError, ValidationError) as e:
            raise StructuredOutputError(f"{stage}: invalid Gemini output: {e}") from e

    def agent(self, **kw):  # pragma: no cover - not used
        raise NotImplementedError("Gemini is used only as a tool-less reviewer")
