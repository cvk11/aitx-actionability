from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from fake_http import transport  # noqa: E402

from aitx.config import get_settings  # noqa: E402
from aitx.http import HttpClient  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


@pytest.fixture
def settings():
    # deployment_mode=True -> no disk cache writes during tests
    return get_settings(deployment_mode=True, genereviews_dir=FIX / "genereviews", max_revisions=2)


@pytest.fixture
def fake_http(settings):
    return HttpClient(settings, transport=transport())


@pytest.fixture
def test_case():
    return json.loads((FIX / "test_case.json").read_text())


@pytest.fixture
def official_cases():
    return json.loads((ROOT / "data" / "official_cases" / "aitx_final_patients.json").read_text())
