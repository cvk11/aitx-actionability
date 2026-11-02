"""Offline guards for the PREPARED live Argon validation (no network, no cost, no key).

These tests verify the paid gate and the cost bound only; they never construct a
provider client or send a request.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import argon_live_check  # noqa: E402


def test_no_key_refuses_before_anything(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert argon_live_check.main(["--authorize-paid"]) == 2
    assert "No request was sent" in capsys.readouterr().out


def test_key_without_authorization_prints_plan_and_refuses(monkeypatch, capsys):
    import google.genai

    monkeypatch.setenv("GEMINI_API_KEY", "offline-placeholder")
    monkeypatch.setattr(google.genai, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no client may be built")))
    assert argon_live_check.main([]) == 3
    out = capsys.readouterr().out
    assert "NOT AUTHORIZED" in out
    assert "gemini-4-argon" in out
    assert "upper bound" in out


def test_cost_bound_matches_manual_sum():
    from aitx.llm.gemini_client import argon_prices

    pin, pout, _ = argon_prices()
    expected = sum(((s.token_bound_input * pin + s.token_bound_output * pout) / 1e6) for s in argon_live_check.STEPS if s.billed)
    assert argon_live_check.cost_bound() == pytest.approx(expected)
    # sanity: the billed steps exist and every step has a non-negative bound
    assert sum(1 for s in argon_live_check.STEPS if s.billed) >= 4
    assert all(s.token_bound_input >= 0 and s.token_bound_output >= 0 for s in argon_live_check.STEPS)


def test_mini_toolbox_roundtrip_and_invalid_input():
    tb = argon_live_check.MiniToolbox()
    assert tb.execute("double", {"value": 21}) == ("42", False)
    assert tb.execute("double", {"value": "x"})[1] is True  # is_error
    assert tb.execute("unknown", {"value": 1})[1] is True
    assert [s["name"] for s in tb.specs(None)] == ["double"]
