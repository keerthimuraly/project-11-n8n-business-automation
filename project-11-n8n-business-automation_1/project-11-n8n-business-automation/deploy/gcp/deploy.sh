#!/usr/bin/env bash
# =============================================================================
# Project 11 - deploy the n8n Business Automation Pipeline to Google Cloud Run
#
# Creates, in order:
#   1. the required APIs
#   2. a Cloud SQL for PostgreSQL instance + database + user
#   3. secrets in Secret Manager (never in an image, never in an env file)
#   4. a service account with least-privilege bindings
#   5. the ITSM API service on Cloud Run
#   6. the n8n service on Cloud Run, with the Cloud SQL connector attached
#
# Usage:
#   export PROJECT_ID=my-gcp-project
#   export OPENAI_API_KEY=sk-...
#   ./deploy.sh
#
# Re-running is safe: every step is idempotent and skips work already done.
# =============================================================================
set -euo pipefail

# --- configuration -----------------------------------------------------------
PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-south1}"                 # Mumbai
SQL_INSTANCE="${SQL_INSTANCE:-p11-n8n-pg}"
SQL_TIER="${SQL_TIER:-db-f1-micro}"             # smallest; raise for real load
DB_NAME="${DB_NAME:-n8n}"
DB_USER="${DB_USER:-n8n}"
SA_NAME="${SA_NAME:-p11-n8n-runner}"
N8N_SERVICE="${N8N_SERVICE:-p11-n8n}"
ITSM_SERVICE="${ITSM_SERVICE:-p11-itsm-api}"
N8N_IMAGE="${N8N_IMAGE:-docker.n8n.io/n8nio/n8n:latest}"
AR_REPO="${AR_REPO:-p11-images}"

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()  { printf '    \033[32m%s\033[0m\n' "$*"; }

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud config set run/region "$REGION"  >/dev/null

# --- 1. APIs -----------------------------------------------------------------
say "Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  compute.googleapis.com \
  --quiet
ok "done"

# --- 2. Cloud SQL ------------------------------------------------------------
say "Cloud SQL for PostgreSQL"
if gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  ok "instance $SQL_INSTANCE already exists"
else
  gcloud sql instances create "$SQL_INSTANCE" \
    --database-version=POSTGRES_16 \
    --tier="$SQL_TIER" \
    --region="$REGION" \
    --storage-size=10GB \
    --storage-auto-increase \
    --backup-start-time=19:00 \
    --availability-type=zonal \
    --database-flags=max_connections=100 \
    --quiet
  ok "instance created"
fi

DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"

gcloud sql databases create "$DB_NAME" --instance="$SQL_INSTANCE" --quiet 2>/dev/null \
  && ok "database $DB_NAME created" || ok "database $DB_NAME already exists"

if gcloud sql users list --instance="$SQL_INSTANCE" --format='value(name)' | grep -qx "$DB_USER"; then
  ok "user $DB_USER already exists (keeping its existing password)"
else
  gcloud sql users create "$DB_USER" \
    --instance="$SQL_INSTANCE" --password="$DB_PASSWORD" --quiet
  ok "user $DB_USER created"
fi

# --- 3. Secrets --------------------------------------------------------------
say "Secret Manager"
put_secret() {
  local name="$1" value="$2"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --quiet >/dev/null
    ok "$name: new version added"
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- \
      --replication-policy=automatic --quiet >/dev/null
    ok "$name: created"
  fi
}

# Generated once and stored. Losing N8N_ENCRYPTION_KEY means losing every
# credential saved in n8n, so it lives in Secret Manager, not in a file.
N8N_KEY="${N8N_ENCRYPTION_KEY:-$(openssl rand -hex 32)}"
ITSM_KEY="${ITSM_API_KEY:-$(openssl rand -hex 16)}"

put_secret p11-db-password       "$DB_PASSWORD"
put_secret p11-n8n-encryption    "$N8N_KEY"
put_secret p11-openai-key        "${OPENAI_API_KEY:?set OPENAI_API_KEY}"
put_secret p11-itsm-api-key      "$ITSM_KEY"

# --- 4. Service account ------------------------------------------------------
say "Service account and IAM"
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Project 11 n8n runner" --quiet 2>/dev/null \
  && ok "service account created" || ok "service account already exists"

for role in roles/cloudsql.client roles/secretmanager.secretAccessor roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" --role="$role" \
    --condition=None --quiet >/dev/null
  ok "$role"
done

# --- 5. ITSM API -------------------------------------------------------------
say "Building and deploying the ITSM API"
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker --location="$REGION" \
  --description="Project 11 images" --quiet 2>/dev/null \
  && ok "artifact repo created" || ok "artifact repo already exists"

ITSM_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/itsm-api:latest"
gcloud builds submit ../../services/itsm-api --tag "$ITSM_IMAGE" --quiet
ok "image built: $ITSM_IMAGE"

# Internal only: nothing outside the project needs to reach the mock system of
# record, so it is not exposed publicly.
gcloud run deploy "$ITSM_SERVICE" \
  --image="$ITSM_IMAGE" \
  --service-account="$SA_EMAIL" \
  --region="$REGION" \
  --no-allow-unauthenticated \
  --ingress=internal \
  --min-instances=0 --max-instances=3 \
  --memory=512Mi --cpu=1 \
  --set-secrets="ITSM_API_KEY=p11-itsm-api-key:latest" \
  --set-env-vars="LOG_LEVEL=INFO" \
  --quiet

