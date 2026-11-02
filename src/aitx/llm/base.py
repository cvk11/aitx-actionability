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
    uncertain_billing: bool = False  # request failed after being sent; cost charged conservatively at its reserved upper bound
    detail: str = ""  # diagnosable accounting detail (token counts etc.), never provider response bodies


def max_prices() -> dict[str, float]:
    """Most expensive per-token rates in the price table (used for unknown/fallback models)."""
    return {k: max(p[k] for p in PRICES_PER_MTOK.values()) for k in ("input", "output", "cache_read", "cache_write")}


@dataclass
class CostTracker:
    """Application-level budget guard (NOT a provider-enforced spending cap).

    Before every request the caller reserves a conservative upper bound of that
    request's cost (`reserve`). Reservations are atomic, so concurrent callers can
    never jointly exceed the budget with reserved amounts. After the request the
    reservation is settled with the measured cost (`settle`). Requests whose
    billing is uncertain (failure after sending) are charged at the reserved
    upper bound. Independent provider-side limits are still required."""

    budget_usd: float = float("inf")
    reserve_usd: float = 0.0  # extra safety margin added to every reservation
    calls: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reserved: dict[int, float] = field(default_factory=dict, repr=False)
    _next_id: int = 0

    @staticmethod
    def price(model: str, inp: int, out: int, cw: int, cr: int) -> float:
        p = PRICES_PER_MTOK.get(model) or max_prices()  # unknown model -> most expensive known rates (conservative)
        return (inp * p["input"] + out * p["output"] + cw * p["cache_write"] + cr * p["cache_read"]) / 1e6

    def reserve(self, stage: str, upper_bound_usd: float) -> int:
        amount = upper_bound_usd + self.reserve_usd
        with self._lock:
            committed = sum(c.cost_usd for c in self.calls) + sum(self._reserved.values())
            if committed + amount > self.budget_usd + 1e-9:
                raise BudgetExceeded(f"{stage}: committed {committed:.2f} USD + request upper bound {amount:.2f} USD would exceed "
                                     f"budget {self.budget_usd:.2f} USD; request not sent")
            self._next_id += 1
            self._reserved[self._next_id] = amount
            return self._next_id

    def settle(self, token: int, rec: CallRecord | None) -> None:
        """Release a reservation and record the actual call (None = nothing billed, e.g. 429 rejected)."""
        with self._lock:
            reserved = self._reserved.pop(token, 0.0)
            if rec is not None:
                if rec.uncertain_billing:
                    rec.cost_usd = max(rec.cost_usd, reserved)
                self.calls.append(rec)

    @property
    def reserved_usd(self) -> float:
        with self._lock:
            return round(sum(self._reserved.values()), 4)

    def record(self, rec: CallRecord) -> None:
        with self._lock:
            self.calls.append(rec)

    @property
    def total_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 4)

    def check(self, stage: str) -> None:
        if self.total_usd + self.reserved_usd + self.reserve_usd > self.budget_usd:
            raise BudgetExceeded(f"spent {self.total_usd:.2f} USD + reserved {self.reserved_usd:.2f} would exceed budget "
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
                "fallback_calls": sum(1 for c in self.calls if c.fallback_used),
                "uncertain_billing_calls": sum(1 for c in self.calls if c.uncertain_billing),
                "budget_usd": self.budget_usd, "guard": "application-level reservation guard; not a provider-enforced cap",
                "calls": [asdict(c) for c in self.calls]}


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
