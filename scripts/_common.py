"""Shared helpers for CLI scripts."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
OFFICIAL = ROOT / "data" / "official_cases" / "aitx_final_patients.json"
INTERNAL = ROOT / "outputs" / "internal"


def load_cases(path: Path = OFFICIAL) -> list[dict]:
    data = json.loads(Path(path).read_text())
    return data if isinstance(data, list) else [data]


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "anthropic", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
