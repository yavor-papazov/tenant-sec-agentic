# tenant-sec-agentic

Python implementation of the **tenant-sec agentic assessment pipeline** described in the product requirements: multi-agent doc retrieval, scoring, skeptic/consistency review, optional deep research, and deterministic artifacts (per-control YAML + HTML + dashboard).

**Canonical data model:** [ypapazov/tenant-sec](https://github.com/ypapazov/tenant-sec). This repo only consumes that layout (`controls/`, `providers/`, `schema/`, `METHODOLOGY.md` when present).

## Requirements

- Python 3.12+
- A checkout of `tenant-sec` on disk; set `paths.repo_root` in the config to that directory
- API keys (for non-mock runs):
  - **LiteLLM / models:** e.g. `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or other provider env vars your model strings need ([LiteLLM providers](https://docs.litellm.ai/docs/providers))
  - **Search:** `TAVILY_API_KEY` for the `web_search` tool (or extend `web_tools.py`)

## Install

```bash
pip install -e .
```

## Configure

Copy `assessment-config.example.yaml` to `assessment-config.yaml` (gitignored), set `paths.repo_root`, provider metadata, and model IDs.

- `mock_mode: true` — no LLM or search calls; exercises YAML/HTML and SQLite session end-to-end.
- `session.resume_existing: true` — reuses session id `{slug}-assessment` in the SQLite DB.

## Run pipeline

```bash
tenant-sec-pipeline --config assessment-config.yaml
# optional: --limit 5
```

Outputs:

- `assessments/{provider}/{control-id}.yaml` — full audit trail + `review` block for humans
- `reports/{provider}/{control-id}.html` — standalone report
- `reports/{provider}/index.html` — summary table

Session DB: `session.database_path` (ADK `DatabaseSessionService`).

## Aggregate to provider profile (after human review)

Requires an existing `providers/{slug}.yaml` in the tenant-sec repo (for `services_in_scope` and merge). Approved assessments update controls via:

```bash
tenant-sec-aggregate --repo-root /path/to/tenant-sec --provider scaleway \
  --assessments-dir /path/to/assessments
```

Use `--allow-partial` only if you accept merging while some `review.status` values are still `pending`.

## Methodology note

If `METHODOLOGY.md` is missing from the tenant-sec checkout, the pipeline logs a warning and embeds **interim** criteria (PRD §4.4) in session state for prompts. For production assessments, add `METHODOLOGY.md` to tenant-sec first.

## Development

```bash
pip install -e '.[dev]'
pytest
```
