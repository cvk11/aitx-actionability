"""Argon-only client with explicit tool history and per-request budget reservations.

No SDK retries, automatic function execution, continuation, storage or fallback.
Synchronous bounded calls avoid consuming partial streams as complete evidence.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from typing import Any, TypeVar

from google.genai import types
from pydantic import BaseModel, ValidationError

from ..config import Settings
from .base import CallRecord, CostTracker, StructuredOutputError, now

T = TypeVar('T', bound=BaseModel)
MODEL = 'gemini-4-argon'
CONTEXT_LIMIT = 1048576  # unified input+output token limit documented for the model; the client checks bytes conservatively


def argon_prices(day: date | None = None) -> tuple[float, float, float]:
    """Official USD/Mtoken schedule, checked 2026-11-02; cached input is 5%."""
    day = day or datetime.now(timezone.utc).date()
    inp, out = (2.0, 10.0) if day < date(2026, 12, 1) else (4.0, 20.0)
    return inp, out, inp * 0.05


class GeminiLLM:
    name = 'gemini'

    def __init__(self, settings: Settings, cost: CostTracker, client: Any = None, model: str | None = None):
        self.settings, self.cost = settings, cost
        self.model = model or settings.primary_model
        if self.model != MODEL or settings.refusal_fallback:
            raise ValueError('Only gemini-4-argon without provider fallback is supported')
        if client is None:
            from google import genai
            key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
            if not key:
                raise RuntimeError('GEMINI_API_KEY or GOOGLE_API_KEY is required')
            client = genai.Client(api_key=key, http_options=types.HttpOptions(
                timeout=900000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.client = client
        self.compacted_rounds = 0  # conversation rounds dropped by the bounded history compaction

    def _input_bound(self, contents, config) -> int:
        """Conservative serialized-request size: UTF-8 bytes of the request payload plus fixed overhead.
        This is an application estimate (bytes overestimate tokens), not a tokenizer count."""
        payload = json.dumps({'contents': [c.model_dump(mode='json', exclude_none=True) for c in contents],
                              'config': config.model_dump(mode='json', exclude_none=True)}, ensure_ascii=False)
        return len(payload.encode('utf-8')) + 4096

    @staticmethod
    def _compact(contents, keep_pairs: int):
        """Drop the oldest conversation rounds, keeping the original user message and the most recent
        `keep_pairs` model/user pairs (roles stay alternating). Returns None when there is nothing to drop.
        The dropped rounds are conversation state only: retrieved evidence remains in the evidence ledger,
        and the verifier re-grounds every claim against the ledger, so this cannot erase evidence."""
        if len(contents) <= 1 + 2 * keep_pairs:
            return None
        return [contents[0]] + contents[-(2 * keep_pairs):]

    def _fit(self, stage: str, contents, config):
        """Trim old rounds until the conservative context bound holds; raise (fail closed) if even the
        last conversation pair does not fit. The threshold mirrors `_send` (input bound + max_tokens)."""
        keep = 4
        while self._input_bound(contents, config) + self.settings.max_tokens > CONTEXT_LIMIT:
            trimmed = self._compact(contents, keep)
            if trimmed is None:
                keep -= 1
                if keep < 1:
                    raise StructuredOutputError(f'{stage}: conservative context limit exceeded')
                continue
            contents = trimmed
            self.compacted_rounds += 1
        return contents

    def _config(self, system, effort, output_model=None, specs=None):
        level = effort or self.settings.effort
        if level not in {'low', 'medium', 'high'}:
            raise ValueError('Argon thinking level must be low, medium or high')
        kw = dict(system_instruction=system, max_output_tokens=self.settings.max_tokens,
                  thinking_config=types.ThinkingConfig(thinking_level=level), store=False, service_tier=types.ServiceTier.STANDARD,
                  automatic_continuation=False,
                  automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        if specs:
            kw['tools'] = [types.Tool(function_declarations=[types.FunctionDeclaration(
                name=s['name'], description=s['description'], parameters_json_schema=s['input_schema']) for s in specs])]
        if output_model is not None:
            kw.update(response_mime_type='application/json', response_json_schema=output_model.model_json_schema())
        return types.GenerateContentConfig(**kw)

    def _send(self, stage, contents, config):
        # Text-only pipeline. Byte count plus overhead intentionally overestimates
        # ordinary tokenizer input; this is an application estimate, not a quota.
        input_bound = self._input_bound(contents, config)
        if input_bound + self.settings.max_tokens > CONTEXT_LIMIT:
            raise StructuredOutputError(f'{stage}: conservative context limit exceeded')
        for attempt in range(self.settings.max_attempts):
            pin, pout, cache_price = argon_prices()
            upper = (input_bound * pin + self.settings.max_tokens * pout) / 1e6
            token = self.cost.reserve(stage, upper)
            start = now()
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except Exception as exc:
                code = getattr(exc, 'code', None)
                code = code if isinstance(code, int) else None
                # Error bodies are deliberately never logged or propagated.
                rejected = code in (400, 401, 403, 404, 429)
                self.cost.settle(token, None if rejected else CallRecord(
                    stage=stage, requested_model=self.model, served_model='unknown',
                    stop_reason='provider_error', uncertain_billing=True))
                if (code == 429 or isinstance(code, int) and code >= 500) and attempt + 1 < self.settings.max_attempts:
                    time.sleep(min(8, 2 ** attempt))
                    continue
                raise StructuredOutputError(f'{stage}: Gemini request failed ({type(exc).__name__}, code={code})') from None
            served = getattr(resp, 'model_version', None) or 'unknown'
            um = getattr(resp, 'usage_metadata', None)
            try:
                if um is None:
                    raise ValueError('usage metadata absent')
                inp, out = um.prompt_token_count, um.candidates_token_count
                if inp is None or out is None:
                    raise ValueError('prompt/candidates token counts absent')
                inp, out = int(inp), int(out)
                thought, cached = int(um.thoughts_token_count or 0), int(um.cached_content_token_count or 0)
                tool_input = int(um.tool_use_prompt_token_count or 0)
                if min(inp, out, thought, cached, tool_input) < 0:
                    raise ValueError('negative token count')
                # Google documents prompt_token_count as INCLUDING cached tokens, but the live
                # service has been observed reporting cached_content_token_count above
                # prompt_token_count on tool-heavy turns (prompt then excludes the cached prefix).
                # Accept both shapes without double counting: fresh prompt tokens and tool-result
                # tokens at the input rate, the cached prefix at the cache rate. An anomalous shape
                # is flagged as uncertain billing (settled at the conservative reserved bound).
                anomaly = cached > inp
                fresh = inp - cached if not anomaly else inp
                bill = ((fresh + tool_input) * pin + cached * cache_price + (out + thought) * pout) / 1e6
                known = served == MODEL or served.startswith(MODEL + '-')
                rec = CallRecord(stage=stage, requested_model=self.model, served_model=served,
                    input_tokens=fresh + tool_input, output_tokens=out + thought, cache_read_input_tokens=cached,
                    cost_usd=bill, seconds=round(now() - start, 2), uncertain_billing=(not known) or anomaly)
            except (ValueError, TypeError, AttributeError) as e:
                # Token counts are accounting data, not provider response bodies or model text; they are
                # included so a future shape change is diagnosable from the audit without a paid probe.
                detail = (f'{e}; prompt={getattr(um, "prompt_token_count", None)} '
                          f'candidates={getattr(um, "candidates_token_count", None)} '
                          f'cached={getattr(um, "cached_content_token_count", None)} '
                          f'thoughts={getattr(um, "thoughts_token_count", None)} '
                          f'tool_use={getattr(um, "tool_use_prompt_token_count", None)}')
                self.cost.settle(token, CallRecord(stage=stage, requested_model=self.model, served_model=served,
                    stop_reason='invalid_usage', uncertain_billing=True, detail=detail))
                if attempt + 1 < self.settings.max_attempts:
                    # A response whose billing cannot be verified is unusable (fail-closed), but a single
                    # transient metadata omission must not kill the stage: retry like a provider error.
                    time.sleep(min(8, 2 ** attempt))
                    continue
                raise StructuredOutputError(
                    f'{stage}: missing or invalid billing metadata after retries ({detail})') from None
            self.cost.settle(token, rec)
            if not known:
                raise StructuredOutputError(f'{stage}: unrecognized served model; no fallback allowed')
            if bill > upper + 1e-9:
                raise StructuredOutputError(f'{stage}: actual usage exceeded reserved estimate')
            candidates = getattr(resp, 'candidates', None) or []
            if len(candidates) != 1 or candidates[0].finish_reason != types.FinishReason.STOP or not candidates[0].content:
                raise StructuredOutputError(f'{stage}: missing, blocked, truncated or incomplete Gemini response')
            return candidates[0].content
        raise StructuredOutputError(f'{stage}: retry budget exhausted')

    @staticmethod
    def _parse(stage, content, output_model):
        if any(p.function_call for p in (content.parts or [])):
            raise StructuredOutputError(f'{stage}: unexpected tool call in structured output')
        text = ''.join(p.text or '' for p in (content.parts or []) if not p.thought)
        try:
            return output_model.model_validate_json(text)
        except (ValidationError, ValueError):
            raise StructuredOutputError(f'{stage}: invalid structured Gemini output') from None

    def structured(self, *, stage: str, system: str, user: str, output_model: type[T], effort=None) -> T:
        contents = [types.Content(role='user', parts=[types.Part(text=user)])]
        content = self._send(stage, contents, self._config(system, effort, output_model))
        return self._parse(stage, content, output_model)

    def agent(self, *, stage: str, system: str, user: str, output_model: type[T], toolbox: Any,
              tool_names: list[str] | None, max_rounds: int, effort=None) -> T:
        specs = toolbox.specs(tool_names, strict=False)
        allowed = {s['name'] for s in specs}
        contents = [types.Content(role='user', parts=[types.Part(text=user)])]
        for n in range(max_rounds):
            cfg = self._config(system, effort, specs=specs)
            # Long tool loops can grow the serialized history past the conservative context
            # bound; trim the oldest rounds (evidence stays in the ledger) before sending.
            contents = self._fit(f'{stage}:r{n}', contents, cfg)
            content = self._send(f'{stage}:r{n}', contents, cfg)
            # Preserve every part, including opaque thought signatures and call IDs.
            contents.append(content)
            calls = [p.function_call for p in (content.parts or []) if p.function_call]
            if not calls:
                break
            responses = []
            for call in calls:
                if call.name not in allowed or not isinstance(call.args, dict):
                    raise StructuredOutputError(f'{stage}: invalid or disallowed tool call')
                text, error = toolbox.execute(call.name, call.args)
                responses.append(types.Part(function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response={'result': text, 'is_error': error})))
            contents.append(types.Content(role='user', parts=responses))
        # Separate schema-only finalization avoids mixing tools with structured
        # output constraints; the original evidence-bearing history is preserved.
        final_contents = contents + [types.Content(role='user', parts=[types.Part(text=
            'Return the final JSON using only retrieved evidence. Do not invent missing evidence; state gaps. Tool use is now disabled.')])]
        final_cfg = self._config(system, effort, output_model)
        final_contents = self._fit(stage + ':final', final_contents, final_cfg)
        content = self._send(stage + ':final', final_contents, final_cfg)
        if any(p.function_call for p in (content.parts or [])):
            raise StructuredOutputError(f'{stage}: tool call after tool budget exhausted')
        return self._parse(stage, content, output_model)
