"""Offline mock LLM for tests and dry runs.

Outputs are produced by caller-supplied handlers per stage prefix. Everything
produced through this client is labelled MOCK by the pipeline and can never be
written to the submission file.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from .base import CallRecord, CostTracker

T = TypeVar("T", bound=BaseModel)
Handler = Callable[[dict[str, Any]], Any]


class MockLLM:
    name = "mock"
    is_mock = True

    def __init__(self, handlers: dict[str, Handler], cost: CostTracker | None = None) -> None:
        self.handlers = handlers
        self.cost = cost or CostTracker()
        self.seen: list[dict[str, Any]] = []

    def _handle(self, stage: str, ctx: dict[str, Any], output_model: type[T]) -> T:
        self.seen.append({"stage": stage, **{k: v for k, v in ctx.items() if k != "toolbox"}})
        self.cost.record(CallRecord(stage=stage, requested_model="mock", served_model="mock"))
        for prefix in sorted(self.handlers, key=len, reverse=True):
            if stage.startswith(prefix):
                out = self.handlers[prefix](ctx)
                return out if isinstance(out, output_model) else output_model.model_validate(out)
        raise KeyError(f"MockLLM: no handler for stage {stage!r}")

    def structured(self, *, stage: str, system: str, user: str, output_model: type[T], effort: str | None = None) -> T:
        return self._handle(stage, {"system": system, "user": user}, output_model)

    def agent(self, *, stage: str, system: str, user: str, output_model: type[T], toolbox: Any, tool_names: list[str] | None,
              max_rounds: int, effort: str | None = None) -> T:
        return self._handle(stage, {"system": system, "user": user, "toolbox": toolbox, "tool_names": tool_names}, output_model)
