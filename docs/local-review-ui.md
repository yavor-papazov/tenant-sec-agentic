# Local assessment review UI

Branch-local tooling (`tool/local-assessment-review-ui`) for human reviewers working through assessment YAML artifacts. It is **not** intended to merge into `main`.

## Start

From the repo root (with the package installed, e.g. `pip install -e '.[dev]'`):

```bash
tenant-sec-review-ui \
  --assessments-dir ./assessments \
  --provider aws \
  --reviewer 'community:your-handle'
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). The server binds to `127.0.0.1` by default and refuses non-localhost hosts. `--reviewer` must match aggregation (`self`, `community:{handle}`, or `accredited:{org}`).

## Behavior

- Lists controls for one provider with pending/reviewed status and progress counts.
- Shows the reconciled `final_recommendation` (including deep-research revisions / skeptic downgrades), confidence, evidence, claims, criteria, flags, and source URLs.
- **Approve** writes `review.status: approved` and clears adjustment fields. If top-level publish fields diverge from the reconciled recommendation, Approve syncs `result_status` / `recommended_score` / `recommended_services` / `overall_confidence` to match what the UI displayed (so `tenant-sec-aggregate` publishes the reviewed score). Evidence/claims on approve still come from the artifact `assessor` block per aggregate semantics.
- **Adjust** requires a score (`0`–`3` or `mixed`) and reasoning; `mixed` requires an `adjusted_services` map compatible with aggregation.
- **Reject** writes `review.status: rejected`.
- Updates use atomic writes; Approve may also sync stale top-level recommendation fields as above. Review decision fields reuse the same validation as `tenant-sec-review-apply`.

POST requests require the session CSRF token (`X-Review-Token` header and JSON `csrf_token` field). Artifact paths are constrained to `{assessments-dir}/{provider}/{control}.yaml` with validated provider/control identifiers.

## After review

Use `tenant-sec-aggregate` as usual once assessments are approved or adjusted with a single consistent `--reviewer` identity.
