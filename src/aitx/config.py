"""Runtime configuration.

All settings come from environment variables (optionally loaded from a local
``.env`` file). Secrets are never written to disk or logs by this package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:  # optional dependency; absent in minimal deployments
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except Exception:  # pragma: no cover
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw or default


# USD per million tokens (first-party Claude API list prices, cached 2026-10-06
# in the Anthropic API reference; Gemini price is a placeholder that must be
# checked on the provider's pricing page before budgeting).
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}

FORBIDDEN_MODEL_SUBSTRINGS = ("latest",)


@dataclass(frozen=True)
class Settings:
    primary_model: str = field(default_factory=lambda: _str("AITX_PRIMARY_MODEL", "gemini-4-argon"))
    verifier_model: str = field(default_factory=lambda: _str("AITX_VERIFIER_MODEL", "gemini-4-argon"))
    gemini_model: str = field(default_factory=lambda: _str("AITX_GEMINI_MODEL", "gemini-4-argon"))
    effort: str = field(default_factory=lambda: _str("AITX_EFFORT", "high"))
    verifier_effort: str = field(default_factory=lambda: _str("AITX_VERIFIER_EFFORT", "medium"))
    enable_gemini: bool = True  # sole active provider
    # off by default: a fallback could serve a turn with another model; enable only when that is acceptable and accounted for
    refusal_fallback: bool = field(default_factory=lambda: _bool("AITX_REFUSAL_FALLBACK", False))
    max_tool_rounds: int = field(default_factory=lambda: _int("AITX_MAX_TOOL_ROUNDS", 24))
    max_revisions: int = field(default_factory=lambda: _int("AITX_MAX_REVISIONS", 2))
    max_cost_usd_per_case: float = field(default_factory=lambda: _float("AITX_MAX_COST_USD_PER_CASE", 12.0))
    call_reserve_usd: float = field(default_factory=lambda: _float("AITX_CALL_RESERVE_USD", 0.25))
    max_attempts: int = field(default_factory=lambda: _int("AITX_MAX_ATTEMPTS", 3))
    gemini_price_in: float = field(default_factory=lambda: _float("AITX_GEMINI_PRICE_IN_PER_MTOK", -1.0))
    gemini_price_out: float = field(default_factory=lambda: _float("AITX_GEMINI_PRICE_OUT_PER_MTOK", -1.0))
    max_cost_usd_total: float = field(default_factory=lambda: _float("AITX_MAX_COST_USD_TOTAL", 30.0))
    max_tokens: int = field(default_factory=lambda: _int("AITX_MAX_TOKENS", 32768))
    tool_result_char_limit: int = field(default_factory=lambda: _int("AITX_TOOL_RESULT_CHARS", 14000))
    deployment_mode: bool = field(default_factory=lambda: _bool("AITX_DEPLOYMENT_MODE", False))
    cache_dir: Path = field(default_factory=lambda: PROJECT_ROOT / _str("AITX_CACHE_DIR", "data/cache"))
    genereviews_dir: Path = field(default_factory=lambda: PROJECT_ROOT / _str("AITX_GENEREVIEWS_DIR", "data/genereviews"))
    ncbi_api_key: str = field(default_factory=lambda: os.environ.get("NCBI_API_KEY", "").strip(), repr=False)
    ncbi_email: str = field(default_factory=lambda: os.environ.get("NCBI_EMAIL", "").strip(), repr=False)
    ncbi_tool: str = field(default_factory=lambda: _str("NCBI_TOOL", "aitx-actionability"))
    http_timeout_s: float = field(default_factory=lambda: _float("AITX_HTTP_TIMEOUT", 30.0))
    log_level: str = field(default_factory=lambda: _str("AITX_LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        for m in (self.primary_model, self.verifier_model, self.gemini_model):
            if any(s in m.lower() for s in FORBIDDEN_MODEL_SUBSTRINGS):
                raise ValueError(f"Mutable model alias not allowed (pin an exact version): {m!r}")
        if not 1 <= self.max_tokens <= 65536 or not 1 <= self.max_attempts <= 5:
            raise ValueError("Invalid token or retry limit")
        if self.effort not in {"low", "medium", "high"} or self.verifier_effort not in {"low", "medium", "high"}:
            raise ValueError(f"Invalid effort {self.effort!r}")

    @property
    def disk_cache_enabled(self) -> bool:
        # Evaluation queries must not be stored in deployment mode.
        return not self.deployment_mode

    @property
    def persist_artifacts(self) -> bool:
        return not self.deployment_mode

    def public_dict(self) -> dict:
        """Configuration snapshot without secrets (for provenance records)."""
        d = asdict(self)
        for secret in ("ncbi_api_key", "ncbi_email"):
            d[secret] = bool(d[secret])
        d["cache_dir"] = str(self.cache_dir.relative_to(PROJECT_ROOT)) if self.cache_dir.is_relative_to(PROJECT_ROOT) else "<external>"
        d["genereviews_dir"] = (
            str(self.genereviews_dir.relative_to(PROJECT_ROOT)) if self.genereviews_dir.is_relative_to(PROJECT_ROOT) else "<external>"
        )
        d["gemini_api_key_present"] = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        return d


def get_settings(**overrides) -> Settings:
    s = Settings()
    if overrides:
        from dataclasses import replace

        s = replace(s, **overrides)
    return s
