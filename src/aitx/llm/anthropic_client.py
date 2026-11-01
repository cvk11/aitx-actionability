"""Claude client: structured outputs + client-side tool loop.

Design notes (verified against the Anthropic API reference bundled with Claude
Code, cached 2026-10-06, and the installed SDK anthropic==2.3.0):

* model pinned to an exact identifier (default ``claude-opus-5-5``; this ID has
  no dated-snapshot variant and is not an alias)
* thinking is adaptive (always on for Opus 5.5); depth via ``output_config.effort``
* no ``temperature``/``top_p`` (rejected by the 2026-10-21 API version)
* forced ``tool_choice`` is rejected on Opus 5.5 -> ``auto`` + strict tools +
  ``output_config.format`` (JSON schema) for the final answer
* streaming + ``get_final_message()`` for long outputs
* refusal handling via server-side ``fallbacks: "default"`` (beta header
  ``server-side-fallback-2026-07-01``); the served model of every call is recorded
* top-level automatic prompt caching (stable system prompt + tool list first)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import PRICES_PER_MTOK, Settings
from .base import CallRecord, CostTracker, ModelRefusal, StructuredOutputError, now

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _json_from_text(text: str) -> Any:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.+?)\s*```$", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


class ClaudeLLM:
    name = "claude"

    def __init__(self, settings: Settings, cost: CostTracker, client: Any | None = None, model: str | None = None) -> None:
        self.settings = settings
        self.cost = cost
        self.model = model or settings.primary_model
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=4, timeout=900.0)
        self.client = client

    # ------------------------------------------------------------------ core
    def _call(self, *, stage: str, system: str, messages: list[dict[str, Any]], output_model: type[BaseModel] | None,
              tools: list[dict[str, Any]] | None, effort: str | None, tool_choice: dict[str, Any] | None = None) -> Any:
        import anthropic

        self.cost.check(stage)
        output_config: dict[str, Any] = {"effort": effort or self.settings.effort}
        if output_model is not None:
            output_config["format"] = {"type": "json_schema", "schema": anthropic.transform_schema(output_model)}
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=self.settings.max_tokens,
            system=[{"type": "text", "text": system}],
            messages=messages, thinking={"type": "adaptive"}, output_config=output_config,
            cache_control={"type": "ephemeral"},
        )
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        t0 = now()
        if self.settings.refusal_fallback:
            with self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs) as stream:
                msg = stream.get_final_message()
        else:
            with self.client.messages.stream(**kwargs) as stream:
                msg = stream.get_final_message()
        u = msg.usage
        served = getattr(msg, "model", self.model) or self.model
        inp, out = getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0
        cw, cr = getattr(u, "cache_creation_input_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0
        fallback_used = any(getattr(b, "type", "") == "fallback" for b in msg.content) or served != self.model
        self.cost.record(CallRecord(stage=stage, requested_model=self.model, served_model=served, input_tokens=inp, output_tokens=out,
                                    cache_creation_input_tokens=cw, cache_read_input_tokens=cr,
                                    cost_usd=CostTracker.price(served if served in PRICES_PER_MTOK else self.model, inp, out, cw, cr),
                                    stop_reason=str(msg.stop_reason), seconds=round(now() - t0, 2),
                                    request_id=getattr(msg, "_request_id", "") or "", fallback_used=fallback_used))
        if msg.stop_reason == "refusal":
            sd = getattr(msg, "stop_details", None)
            raise ModelRefusal(stage, getattr(sd, "category", None), getattr(sd, "explanation", None))
        return msg

    @staticmethod
    def _text(msg: Any) -> str:
        return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")

    def _parse(self, stage: str, msg: Any, output_model: type[T]) -> T:
        if msg.stop_reason == "max_tokens":
            raise StructuredOutputError(f"{stage}: output truncated at max_tokens")
        raw = self._text(msg)
        try:
            return output_model.model_validate(_json_from_text(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            raise StructuredOutputError(f"{stage}: invalid structured output: {e}; head={raw[:300]!r}") from e

    # -------------------------------------------------------------- public
    def structured(self, *, stage: str, system: str, user: str, output_model: type[T], effort: str | None = None) -> T:
        msgs = [{"role": "user", "content": user}]
        msg = self._call(stage=stage, system=system, messages=msgs, output_model=output_model, tools=None, effort=effort)
        try:
            return self._parse(stage, msg, output_model)
        except StructuredOutputError as e:
            log.warning("%s; retrying once", e)
            msg = self._call(stage=stage + ":retry", system=system, messages=msgs, output_model=output_model, tools=None, effort=effort)
            return self._parse(stage, msg, output_model)

    def agent(self, *, stage: str, system: str, user: str, output_model: type[T], toolbox: Any, tool_names: list[str] | None,
              max_rounds: int, effort: str | None = None) -> T:
        tools = toolbox.specs(tool_names)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        for round_i in range(max_rounds + 1):
            final_round = round_i == max_rounds
            msg = self._call(stage=f"{stage}:r{round_i}", system=system, messages=messages, output_model=output_model, tools=tools,
                             effort=effort, tool_choice={"type": "none"} if final_round else None)
            uses = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
            if msg.stop_reason != "tool_use" or not uses:
                return self._parse(stage, msg, output_model)
            messages.append({"role": "assistant", "content": msg.content})
            results = []
            for b in uses:
                text, is_err = toolbox.execute(b.name, dict(b.input))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": text, "is_error": is_err})
            if round_i + 1 == max_rounds:
                results.append({"type": "text", "text": "Tool budget reached. Do not call more tools. Return the final JSON answer now, "
                                                        "using only evidence already retrieved and stating remaining gaps."})
            messages.append({"role": "user", "content": results})
        raise StructuredOutputError(f"{stage}: no final answer after {max_rounds} rounds")  # pragma: no cover
