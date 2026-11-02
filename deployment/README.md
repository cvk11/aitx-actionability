# Hugging Face Space deployment (prepared, NOT deployed)

Required only for winner verification (https://aitxchallenge.org/models/#top-of-tier). Do not deploy without the owner's explicit authorization.

## Endpoint contract
* Gradio API: `api_name="/actionability_report"` — `gradio_client.Client("<owner>/<space>", token=...).predict(payload, api_name="/actionability_report")` (the call style used by the organizers' validator code, which for Phase 1 used `/answer_question`).
* HTTP: `POST /actionability_report`, JSON body = official input object (or array). Returns `{"id", "report_markdown"}` (or array). Invalid input → Gradio error / HTTP 422. `GET /healthz` reports readiness of the GeneReviews snapshot.

## Build the Space folder
```bash
python scripts/prepare_space.py
```
Creates `outputs/space_bundle/` (`app.py`, `src/`, `prompts/`, the official template, the GeneReviews build script, `requirements.txt`, Space `README.md` card). The script refuses to build if `.env` or official case files would be included.

## Deploy (owner action, after authorization)
1. Create a Space (SDK: Gradio, Python 3.12), public or private.
2. Push the contents of `outputs/space_bundle/` to the Space repository.
3. Space **secrets**: `ANTHROPIC_API_KEY` (required); optional free `NCBI_API_KEY`, `NCBI_EMAIL`. No other model provider key is used. Recommended variables: `AITX_MAX_COST_USD_PER_CASE`, `AITX_MAX_TOOL_ROUNDS` — **identical to the settings used for the submitted reports** (see `outputs/run_manifest.json`).
4. On start, the app builds the GeneReviews snapshot from NCBI's archive in the background (~1–2 min); requests wait for it.
5. If private: create a fine-grained, read-only token limited to this Space for the organizers.
6. Register the Space in https://huggingface.co/spaces/aitxchallenge/aitxchallenge_submission and run the validator.

## No-logging policy (organizer requirement)
`SPACE_ID` present ⇒ `AITX_DEPLOYMENT_MODE=true`: no HTTP disk cache, no audit/report files, logging level WARNING with no payload content, uvicorn access log off, Gradio analytics/run history/monitoring/flagging/MCP off. Evaluation inputs and outputs are never stored, redistributed or used for training.

## Local test (offline)
`pytest tests/test_deployment.py` launches the app with a mock pipeline and calls it via `gradio_client` and HTTP.

## Open questions for the organizers
Validator call style and timeout for `/actionability_report`; single object vs array; tolerance for differences caused by live records (e.g., trial status changes).
