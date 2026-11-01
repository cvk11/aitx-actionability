"""LLM abstraction, cost/token accounting, and errors."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ..config import PRICES_PER_MTOK

T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(RuntimeError):
    pass


class ModelRefusal(RuntimeError):
    def __init__(self, stage: str, category: str | None, explanation: str | None) -> None:
        super().__init__(f"model refusal in {stage}: {category} {explanation or ''}")
        self.stage, self.category, self.explanation = stage, category, explanation


class StructuredOutputError(RuntimeError):
    pass


@dataclass
class CallRecord:
    stage: str
    requested_model: str
    served_model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str = ""
    seconds: float = 0.0
    request_id: str = ""
    fallback_used: bool = False


@dataclass
class CostTracker:
    budget_usd: float = float("inf")
    # A call is refused if spent + reserve would exceed the budget; the reserve
    # approximates the worst-case cost of one more call (prevents overshoot).
    reserve_usd: float = 0.0
    calls: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @staticmethod
    def price(model: str, inp: int, out: int, cw: int, cr: int) -> float:
        p = PRICES_PER_MTOK.get(model)
        if p is None:
            return 0.0  # unknown price (e.g. Gemini) -> reported as tokens only
        return (inp * p["input"] + out * p["output"] + cw * p["cache_write"] + cr * p["cache_read"]) / 1e6

    def record(self, rec: CallRecord) -> None:
        with self._lock:
            self.calls.append(rec)

    @property
    def total_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 4)

    def check(self, stage: str) -> None:
        if self.total_usd + self.reserve_usd > self.budget_usd:
            raise BudgetExceeded(f"spent {self.total_usd:.2f} USD + reserve {self.reserve_usd:.2f} would exceed budget "
                                 f"{self.budget_usd:.2f} USD before {stage}")

    def summary(self) -> dict[str, Any]:
        by_stage: dict[str, dict[str, float]] = {}
        for c in self.calls:
            s = by_stage.setdefault(c.stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0})
            s["calls"] += 1
            s["input_tokens"] += c.input_tokens
            s["output_tokens"] += c.output_tokens
            s["cache_read"] += c.cache_read_input_tokens
            s["cache_write"] += c.cache_creation_input_tokens
            s["usd"] = round(s["usd"] + c.cost_usd, 4)
        return {"total_usd": self.total_usd, "n_calls": len(self.calls), "by_stage": by_stage,
                "served_models": sorted({c.served_model for c in self.calls}),
                "fallback_calls": sum(1 for c in self.calls if c.fallback_used), "calls": [asdict(c) for c in self.calls]}


class LLM(Protocol):
    """Interface implemented by the Claude, Gemini and mock clients."""

    name: str

    def structured(self, *, stage: str, system: str, user: str, output_model: type[T], effort: str | None = None) -> T: ...

    def agent(
        self, *, stage: str, system: str, user: str, output_model: type[T], toolbox: Any, tool_names: list[str] | None,
        max_rounds: int, effort: str | None = None,
    ) -> T: ...


def now() -> float:
    return time.monotonic()
