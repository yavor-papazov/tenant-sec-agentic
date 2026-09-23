#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Configure an existing billed Google Cloud project for the tenant-sec Vertex pilot.

Usage:
  scripts/setup-gcloud-pilot.sh \
    --project-id PROJECT_ID \
    --billing-account BILLING_ACCOUNT_ID \
    [--principal user:EMAIL] \
    [--location global] \
    [--budget-amount 15USD] \
    [--budget-name tenant-sec-pilot] \
    [--env-file .env] \
    [--skip-budget] \
    [--dry-run]

The script is idempotent. It does not create a project, link billing, perform
interactive login, store credentials, or create a hard spend cap. It verifies
ADC, enables required APIs, grants Vertex AI User, sets the ADC quota project,
creates one project-scoped alerting budget, and writes non-secret environment
values to the selected env file.
EOF
}

PROJECT_ID=""
BILLING_ACCOUNT=""
PRINCIPAL=""
LOCATION="global"
BUDGET_AMOUNT="15USD"
BUDGET_NAME="tenant-sec-pilot"
ENV_FILE=".env"
SKIP_BUDGET=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-id) PROJECT_ID="${2:?missing value}"; shift 2 ;;
    --billing-account) BILLING_ACCOUNT="${2:?missing value}"; shift 2 ;;
    --principal) PRINCIPAL="${2:?missing value}"; shift 2 ;;
    --location) LOCATION="${2:?missing value}"; shift 2 ;;
    --budget-amount) BUDGET_AMOUNT="${2:?missing value}"; shift 2 ;;
    --budget-name) BUDGET_NAME="${2:?missing value}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?missing value}"; shift 2 ;;
    --skip-budget) SKIP_BUDGET=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$PROJECT_ID" ]] || { echo "--project-id is required" >&2; exit 2; }
[[ -n "$BILLING_ACCOUNT" ]] || {
  echo "--billing-account is required (use --skip-budget only to skip budget creation)" >&2
  exit 2
}
command -v gcloud >/dev/null || {
  echo "gcloud is not installed or not on PATH" >&2
  exit 1
}

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  if [[ "$DRY_RUN" == false ]]; then
    "$@"
  fi
}

active_account="$(gcloud config get-value account 2>/dev/null)"
[[ -n "$active_account" ]] || {
  echo "No active gcloud account. Run: gcloud auth login" >&2
  exit 1
}

if [[ -z "$PRINCIPAL" ]]; then
  if [[ "$active_account" == *".gserviceaccount.com" ]]; then
    PRINCIPAL="serviceAccount:${active_account}"
  else
    PRINCIPAL="user:${active_account}"
  fi
fi

project_identity="$(
  gcloud projects describe "$PROJECT_ID" \
    --format='value(projectId,projectNumber)'
)"
read -r PROJECT_ID PROJECT_NUMBER <<<"$project_identity"
[[ -n "$PROJECT_ID" && -n "$PROJECT_NUMBER" ]] || {
  echo "Could not resolve project ID and number" >&2
  exit 1
}

if [[ "$DRY_RUN" == false ]]; then
  gcloud auth application-default print-access-token >/dev/null 2>&1 || {
    echo "ADC is unavailable. Run: gcloud auth application-default login" >&2
    exit 1
  }
  gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null

  billing_enabled="$(
    gcloud billing projects describe "$PROJECT_ID" \
      --format='value(billingEnabled)'
  )"
  linked_account="$(
    gcloud billing projects describe "$PROJECT_ID" \
      --format='value(billingAccountName)'
  )"
  [[ "$billing_enabled" == "True" ]] || {
    echo "Billing is not enabled on project $PROJECT_ID" >&2
    exit 1
  }
  [[ "$linked_account" == "billingAccounts/${BILLING_ACCOUNT}" ]] || {
    echo "Project billing account is $linked_account, not billingAccounts/$BILLING_ACCOUNT" >&2
    exit 1
  }
fi

run gcloud config set project "$PROJECT_ID"
run gcloud services enable \
  aiplatform.googleapis.com \
  billingbudgets.googleapis.com \
  --project "$PROJECT_ID"
run gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "$PRINCIPAL" \
  --role roles/aiplatform.user \
  --condition=None \
  --quiet
run gcloud auth application-default set-quota-project "$PROJECT_ID"

if [[ "$SKIP_BUDGET" == false ]]; then
  existing_budget=""
  if [[ "$DRY_RUN" == false ]]; then
    existing_budget="$(
      gcloud billing budgets list \
        --billing-account "$BILLING_ACCOUNT" \
        --filter "displayName=${BUDGET_NAME}" \
        --format='value(name)' \
        --limit=1
    )"
  fi
  if [[ -z "$existing_budget" ]]; then
    run gcloud billing budgets create \
      --billing-account "$BILLING_ACCOUNT" \
      --display-name "$BUDGET_NAME" \
      --budget-amount "$BUDGET_AMOUNT" \
      --filter-projects "projects/${PROJECT_NUMBER}" \
      --calendar-period month \
      --threshold-rule percent=0.5 \
      --threshold-rule percent=0.8 \
      --threshold-rule percent=1.0
  else
    echo "Budget already exists: $existing_budget"
  fi
fi

if [[ "$DRY_RUN" == false ]]; then
  mkdir -p "$(dirname "$ENV_FILE")"
  touch "$ENV_FILE"
  upsert_env() {
    local key="$1"
    local value="$2"
    local tmp
    tmp="$(mktemp)"
    awk -v key="$key" -v value="$value" '
      BEGIN { found = 0 }
      $0 ~ "^" key "=" {
        if (!found) print key "=" value
        found = 1
        next
      }
      { print }
      END { if (!found) print key "=" value }
    ' "$ENV_FILE" >"$tmp"
    mv "$tmp" "$ENV_FILE"
  }
  upsert_env GOOGLE_CLOUD_PROJECT "$PROJECT_ID"
  upsert_env GOOGLE_CLOUD_LOCATION "$LOCATION"
  upsert_env VERTEXAI_PROJECT "$PROJECT_ID"
  upsert_env VERTEXAI_LOCATION "$LOCATION"
  chmod 600 "$ENV_FILE"
  echo "Wrote non-secret Vertex settings to $ENV_FILE"
  echo "Add TAVILY_API_KEY to that file yourself; never commit it."
fi

cat <<EOF
Google Cloud pilot setup complete.
Project:   $PROJECT_ID
Number:    $PROJECT_NUMBER
Principal: $PRINCIPAL
Location:  $LOCATION
Budget:    $([[ "$SKIP_BUDGET" == true ]] && echo skipped || echo "$BUDGET_AMOUNT alerting budget")

Cloud Billing budgets send alerts; they are not hard spend caps.
The pipeline still enforces its own \$10 projected-cost ceiling.
EOF
