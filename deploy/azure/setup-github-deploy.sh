#!/usr/bin/env bash
# One-time setup for .github/workflows/deploy-azure.yml (GitHub Actions -> Azure App Service, OIDC).
# Bash version of setup-github-deploy.ps1 (works in Azure Cloud Shell).
#
#   CLOUDARC_APP=cloudarc-console GITHUB_REPO=hnj1994/CloudArc ./deploy/azure/setup-github-deploy.sh
set -euo pipefail

APP="${CLOUDARC_APP:?set CLOUDARC_APP, e.g. cloudarc-console}"
REPO="${GITHUB_REPO:?set GITHUB_REPO, e.g. owner/repo}"
RG="${CLOUDARC_RG:-rg-cloudarc}"
BRANCH="${DEPLOY_BRANCH:-main}"
NAME="${CLOUDARC_DEPLOY_APP_NAME:-CloudArc GitHub Deploy}"

WEBAPP_ID=$(az webapp show -n "$APP" -g "$RG" --query id -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
SUB=$(az account show --query id -o tsv)

CLIENT_ID=$(az ad app list --display-name "$NAME" --query "[0].appId" -o tsv)
if [ -z "$CLIENT_ID" ]; then
  CLIENT_ID=$(az ad app create --display-name "$NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)
  echo "Created app registration '$NAME' ($CLIENT_ID)"
fi
SP_ID=$(az ad sp list --filter "appId eq '$CLIENT_ID'" --query "[0].id" -o tsv)
[ -n "$SP_ID" ] || SP_ID=$(az ad sp create --id "$CLIENT_ID" --query id -o tsv)

SUBJECT="repo:${REPO}:ref:refs/heads/${BRANCH}"
if [ -z "$(az ad app federated-credential list --id "$CLIENT_ID" --query "[?subject=='$SUBJECT'].name | [0]" -o tsv)" ]; then
  az ad app federated-credential create --id "$CLIENT_ID" --output none --parameters "{
    \"name\": \"github-$(echo "$REPO" | tr -c 'A-Za-z0-9-\n' '-')-$BRANCH\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"$SUBJECT\",
    \"audiences\": [\"api://AzureADTokenExchange\"],
    \"description\": \"GitHub Actions deploy from $REPO ($BRANCH)\"
  }"
  echo "Added federated credential for $SUBJECT"
fi

if [ -z "$(az role assignment list --assignee "$SP_ID" --scope "$WEBAPP_ID" --role "Website Contributor" --query "[0].id" -o tsv)" ]; then
  az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
    --role "Website Contributor" --scope "$WEBAPP_ID" --output none
fi

if command -v gh >/dev/null; then
  gh secret set AZURE_CLIENT_ID --repo "$REPO" --body "$CLIENT_ID"
  gh secret set AZURE_TENANT_ID --repo "$REPO" --body "$TENANT"
  gh secret set AZURE_SUBSCRIPTION_ID --repo "$REPO" --body "$SUB"
  gh variable set AZURE_WEBAPP_NAME --repo "$REPO" --body "$APP"
  gh variable set AZURE_RESOURCE_GROUP --repo "$REPO" --body "$RG"
  echo "GitHub secrets and variables set on $REPO. Next push to $BRANCH deploys to $APP."
else
  cat <<EOF
GitHub CLI not found. Add these in GitHub: $REPO > Settings > Secrets and variables > Actions
  [Secrets]   AZURE_CLIENT_ID       = $CLIENT_ID
  [Secrets]   AZURE_TENANT_ID       = $TENANT
  [Secrets]   AZURE_SUBSCRIPTION_ID = $SUB
  [Variables] AZURE_WEBAPP_NAME     = $APP
  [Variables] AZURE_RESOURCE_GROUP  = $RG
EOF
fi
