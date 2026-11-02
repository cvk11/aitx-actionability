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

Model (pinned): `claude-opus-5-5` (research, verification and report generation). No other model provider is used.
No-logging policy: inputs/outputs are never logged, stored, cached to disk, redistributed, or used for training.
Secrets required: `ANTHROPIC_API_KEY` (optional free `NCBI_API_KEY`, `NCBI_EMAIL`).
