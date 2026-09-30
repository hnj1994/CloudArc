#!/usr/bin/env bash
# Deploy CloudArc to Azure App Service as Python code (built-in Python 3.11 runtime, Oryx build).
# Needs only the Microsoft.Web resource provider: no Container Registry or Key Vault. Use this when
# those providers are not registered in the subscription; otherwise prefer deploy-webapp.sh.
#
#   CLOUDARC_APP=cloudarc-console ADMIN_EMAIL=you@example.com ./deploy/azure/deploy-webapp-code.sh
#
# The master key is generated once and kept as an App Service application setting (encrypted at rest
# by the platform); re-runs never rotate it. Idempotent: re-run to ship a new version.
set -euo pipefail
cd "$(dirname "$0")/../.."

APP="${CLOUDARC_APP:?set CLOUDARC_APP to a globally unique web app name}"
RG="${CLOUDARC_RG:-rg-cloudarc}"
LOCATION="${CLOUDARC_LOCATION:-centralindia}"
SKU="${CLOUDARC_PLAN_SKU:-B2}"
PLAN="${APP}-plan"

if [ -n "${AZURE_CLIENT_ID:-}" ] && [ -n "${AZURE_CLIENT_SECRET:-}" ]; then
  az login --service-principal -u "$AZURE_CLIENT_ID" -p "$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID" --output none
fi
[ -n "${AZURE_SUBSCRIPTION_ID:-}" ] && az account set --subscription "$AZURE_SUBSCRIPTION_ID"

az group show -n "$RG" --output none 2>/dev/null || az group create -n "$RG" -l "$LOCATION" --output none
az appservice plan show -n "$PLAN" -g "$RG" --output none 2>/dev/null || \
  az appservice plan create -n "$PLAN" -g "$RG" -l "$LOCATION" --is-linux --sku "$SKU" --output none
if ! az webapp show -n "$APP" -g "$RG" --output none 2>/dev/null; then
  az webapp create -n "$APP" -g "$RG" -p "$PLAN" --runtime "PYTHON:3.11" --output none
fi

# Master key: generate once, never rotate on re-deploy.
EXISTING_KEY=$(az webapp config appsettings list -n "$APP" -g "$RG" --query "[?name=='CLOUDARC_MASTER_KEY'].value | [0]" -o tsv)
SETTINGS=(
  SCM_DO_BUILD_DURING_DEPLOYMENT=true
  WEBSITES_CONTAINER_START_TIME_LIMIT=600
  CLOUDARC_DATA_DIR=/home/cloudarc
  MPLCONFIGDIR=/tmp/matplotlib
  CLOUDARC_SCHEDULER_ENABLED=true
  CLOUDARC_BASE_CURRENCY=INR
)
if [ -z "$EXISTING_KEY" ]; then
  SETTINGS+=("CLOUDARC_MASTER_KEY=$(head -c 32 /dev/urandom | base64)")
  echo "Generated CLOUDARC_MASTER_KEY (App Service setting). Back it up: stored credentials cannot be decrypted without it."
fi
# Sign-in mode: an explicit CLOUDARC_AUTH_MODE wins; otherwise keep the app's current mode (so a redeploy
# never turns SSO off), defaulting to "dev" (API tokens) on first deploy.
CURRENT_MODE=$(az webapp config appsettings list -n "$APP" -g "$RG" --query "[?name=='CLOUDARC_AUTH_MODE'].value | [0]" -o tsv)
SETTINGS+=(CLOUDARC_AUTH_MODE="${CLOUDARC_AUTH_MODE:-${CURRENT_MODE:-dev}}")
[ -n "${ADMIN_EMAIL:-}" ] && SETTINGS+=(CLOUDARC_BOOTSTRAP_ADMIN_EMAIL="$ADMIN_EMAIL")
az webapp config appsettings set -n "$APP" -g "$RG" --settings "${SETTINGS[@]}" --output none

# Single instance: DuckDB allows one writer and the scheduler runs in-process.
az appservice plan update -n "$PLAN" -g "$RG" --number-of-workers 1 --output none
az webapp config set -n "$APP" -g "$RG" --always-on true --min-tls-version 1.2 --ftps-state Disabled --http20-enabled true \
  --startup-file "python -m cloudarc.cli serve --host 0.0.0.0 --port 8000" --output none
az webapp update -n "$APP" -g "$RG" --https-only true --output none
az webapp config set -n "$APP" -g "$RG" --generic-configurations '{"healthCheckPath": "/api/health"}' --output none

# Package the committed source (plus requirements.txt for the Oryx build) and deploy.
PKG=$(mktemp -d)/cloudarc.zip
git archive --format=zip -o "$PKG" HEAD cloudarc pyproject.toml README.md requirements.txt
az webapp deploy -n "$APP" -g "$RG" --src-path "$PKG" --type zip --async false --output none

HOST=$(az webapp show -n "$APP" -g "$RG" --query defaultHostName -o tsv)
echo "Waiting for https://${HOST}/api/health …"
for _ in $(seq 1 40); do
  if curl -fsS "https://${HOST}/api/health" >/dev/null 2>&1; then echo "Healthy."; break; fi
  sleep 15
done
echo "CloudArc deployed: https://${HOST}"
