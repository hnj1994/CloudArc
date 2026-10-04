#!/usr/bin/env bash
# Nightly backups of CloudArc to Azure Blob Storage, authenticated with the web app's managed identity.
#
#   APP=cloudarc-console ./deploy/azure/setup-backups.sh
#
# Creates (idempotently) a locked-down StorageV2 account in the app's resource group:
#   - private "backups" container, no public access, no shared-key auth (Entra ID only), TLS 1.2+
#   - 7-day soft delete, and a lifecycle rule deleting backups after RETENTION_DAYS (default 30)
# Enables the web app's system-assigned identity, grants it Storage Blob Data Contributor on that one
# container, and sets CLOUDARC_BACKUP_URL so the app backs up daily at CLOUDARC_BACKUP_HOUR_UTC (04:00).
# Requires the Microsoft.Storage resource provider to be registered on the subscription.
set -euo pipefail
: "${APP:?set APP to the web app name}"
RG="${RG:-rg-cloudarc}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
CONTAINER=backups

location=$(az webapp show -n "$APP" -g "$RG" --query location -o tsv)
ACCOUNT="${ACCOUNT:-$(az storage account list -g "$RG" --query "[?tags.purpose=='cloudarc-backups'].name | [0]" -o tsv)}"
if [[ -z "$ACCOUNT" ]]; then
  ACCOUNT="cloudarcbk$(head -c 64 /dev/urandom | tr -dc 'a-z0-9' | head -c 8)"
  az storage account create -n "$ACCOUNT" -g "$RG" -l "$location" --sku Standard_LRS --kind StorageV2 \
    --min-tls-version TLS1_2 --https-only true --allow-blob-public-access false --allow-shared-key-access false \
    --tags purpose=cloudarc-backups --output none
  echo "Created storage account $ACCOUNT"
fi
az storage account blob-service-properties update --account-name "$ACCOUNT" -g "$RG" \
  --enable-delete-retention true --delete-retention-days 7 --output none
az storage container-rm create --storage-account "$ACCOUNT" -g "$RG" -n "$CONTAINER" --public-access off --output none 2>/dev/null \
  || true  # already exists

policy=$(mktemp)
cat > "$policy" <<JSON
{"rules": [{"enabled": true, "name": "expire-cloudarc-backups", "type": "Lifecycle",
  "definition": {"filters": {"blobTypes": ["blockBlob"], "prefixMatch": ["$CONTAINER/cloudarc-"]},
                 "actions": {"baseBlob": {"delete": {"daysAfterCreationGreaterThan": $RETENTION_DAYS}}}}}]}
JSON
az storage account management-policy create --account-name "$ACCOUNT" -g "$RG" --policy "@$policy" --output none
rm -f "$policy"
echo "Retention: backups older than $RETENTION_DAYS days are deleted; deleted blobs recoverable for 7 days"

principal=$(az webapp identity assign -n "$APP" -g "$RG" --query principalId -o tsv)
scope="$(az storage account show -n "$ACCOUNT" -g "$RG" --query id -o tsv)/blobServices/default/containers/$CONTAINER"
if [[ -z "$(az role assignment list --assignee "$principal" --scope "$scope" --role "Storage Blob Data Contributor" --query '[0].id' -o tsv)" ]]; then
  az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
    --role "Storage Blob Data Contributor" --scope "$scope" --output none
fi
echo "Granted the web app's managed identity access to $ACCOUNT/$CONTAINER only"

url="https://$ACCOUNT.blob.core.windows.net/$CONTAINER"
az webapp config appsettings set -n "$APP" -g "$RG" --settings CLOUDARC_BACKUP_URL="$url" --output none
echo
echo "Backups go to $url daily at 04:00 UTC. Check: GET /api/health -> backup. Run one now from the"
echo "web app's SSH console: python -m cloudarc.cli backup --upload"
