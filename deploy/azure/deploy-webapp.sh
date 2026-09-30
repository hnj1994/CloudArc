#!/usr/bin/env bash
# Deploy CloudArc to Azure App Service (Web App for Containers). Idempotent: re-run to ship a new version.
#
#   CLOUDARC_APP=cloudarc-isource ADMIN_EMAIL=you@isource.example ./deploy/azure/deploy-webapp.sh
#
# Creates (if missing) in one resource group:
#   Azure Container Registry  (image built in the cloud with `az acr build` — no local Docker needed)
#   Key Vault                 (CLOUDARC_MASTER_KEY; generated once and never rotated by re-runs)
#   Linux App Service plan + Web App (system-assigned identity: AcrPull + Key Vault Secrets User)
#
# Requirements: Azure CLI >= 2.60, logged in (`az login`) or AZURE_CLIENT_ID / AZURE_CLIENT_SECRET /
# AZURE_TENANT_ID set for a service principal, with Contributor + User Access Administrator on the
# resource group (role assignments are created for the web app's identity).
set -euo pipefail
cd "$(dirname "$0")/../.."

APP="${CLOUDARC_APP:?set CLOUDARC_APP to a globally unique web app name, e.g. cloudarc-isource}"
RG="${CLOUDARC_RG:-rg-cloudarc}"
LOCATION="${CLOUDARC_LOCATION:-centralindia}"
SKU="${CLOUDARC_PLAN_SKU:-B2}"                       # B2: 2 vCPU / 3.5 GB. P1v3 for production workloads.
ACR="${CLOUDARC_ACR:-$(echo "${APP}acr" | tr -cd 'a-z0-9' | cut -c1-50)}"
KV="${CLOUDARC_KEYVAULT:-$(echo "kv-${APP}" | cut -c1-24)}"
PLAN="${APP}-plan"
TAG="${CLOUDARC_IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
AUTH_MODE="${CLOUDARC_AUTH_MODE:-dev}"                # set to "entra" once the SSO app registration exists

if [ -n "${AZURE_CLIENT_ID:-}" ] && [ -n "${AZURE_CLIENT_SECRET:-}" ]; then
  az login --service-principal -u "$AZURE_CLIENT_ID" -p "$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID" --output none
fi
[ -n "${AZURE_SUBSCRIPTION_ID:-}" ] && az account set --subscription "$AZURE_SUBSCRIPTION_ID"
SUB=$(az account show --query id -o tsv)
echo "Subscription $SUB · resource group $RG · region $LOCATION · app $APP"

az group create -n "$RG" -l "$LOCATION" --output none

# ---- registry + image -------------------------------------------------------------------------------
az acr show -n "$ACR" -g "$RG" --output none 2>/dev/null || az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled false --output none
az acr build -r "$ACR" -t "cloudarc:${TAG}" -t cloudarc:latest . --output none
IMAGE="${ACR}.azurecr.io/cloudarc:${TAG}"
echo "Built $IMAGE"

# ---- key vault + master key (generated once) --------------------------------------------------------
az keyvault show -n "$KV" -g "$RG" --output none 2>/dev/null || \
  az keyvault create -n "$KV" -g "$RG" -l "$LOCATION" --enable-rbac-authorization true --output none
KV_ID=$(az keyvault show -n "$KV" -g "$RG" --query id -o tsv)
ME=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || az ad sp show --id "${AZURE_CLIENT_ID}" --query id -o tsv)
az role assignment create --assignee "$ME" --role "Key Vault Secrets Officer" --scope "$KV_ID" --output none 2>/dev/null || true
if ! az keyvault secret show --vault-name "$KV" -n cloudarc-master-key --output none 2>/dev/null; then
  for _ in 1 2 3 4 5 6; do  # role assignments take a moment to propagate
    az keyvault secret set --vault-name "$KV" -n cloudarc-master-key --value "$(head -c 32 /dev/urandom | base64)" --output none && break
    sleep 10
  done
  echo "Generated CLOUDARC_MASTER_KEY in Key Vault $KV (back it up: stored credentials cannot be decrypted without it)"
