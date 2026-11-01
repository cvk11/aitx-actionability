"""Endpoint test: launches the Space app locally (mock pipeline, fake HTTP) and calls it
exactly like the organizers' validator does (gradio_client .predict) plus plain HTTP POST."""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_app():
    spec = importlib.util.spec_from_file_location("space_app", ROOT / "deployment" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def space(settings, fake_http, monkeypatch):
    from mock_handlers import HANDLERS

    from aitx.llm.mock import MockLLM
    from aitx.pipeline import Pipeline

    mod = _load_app()
    monkeypatch.setattr(mod, "_pipeline_factory", lambda: Pipeline(MockLLM(HANDLERS), settings, http=fake_http, parallel=False))
    mod._gr_ready.set()
    app, _ = mod.build_app()
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield mod, f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(timeout=10)


def test_function_contract(space, test_case):
    mod, _ = space
    out = mod.actionability_report(test_case)
    assert set(out) == {"id", "report_markdown"} and out["id"] == "TEST-0001"
    assert isinstance(mod.actionability_report([test_case, test_case]), list)
    with pytest.raises(Exception):
        mod.actionability_report({"id": "X"})


def test_http_post(space, test_case):
    _, base = space
    r = httpx.post(f"{base}/actionability_report", json=test_case, timeout=60)
    assert r.status_code == 200 and r.json()["id"] == "TEST-0001"
    assert httpx.post(f"{base}/actionability_report", json={"id": "X"}, timeout=30).status_code == 422
    assert httpx.get(f"{base}/healthz").json()["ok"]


def test_gradio_client_predict(space, test_case):
    from gradio_client import Client

    _, base = space
    client = Client(base + "/", verbose=False)
    out = client.predict(test_case, api_name="/actionability_report")
    assert isinstance(out, dict) and out["id"] == "TEST-0001" and out["report_markdown"].startswith("# Therapeutic Actionability Report for Patient TEST-0001")
