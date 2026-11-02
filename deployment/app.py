"""Hugging Face Space: `/actionability_report` endpoint (winner-verification stage).

Exposes the SAME pipeline used to produce the submission:
  * Gradio API endpoint `api_name="/actionability_report"`
      -> gradio_client: Client(space).predict(payload, api_name="/actionability_report")
  * plain HTTP:  POST /actionability_report  (JSON body = official input object)
Input: official input object {"id", "patient": {"genotype": [...], "clinical_context"}}
       (a JSON array of such objects is also accepted and returns an array).
Output: {"id": <same id>, "report_markdown": <markdown>}

No-logging policy (required by the organizers): evaluation inputs and outputs
are never logged, written to disk, cached on disk, or reused. Deployment mode
is forced when running on a Space (SPACE_ID set) or AITX_DEPLOYMENT_MODE=true.
Secrets (GEMINI_API_KEY; optional free NCBI_API_KEY/NCBI_EMAIL) are read
from Space secrets only.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

_here = Path(__file__).resolve()
ROOT = next((p for p in [_here.parent, *_here.parents] if (p / "src" / "aitx").is_dir()), _here.parents[1])
sys.path.insert(0, str(ROOT / "src"))

if os.environ.get("SPACE_ID"):
    os.environ["AITX_DEPLOYMENT_MODE"] = "true"
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

from pydantic import ValidationError  # noqa: E402

from aitx.config import get_settings  # noqa: E402
from aitx.schemas import PatientCase  # noqa: E402

logging.basicConfig(level=logging.WARNING)
if os.environ.get("AITX_DEPLOYMENT_MODE", "").lower() in ("1", "true", "yes", "on"):
    # pipeline/tool log messages can contain gene/variant/clinical text: silence them entirely in deployment
    for name in ("aitx", "httpx", "httpcore", "google_genai", "google.genai"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
log = logging.getLogger("aitx.deployment")


class ReportUnavailable(RuntimeError):
    """Raised when the pipeline could not complete a fully verified report (no partial report is returned)."""


def _safe_validation_message(e: ValidationError) -> str:
    # field locations only — never echo submitted values (patient data)
    locs = sorted({".".join(str(x) for x in err.get("loc", ())) for err in e.errors()})
    return "Invalid input at: " + ", ".join(locs[:10])

_gr_lock = threading.Lock()
_gr_ready = threading.Event()


def ensure_genereviews_snapshot() -> None:
    """Build the GeneReviews snapshot once at startup (public NCBI archive; no patient data involved)."""
    s = get_settings()
    with _gr_lock:
        if (s.genereviews_dir / "MANIFEST.json").exists():
            _gr_ready.set()
            return
        import subprocess

        try:
            subprocess.run([sys.executable, str(ROOT / "scripts" / "build_genereviews_index.py"), "--out", str(s.genereviews_dir)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
        except Exception as e:  # pipeline still runs; GeneReviews tools then report "not available"
            log.warning("GeneReviews snapshot build failed: %s", type(e).__name__)
        _gr_ready.set()


def _pipeline_factory():
    """Overridable in tests."""
    from aitx.factory import build_pipeline

    return build_pipeline("default")


def _run_one(obj: dict[str, Any]) -> dict[str, Any]:
    case = PatientCase.model_validate(obj)  # raises ValidationError on malformed input
    _gr_ready.wait(timeout=3600)
    try:
        result = _pipeline_factory().run(case)  # nothing is persisted in deployment mode
    except Exception as e:  # never surface exception text (may contain patient data)
        raise ReportUnavailable(f"report generation failed ({type(e).__name__})") from None
    blockers = [b["code"] for b in result.blockers if b["code"] != "mock_output"]
    if blockers:
        raise ReportUnavailable("report incomplete or not fully verified (" + ", ".join(sorted(set(blockers))) + "); please retry")
    return result.output.model_dump()


def actionability_report(payload: Any) -> Any:
    """Official input object (or array of objects) -> official output object (or array)."""
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    if isinstance(payload, list):
        return [_run_one(o) for o in payload]
    if not isinstance(payload, dict):
        raise ValueError("Input must be a JSON object following the AI-Tx input format")
    return _run_one(payload)


def build_app():
    import gradio as gr
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import JSONResponse

    def gr_fn(payload):
        try:
            return actionability_report(payload)
        except ValidationError as e:
            raise gr.Error(_safe_validation_message(e))
        except ReportUnavailable as e:
            raise gr.Error(str(e))
        except ValueError:
            raise gr.Error("Invalid input: expected a JSON object (or array) in the AI-Tx input format")

    demo = gr.Interface(
        fn=gr_fn, inputs=gr.JSON(label="AI-Tx input object"), outputs=gr.JSON(label="AI-Tx output object"),
        api_name="actionability_report", flagging_mode="never", analytics_enabled=False, concurrency_limit=2,
        title="AI-Tx Therapeutic Actionability Report", description="POST the official AI-Tx input JSON; returns {id, report_markdown}.",
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/actionability_report")
    def http_endpoint(payload: Any = Body(...)):
        try:
            return JSONResponse(actionability_report(payload))
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=_safe_validation_message(e))
        except ReportUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid input: expected a JSON object (or array) in the AI-Tx input format")

    @app.get("/healthz")
    def health():
        return {"ok": True, "genereviews_ready": _gr_ready.is_set()}

    return gr.mount_gradio_app(app, demo, path="/", run_history=False, enable_monitoring=False, mcp_server=False, ssr_mode=False), demo


if __name__ == "__main__":
    import uvicorn

    threading.Thread(target=ensure_genereviews_snapshot, daemon=True).start()
    app, _ = build_app()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)), access_log=False, log_level="warning")