ITSM_URL=$(gcloud run services describe "$ITSM_SERVICE" --region="$REGION" --format='value(status.url)')
ok "ITSM API: $ITSM_URL"

# --- 6. n8n ------------------------------------------------------------------
say "Deploying n8n"

# Deployed once to learn its own URL, then updated with WEBHOOK_URL set to it -
# n8n needs its public address to build correct webhook and resume URLs.
deploy_n8n() {
  local webhook_url="$1"
  gcloud run deploy "$N8N_SERVICE" \
    --image="$N8N_IMAGE" \
    --service-account="$SA_EMAIL" \
    --region="$REGION" \
    --allow-unauthenticated \
    --port=5678 \
    --min-instances=1 \
    --max-instances=3 \
    --memory=2Gi --cpu=2 \
    --timeout=3600 \
    --concurrency=40 \
    --execution-environment=gen2 \
    --add-cloudsql-instances="$CONNECTION_NAME" \
    --set-secrets="DB_POSTGRESDB_PASSWORD=p11-db-password:latest,N8N_ENCRYPTION_KEY=p11-n8n-encryption:latest,OPENAI_API_KEY=p11-openai-key:latest,ITSM_API_KEY=p11-itsm-api-key:latest" \
    --set-env-vars="^@^\
DB_TYPE=postgresdb@\
DB_POSTGRESDB_HOST=/cloudsql/${CONNECTION_NAME}@\
DB_POSTGRESDB_DATABASE=${DB_NAME}@\
DB_POSTGRESDB_USER=${DB_USER}@\
DB_POSTGRESDB_SCHEMA=n8n_meta@\
N8N_PORT=5678@\
N8N_PROTOCOL=https@\
N8N_SECURE_COOKIE=true@\
N8N_RUNNERS_ENABLED=true@\
N8N_BLOCK_ENV_ACCESS_IN_NODE=false@\
N8N_DIAGNOSTICS_ENABLED=false@\
GENERIC_TIMEZONE=Asia/Kolkata@\
TZ=Asia/Kolkata@\
EXECUTIONS_DATA_SAVE_ON_ERROR=all@\
EXECUTIONS_DATA_SAVE_ON_SUCCESS=all@\
EXECUTIONS_DATA_PRUNE=true@\
EXECUTIONS_DATA_MAX_AGE=336@\
N8N_LOG_LEVEL=info@\
ITSM_API_BASE=${ITSM_URL}@\
OPENAI_MODEL=${OPENAI_MODEL:-gpt-4o-mini}@\
APPROVAL_COST_THRESHOLD=${APPROVAL_COST_THRESHOLD:-25000}@\
MIN_LLM_CONFIDENCE=${MIN_LLM_CONFIDENCE:-0.55}@\
APPROVAL_TIMEOUT_HOURS=${APPROVAL_TIMEOUT_HOURS:-48}@\
SERVICE_DESK_FROM=${SERVICE_DESK_FROM:-servicedesk@tcs-demo.local}@\
IT_OPS_EMAIL=${IT_OPS_EMAIL:-it-ops@tcs-demo.local}@\
ESCALATION_EMAIL=${ESCALATION_EMAIL:-it-head@tcs-demo.local}@\
N8N_HOST=${webhook_url#https://}@\
WEBHOOK_URL=${webhook_url}" \
    --quiet
}

deploy_n8n "https://placeholder.invalid"
N8N_URL=$(gcloud run services describe "$N8N_SERVICE" --region="$REGION" --format='value(status.url)')

say "Re-deploying with the real public URL so webhooks resolve"
deploy_n8n "${N8N_URL}/"
ok "n8n: $N8N_URL"

# --- summary -----------------------------------------------------------------
cat <<SUMMARY

$(printf '=%.0s' {1..76})
  Deployment complete
$(printf '=%.0s' {1..76})

  n8n editor      ${N8N_URL}
  Webhook         ${N8N_URL}/webhook/it-request
  ITSM API        ${ITSM_URL}   (internal ingress only)
  Cloud SQL       ${CONNECTION_NAME}

  Remaining manual steps:

  1. Open the n8n editor and create the owner account.

  2. Apply the application schema to Cloud SQL:
       gcloud sql connect ${SQL_INSTANCE} --user=${DB_USER} --database=${DB_NAME} < ../../db/init/001_schema.sql
       gcloud sql connect ${SQL_INSTANCE} --user=${DB_USER} --database=${DB_NAME} < ../../db/init/002_seed.sql

  3. Import the four workflows from ../../workflows/

  4. Create the two credentials:
       Postgres - Project 11    host /cloudsql/${CONNECTION_NAME}
                                db ${DB_NAME}  user ${DB_USER}  schema app
                                password: gcloud secrets versions access latest --secret=p11-db-password
       SMTP - Mailpit (local)   replace with your real SMTP relay or SendGrid
                                (Mailpit is local-only and is not deployed here)

  5. Activate wf_main_triage, wf_approval and wf_sla_sweep.

  6. Smoke test:
       curl -X POST ${N8N_URL}/webhook/it-request \\
         -H 'Content-Type: application/json' \\
         -d '{"requester_email":"arun.mehta@tcs-demo.local","subject":"VPN keeps dropping on Wi-Fi","description":"The corporate VPN disconnects every ten minutes on my home network.","category":"network"}'

  Before treating this as production, read ../../docs/05_deployment_gcp.md -
  in particular the notes on public webhook exposure and Cloud Run scaling.

SUMMARY
