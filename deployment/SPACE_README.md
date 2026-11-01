---
title: AI-Tx Actionability Report
sdk: gradio
sdk_version: 6.31.0
python_version: "3.12"
app_file: app.py
pinned: false
---

# AI-Tx therapeutic actionability report endpoint

`/actionability_report` accepts the official AI-Tx input object and returns `{"id", "report_markdown"}`.

* gradio_client: `Client("<owner>/<space>").predict(payload, api_name="/actionability_report")`
* HTTP: `POST /actionability_report` with the JSON input object

Models (pinned): `claude-opus-5-5` (primary + verifier); optional reviewer `gemini-4-argon`.
No-logging policy: inputs/outputs are never logged, stored, cached to disk, redistributed, or used for training.
Secrets required: `ANTHROPIC_API_KEY` (optional `GEMINI_API_KEY`, `NCBI_API_KEY`, `NCBI_EMAIL`).
