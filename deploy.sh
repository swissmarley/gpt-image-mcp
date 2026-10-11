#!/usr/bin/env bash
# Deploy the GPT Image MCP server to Google Cloud Run and print the permanent connector URL.
#
# Prereqs: gcloud installed + `gcloud auth login` + a project with billing linked.
# Usage:   ./deploy.sh                      # uses gcloud's current project, region us-central1
#          GCP_PROJECT=my-proj REGION=europe-west1 ./deploy.sh
#
# Secrets are read from .env and stored in Secret Manager. They are never printed.
set -euo pipefail

cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/share/google-cloud-sdk/bin:$HOME/google-cloud-sdk/bin:$PATH"

SERVICE="${SERVICE:-gpt-image-mcp}"
REGION="${REGION:-us-central1}"
SA_NAME="${SERVICE}-sa"
SECRET_KEY_NAME="${SERVICE}-openai-key"
SECRET_PATH_NAME="${SERVICE}-secret-path"
MAX_INSTANCES="${MAX_INSTANCES:-2}"

die() { echo "ERROR: $*" >&2; exit 1; }

command -v gcloud >/dev/null 2>&1 || die "gcloud not found. Install: brew install --cask google-cloud-sdk"

PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "$PROJECT" && "$PROJECT" != "(unset)" ]] || die "No project. Run: gcloud config set project <PROJECT_ID>  (or set GCP_PROJECT)"
gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q . || die "Not logged in. Run: gcloud auth login"

# --- read secrets from .env (never echoed) ------------------------------------
[[ -f .env ]] || die ".env not found. Copy .env.example to .env and set OPENAI_API_KEY."
env_get() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' | tr -d '\r'; }

OPENAI_KEY="$(env_get OPENAI_API_KEY)"
[[ -n "$OPENAI_KEY" ]] || die "OPENAI_API_KEY is empty in .env."

MCP_PATH="$(env_get MCP_SECRET_PATH)"
if [[ -z "$MCP_PATH" ]]; then
  MCP_PATH="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
  if grep -qE '^MCP_SECRET_PATH=' .env; then
    sed -i '' "s/^MCP_SECRET_PATH=.*/MCP_SECRET_PATH=$MCP_PATH/" .env
  else
    printf '\nMCP_SECRET_PATH=%s\n' "$MCP_PATH" >>.env
  fi
  chmod 600 .env
  echo "Generated MCP_SECRET_PATH in .env."
fi
[[ "$MCP_PATH" =~ ^[A-Za-z0-9_-]{16,128}$ ]] || die "MCP_SECRET_PATH must be 16-128 chars of [A-Za-z0-9_-]."

echo "Project: $PROJECT   Region: $REGION   Service: $SERVICE"
gcloud config set project "$PROJECT" --quiet >/dev/null

# --- APIs -------------------------------------------------------------------------
echo "Enabling required APIs (first time can take a minute)..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com --quiet

# --- secrets (create or add a new version only if the value changed) ------------
upsert_secret() {
  local name="$1" value="$2" current
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    current="$(gcloud secrets versions access latest --secret="$name" 2>/dev/null || true)"
    if [[ "$current" != "$value" ]]; then
      printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --quiet >/dev/null
      echo "  updated secret $name"
    else
      echo "  secret $name unchanged"
    fi
  else
    printf '%s' "$value" | gcloud secrets create "$name" --replication-policy=automatic --data-file=- --quiet >/dev/null
    echo "  created secret $name"
  fi
}
echo "Storing secrets in Secret Manager..."
upsert_secret "$SECRET_KEY_NAME" "$OPENAI_KEY"
upsert_secret "$SECRET_PATH_NAME" "$MCP_PATH"
unset OPENAI_KEY

# --- dedicated runtime service account with access to just these two secrets ------
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  echo "Creating service account $SA_EMAIL..."
  gcloud iam service-accounts create "$SA_NAME" --display-name="GPT Image MCP runtime" --quiet
fi
for s in "$SECRET_KEY_NAME" "$SECRET_PATH_NAME"; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA_EMAIL" \
    --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
done

# --- build + deploy ----------------------------------------------------------------
echo "Building and deploying with Cloud Build (takes 2-4 minutes)..."
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --allow-unauthenticated \
  --set-secrets "OPENAI_API_KEY=${SECRET_KEY_NAME}:latest,MCP_SECRET_PATH=${SECRET_PATH_NAME}:latest" \
  --memory 512Mi \
  --cpu 1 \
  --timeout 600 \
  --concurrency 10 \
  --min-instances 0 \
  --max-instances "$MAX_INSTANCES" \
  --quiet

BASE_URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
CONNECTOR_URL="$BASE_URL/$MCP_PATH/mcp"
mkdir -p logs
echo "$CONNECTOR_URL" >logs/cloudrun_url.txt
chmod 600 logs/cloudrun_url.txt

# --- smoke test (free: MCP initialize only) ----------------------------------------
echo "Checking the endpoint..."
code="000"
for _ in 1 2 3 4 5 6; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "$CONNECTOR_URL" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"deploy-check","version":"1"}}}' || true)"
  [[ "$code" == "200" ]] && break
  sleep 5
done
[[ "$code" == "200" ]] && echo "  initialize -> HTTP 200 OK" || echo "  WARNING: initialize returned HTTP $code. Check: gcloud run services logs read $SERVICE --region $REGION"

cat <<EOF

==================================================================
 Permanent Claude.ai connector URL (also saved to logs/cloudrun_url.txt):

   $CONNECTOR_URL

 Claude.ai -> Settings -> Connectors -> Add custom connector -> paste URL.
 This URL stays the same across redeploys. Keep it private.
==================================================================
EOF