fi
KEY_URI=$(az keyvault secret show --vault-name "$KV" -n cloudarc-master-key --query id -o tsv | sed 's|/[^/]*$||')

# ---- plan + web app -------------------------------------------------------------------------------------
az appservice plan show -n "$PLAN" -g "$RG" --output none 2>/dev/null || \
  az appservice plan create -n "$PLAN" -g "$RG" -l "$LOCATION" --is-linux --sku "$SKU" --output none
if ! az webapp show -n "$APP" -g "$RG" --output none 2>/dev/null; then
  az webapp create -n "$APP" -g "$RG" -p "$PLAN" --deployment-container-image-name "$IMAGE" --output none
fi
PRINCIPAL=$(az webapp identity assign -n "$APP" -g "$RG" --query principalId -o tsv)
ACR_ID=$(az acr show -n "$ACR" -g "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope "$ACR_ID" --output none 2>/dev/null || true
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" --scope "$KV_ID" --output none 2>/dev/null || true
az webapp config set -n "$APP" -g "$RG" --generic-configurations '{"acrUseManagedIdentityCreds": true}' --output none

# Single instance: DuckDB allows one writer process, and the scheduler runs in-process.
az appservice plan update -n "$PLAN" -g "$RG" --number-of-workers 1 --output none
az webapp config set -n "$APP" -g "$RG" --always-on true --min-tls-version 1.2 --ftps-state Disabled \
  --http20-enabled true --output none
az webapp update -n "$APP" -g "$RG" --https-only true --output none
az webapp config set -n "$APP" -g "$RG" --generic-configurations '{"healthCheckPath": "/api/health"}' --output none

SETTINGS=(
  WEBSITES_PORT=8080
  WEBSITES_ENABLE_APP_SERVICE_STORAGE=true          # /home is persistent storage
  CLOUDARC_DATA_DIR=/home/cloudarc
  "CLOUDARC_MASTER_KEY=@Microsoft.KeyVault(SecretUri=${KEY_URI})"
  CLOUDARC_AUTH_MODE="$AUTH_MODE"
  CLOUDARC_SCHEDULER_ENABLED=true
  CLOUDARC_BASE_CURRENCY=INR
)
[ -n "${ADMIN_EMAIL:-}" ] && SETTINGS+=(CLOUDARC_BOOTSTRAP_ADMIN_EMAIL="$ADMIN_EMAIL")
[ -n "${CLOUDARC_ENTRA_TENANT_ID:-}" ] && SETTINGS+=(CLOUDARC_ENTRA_TENANT_ID="$CLOUDARC_ENTRA_TENANT_ID")
[ -n "${CLOUDARC_ENTRA_CLIENT_ID:-}" ] && SETTINGS+=(CLOUDARC_ENTRA_CLIENT_ID="$CLOUDARC_ENTRA_CLIENT_ID")
az webapp config appsettings set -n "$APP" -g "$RG" --settings "${SETTINGS[@]}" --output none

az webapp config container set -n "$APP" -g "$RG" --container-image-name "$IMAGE" \
  --container-registry-url "https://${ACR}.azurecr.io" --output none
az webapp restart -n "$APP" -g "$RG" --output none

HOST=$(az webapp show -n "$APP" -g "$RG" --query defaultHostName -o tsv)
echo "Waiting for https://${HOST}/api/health …"
for _ in $(seq 1 40); do
  if curl -fsS "https://${HOST}/api/health" >/dev/null 2>&1; then echo "Healthy."; break; fi
  sleep 15
done

cat <<EOF

CloudArc deployed: https://${HOST}

First sign-in (only when ADMIN_EMAIL was set on a fresh database):
  The admin API token was written to persistent storage, not to the logs. Read it once, then delete it:
    az rest --method get --resource "https://management.azure.com/" \\
      --url "https://${APP}.scm.azurewebsites.net/api/vfs/cloudarc/bootstrap-admin-token.txt"
    az rest --method delete --resource "https://management.azure.com/" --headers "If-Match=*" \\
      --url "https://${APP}.scm.azurewebsites.net/api/vfs/cloudarc/bootstrap-admin-token.txt"
EOF
