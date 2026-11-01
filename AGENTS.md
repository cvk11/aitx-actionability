# AGENTS.md — instructions for coding agents (e.g., OpenAI Codex) working in this repository

## Ground rules
- **No paid API calls.** Do not call Anthropic, Gemini or OpenAI APIs, and do not set or request API keys. All tests must run offline (`pytest`). Free public biomedical APIs may be used only via `pytest -m live` when explicitly asked.
- **Never hardcode case content.** Do not add gene symbols, transcripts, variants or ids of the official cases (`data/official_cases/`) to `src/`, `prompts/` or `deployment/`. `tests/test_units.py::test_no_official_case_specifics_in_code_or_prompts` enforces this.
- **Do not edit** `data/official/` or `data/official_cases/` (verbatim official files; hashes are tested).
- **Do not weaken** evidence rules to make reports look fuller: citations must come from the evidence ledger; failed retrieval must not be reported as absence; unsupported claims must stay removable.
- Keep secrets out of the repository; `.env` is git-ignored.
- Deployment mode must never log or persist inputs/outputs.

## Commands
```bash
pip install -r requirements.txt && pip install -e .
```
```bash
pytest
```
```bash
pytest -m live
```

## Map
- `src/aitx/pipeline.py` orchestration (full mode and `baseline` arm A); `src/aitx/factory.py` config arms A/B/C.
- `src/aitx/rules.py` deterministic tier/citation rules; `src/aitx/qc.py` template/submission validation; `src/aitx/render.py` Markdown.
- `src/aitx/variant_model.py` HGVS parsing, transcript model, NMD rules; `src/aitx/retrieval/` API clients; `src/aitx/ledger.py` evidence ledger.
- `src/aitx/llm/anthropic_client.py` Claude tool loop / structured outputs / fallback / cost; `src/aitx/agents/` stage logic; `prompts/` templates.
- Tests: `tests/` (`fake_http.py` = offline API fakes for the fictional gene GENEX; `mock_handlers.py` = scripted LLM stage outputs).

## Style
Python ≥3.11, type hints, Pydantic v2 models, no new frameworks. Add an offline test for every behavioural change.
