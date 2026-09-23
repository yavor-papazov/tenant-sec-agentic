# tenant-sec-agentic

Python implementation of the **tenant-sec agentic assessment pipeline** described in the product requirements: multi-agent doc retrieval, scoring, skeptic/consistency review, optional deep research, and deterministic artifacts (per-control YAML + HTML + dashboard).

**Canonical data model:** [ypapazov/tenant-sec](https://github.com/ypapazov/tenant-sec). This repo only consumes that layout (`controls/`, `providers/`, `schema/`, `METHODOLOGY.md` when present).

## Requirements

- Python 3.12+
- A checkout of `tenant-sec` on disk; set `paths.repo_root` in the config to that directory
- Credentials (for non-mock runs):
  - **Vertex AI:** Application Default Credentials, `GOOGLE_CLOUD_PROJECT`, and
    `GOOGLE_CLOUD_LOCATION` (the pipeline keeps ADK as the runtime and uses
    LiteLLM only as ADK's model adapter)
  - **Search:** `TAVILY_API_KEY` for the `web_search` tool (or extend `web_tools.py`)

## Install

```bash
pip install -e .
```

## Configure

Copy `assessment-config.example.yaml` to `assessment-config.yaml` (gitignored), set `paths.repo_root`, provider metadata, and model IDs.

- `mock_mode: true` — no LLM or search calls; exercises YAML/HTML and SQLite session end-to-end.
- `session.resume_existing: true` — reuses session id `{slug}-assessment` in the SQLite DB.
- `session.resume_artifacts: true` — after a failed run, reuses matching
  completed retrieval and assessor stages instead of paying to repeat them.
- `limits` are hard stops for calls, tokens, retries, searches, fetches, and
  projected model cost. The pilot requires `max_parallel_assessments: 1`.
- The live pilot uses Vertex Gemini 3.5 Flash for every role. Live endpoint
  probes for this project confirmed true Priority service for that model on
  `eu`; Gemini 2.5 Flash was unavailable there and Gemini 3.5 Flash-Lite was
  downgraded to Standard capacity.

## Run pipeline

```bash
tenant-sec-pipeline --config assessment-config.yaml
# optional: --limit 5
```

Outputs:

- `assessments/{provider}/{control-id}.yaml` — full audit trail + `review` block for humans
- `reports/{provider}/{control-id}.html` — standalone report
- `reports/{provider}/index.html` — summary table
- `assessments/{provider}/usage-ledger.json` — per-role and per-control calls,
  token usage, retries, tool usage, and projected/actual model cost

Session DB: `session.database_path` (ADK `DatabaseSessionService`).

## Aggregate to provider profile (after human review)

Requires an existing `providers/{slug}.yaml` in the tenant-sec repo (for `services_in_scope` and merge). Approved assessments update controls via:

```bash
tenant-sec-aggregate --repo-root /path/to/tenant-sec --provider scaleway \
  --assessments-dir /path/to/assessments
```

Use `--allow-partial` only if you accept merging while some `review.status` values are still `pending`.

## Methodology note

If `METHODOLOGY.md` is missing, the fallback still treats missing evidence as
`unknown`; it never converts a failed search into L0. Production assessments
must use the canonical `METHODOLOGY.md`.

## Live pilot prerequisites

The mock run needs no cloud account. Before the first live control:

```bash
gcloud auth login
gcloud auth application-default login

scripts/setup-gcloud-pilot.sh \
  --project-id YOUR_PROJECT \
  --billing-account YOUR_BILLING_ACCOUNT \
  --budget-amount 15USD

# Add this secret to .env yourself:
TAVILY_API_KEY=...
```

The idempotent setup script verifies that the existing project is billed,
enables Vertex AI and Cloud Billing Budgets APIs, grants the selected principal
`roles/aiplatform.user`, sets the ADC quota project, creates a project-scoped
$15 alerting budget, and writes non-secret Vertex variables to `.env`. It does
not create a project, link billing, or perform interactive authentication.

The example demonstrates Vertex **Priority PayGo**. Google documents this
traffic class for agentic workflows; it provides immediate baseline throughput
without a Provisioned Throughput commitment, at a premium over Standard PayGo.
Priority supports the `global`, `us`, and `eu` endpoints.

The RC1 provider configs use Gemini 2.5 Flash with Standard traffic on the
`global` endpoint. Live recovery probes found repeated timeouts and malformed
structured output from Gemini 3.5 Flash on the `eu` Priority route, while the
global 2.5 route responded promptly. Set both `GOOGLE_CLOUD_LOCATION` and
`VERTEXAI_LOCATION` to the selected endpoint and keep the pricing table aligned
with the chosen model and traffic class.

Use a dedicated billed project and configure its budget controls first. Cloud
Billing budgets are normally alerts, not hard caps; the pipeline's
`max_projected_cost_usd` is the per-run enforcement backstop.

## Development

```bash
pip install -e '.[dev]'
pytest
```
