# Next iteration and host handoff

Last updated: 2026-10-26

This document is the restart point for continuing `tenant-sec-agentic` on
another machine. It records the current release baseline, the next research and
product work, and the local-only state that Git deliberately does not carry.

## Current baseline

- `tenant-sec-agentic` is complete for the RC3 provider-expansion run on
  `main` at `3b59641` (`Complete RC3 provider research pipeline`).
- Canonical provider data is in
  [ypapazov/tenant-sec](https://github.com/ypapazov/tenant-sec):
  - RC3 package/data tag: `v1.0.0rc3` at `b6a878a`.
  - Current `main`: `c0c847d`, which adds the public Evidence & rationale UI.
- RC3 covers nine provider profiles and 558 top-level provider/control records.
- The canonical core suite has 205 passing tests. The latest core CI and Pages
  deployment for `c0c847d` passed.
- No assessment or report artifact directory is required to reproduce the
  canonical RC3 data. The aggregated provider YAML in `tenant-sec` is the
  source of truth.

## P0: complete licensing research

### Current state

Commercial availability is separated from L0-L3 maturity, but the dataset is
not complete enough to use licensing as a dependable filter:

- 186 licensing observations exist across IONOS, OCI, and STACKIT.
- 116 are `included`, five are `paid_add_on`, and 65 are `unknown`.
- The 65 unknowns are IONOS 23, OCI 17, and STACKIT 25.
- Only 18 of those unknown observations cite any source; 47 have no
  licensing-specific reference.
- AWS, Azure, GCP, Hetzner, OVHcloud, and Scaleway have no licensing
  observations yet: 372 top-level control records still need backfilling.
- The current `unknown` value conflates insufficient research, information that
  is genuinely unavailable publicly, and pricing that is contract-dependent.

### Required work

1. Define distinct semantics for:
   - not researched
   - researched but unresolved
   - included
   - paid add-on
   - contract-dependent or quote-only
2. Prefer an explicit research state over treating an absent `licensing` block
   as an implicit unknown.
3. Add a dedicated licensing-retrieval stage. Feature documentation alone is
   not enough; search pricing pages, service catalogues, contracts, SKUs, API
   billing metadata, and official sales material.
4. Backfill all nine providers and all applicable tenant controls.
5. Require a research note and source for every resolved value and every
   researched-but-unresolved value.
6. Extend schema validation, aggregation, exports, and the public UI so that
   maturity and commercial availability remain visibly independent.
7. Report licensing completeness separately from control-assessment
   completeness.

### Definition of done

- Every applicable provider/control pair has an explicit licensing research
  state.
- No value implies that information is non-public without evidence.
- Contract-dependent results cite an official contract, catalogue, or
  provider statement where available.
- The pipeline and canonical repository tests cover every new state and reject
  ambiguous output.

## P0: executable tenant-control playbooks

The goal is to complement assessment scores with practical, reproducible
instructions showing how a tenant can configure and verify a capability.

### Scope

- Create playbooks only where the tenant can take an operational action.
- For contractual, provider-operated, or selection-time controls, produce a
  procurement/verification checklist rather than inventing a CLI procedure.
- Scope every playbook by provider, offering, region/partition, edition, service,
  and control. Do not assume one provider-wide command applies to every service.

### Proposed playbook record

Store structured source data and render tutorials from it. Each record should
include:

- provider assessment ID, control ID, service IDs, region, edition
- status: `draft`, `live_verified`, `failed`, or `stale`
- prerequisites and minimum tenant permissions
- supported channels: CLI, API, SDK, IaC, console, or contract/procurement
- ordered commands or request templates with placeholders
- expected output and deterministic verification steps
- rollback and cleanup steps
- idempotency notes
- destructive-action and paid-resource warnings
- estimated cloud cost and resources created
- tool/API versions and `verified_at`
- primary documentation sources and short supporting excerpts
- captured, redacted execution result or test assertion

Never commit account identifiers, tokens, private endpoints, customer data, or
unredacted command output.

### Pipeline design

1. An agent drafts a playbook from official technical documentation.
2. A static validator checks schema, placeholders, cleanup, safety annotations,
   source links, and prohibited secret patterns.
3. A provider-specific harness runs eligible procedures in isolated sandbox
   projects/accounts with budgets and least-privilege identities.
4. Paid, destructive, organization-wide, or irreversible steps require an
   explicit human approval gate.
5. The harness executes verification and cleanup even after a failed setup.
6. Only a successful captured run may set `live_verified`.
7. Version drift or an expired verification interval changes the status to
   `stale`; it must not silently remain verified.

### Costing pilot

Do not extrapolate from documentation-only assessment runs yet. Draft
generation may have similar model cost, but live verification adds cloud
charges, setup, retries, cleanup, and review.

Start with 18 playbooks: six controls across AWS, Azure, and GCP. Suggested
controls:

- `iam.mfa-enforcement`
- `iam.permission-boundaries`
- `enc.cmk`
- `log.control-plane-audit`
- `data.backup-governance`
- `net.flow-logging`

Measure model calls, tokens, elapsed time, cloud spend, retry rate, cleanup
failures, and human-review time. Use those measurements before approving the
full nine-provider scope.

### Definition of done for the pilot

- All 18 records pass static validation.
- Each runnable playbook has been executed from a clean tenant account or
  project and independently verified.
- Cleanup leaves no unexpected paid resources.
- The usage ledger separates model cost from provider spend and human effort.
- Failed or non-runnable controls are represented honestly rather than filled
  with untested commands.

## P1: refine public evidence and citations

The first version is already deployed in Controls Explorer. It presents rubric
decisions, claims, and expandable linked evidence directly under each provider
assessment.

Current canonical data contains 1,895 claim-linked excerpts. The median excerpt
is 249 characters; 65 reach the current 1,200-character maximum.

Next iteration:

1. Add inline claim citations in rubric reasoning, with stable evidence IDs and
   anchors.
2. Prefer primary sources and the smallest excerpt that proves the claim.
3. Target roughly 300-500 public characters per excerpt; do not reproduce
   complete sections, tables, or documents.
4. Deduplicate an evidence item cited by multiple claims while preserving every
   claim-to-evidence link.
5. Record source title, publisher, URL, retrieval/update date, source class,
   content hash, applicability, and known documentation license.
6. Do not publish paywalled, access-controlled, customer-specific, or
   contract-confidential text.
7. Where quotation rights or terms are unclear, publish a concise factual
   summary, a short necessary excerpt, the hash, and the source link.
8. Add tests for escaping, URL protocols, citation integrity, excerpt limits,
   and missing sources.

This is a risk-reduction policy, not a conclusion that public documentation is
copyright-free. Any large-scale republication policy should receive legal
review.

## Small documentation follow-up

While only release candidates exist on PyPI, plain `pip install tenant-sec`
does not select them. Update public installation examples to one of:

```bash
pip install --pre tenant-sec
pip install tenant-sec==1.0.0rc3
```

Return to the plain command when a stable release is published.

## New Mac setup

### GitHub and repositories

The GitHub account currently used for Git operations is `ypapazov`. It has
access to the agentic repository owned by `yavor-papazov`.

```bash
gh auth login
gh auth switch --user ypapazov
gh auth setup-git

git clone https://github.com/ypapazov/tenant-sec.git
git clone https://github.com/yavor-papazov/tenant-sec-agentic.git
```

Keep the repositories as siblings so the committed `paths.repo_root:
../tenant-sec` values continue to work.

The presentation worktree/repository uses the same
`yavor-papazov/tenant-sec-agentic` remote on branch `presentation-slides`; its
current commit is `1e71cf6`.

### Python environment

```bash
cd tenant-sec-agentic
uv sync --extra dev --reinstall-package tenant-sec-agentic
uv run pytest
```

`uv.lock` is committed. Python 3.12 or newer is required.

### Google Cloud and search

The last working configuration used:

- Google account: `yavor@reachup.bg`
- project: `tenantsec`
- Vertex location: `global`
- model: `vertex_ai/gemini-2.5-flash`
- traffic class: Standard

Authenticate interactively on the new machine:

```bash
gcloud auth login
gcloud config set project tenantsec
gcloud auth application-default login
gcloud auth application-default set-quota-project tenantsec
```

Recreate `.env` locally; never commit it:

```dotenv
GOOGLE_CLOUD_PROJECT=tenantsec
GOOGLE_CLOUD_LOCATION=global
VERTEXAI_PROJECT=tenantsec
VERTEXAI_LOCATION=global
TAVILY_API_KEY=RECREATE_OR_TRANSFER_SECURELY
```

Run a structured-output canary before a paid batch. Gemini 2.5 Flash on the
global Standard route is the verified baseline. Do not switch model, endpoint,
or traffic class without another canary and matching price configuration.

### Local-only state

These files are intentionally ignored and are not transferred by Git:

- `.env`
- `assessment-config.yaml`
- `assessments/`
- `reports/`
- `pipeline_sessions.db` and `pipeline_sessions.*.db`

The local SQLite databases are resumable session caches, not canonical results.
RC3 is already aggregated and pushed, so they are not required on the new Mac.
If a future in-progress run must be resumed exactly, securely transfer its
database together with its matching assessment artifacts; transferring only
the database is insufficient.

Use the committed provider-specific configs as starting points. Copy one to
`assessment-config.yaml` only when a local override is needed.

## Restart checklist

1. Pull `main` in both repositories and verify the commits listed above.
2. Run the agentic and core test suites.
3. Recreate `.env` and complete both normal `gcloud` and ADC authentication.
4. Run a mock or one-control canary before a paid batch.
5. Start with the licensing schema/research-state design.
6. Run the 18-playbook pilot before committing to full playbook generation.
