#!/usr/bin/env bash
# One-time Entra ID SSO setup for the CloudArc console. Run as a user who can create app registrations
# (e.g. in Azure Cloud Shell: https://shell.azure.com, Bash), from a checkout of this repository:
#
#   CLOUDARC_APP=cloudarc-console ./deploy/azure/setup-entra-sso.sh
#
# Creates (or updates) the "CloudArc Console" app registration:
#   - single-tenant, SPA redirect URIs on the console (MSAL popup lands on /static/blank.html)
#   - exposes api://<client-id>/access_as_user, pre-authorized for the console itself (no consent prompt)
#   - issues v2 access tokens (preferred_username = the user's sign-in name)
# then switches the web app to CLOUDARC_AUTH_MODE=entra. API tokens keep working for automation.
# MFA is enforced by Entra: Security Defaults, or a Conditional Access policy targeting this app.
set -euo pipefail

APP="${CLOUDARC_APP:?set CLOUDARC_APP to the web app name, e.g. cloudarc-console}"
RG="${CLOUDARC_RG:-rg-cloudarc}"
NAME="${CLOUDARC_SSO_APP_NAME:-CloudArc Console}"
HOST=$(az webapp show -n "$APP" -g "$RG" --query defaultHostName -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
GRAPH=https://graph.microsoft.com/v1.0

CLIENT_ID=$(az ad app list --display-name "$NAME" --query "[0].appId" -o tsv)
if [ -z "$CLIENT_ID" ]; then
  CLIENT_ID=$(az ad app create --display-name "$NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)
  echo "Created app registration '$NAME' ($CLIENT_ID)"
fi
OBJ=$(az ad app show --id "$CLIENT_ID" --query id -o tsv)
SCOPE_ID=$(az ad app show --id "$CLIENT_ID" --query "api.oauth2PermissionScopes[?value=='access_as_user'].id | [0]" -o tsv)
[ -z "$SCOPE_ID" ] && SCOPE_ID=$(python3 -c "import uuid; print(uuid.uuid4())")

# 1) identifier URI, SPA redirects, scope, v2 tokens, email claim, User.Read for sign-in
az rest --method PATCH --uri "$GRAPH/applications/$OBJ" --headers "Content-Type=application/json" --body "{
  \"identifierUris\": [\"api://$CLIENT_ID\"],
  \"spa\": {\"redirectUris\": [\"https://$HOST/static/blank.html\", \"https://$HOST/\"]},
  \"api\": {
    \"requestedAccessTokenVersion\": 2,
    \"oauth2PermissionScopes\": [{
      \"id\": \"$SCOPE_ID\", \"value\": \"access_as_user\", \"type\": \"User\", \"isEnabled\": true,
      \"adminConsentDisplayName\": \"Access CloudArc\",
      \"adminConsentDescription\": \"Sign in to the CloudArc console and call its API as the signed-in user.\",
      \"userConsentDisplayName\": \"Access CloudArc\",
      \"userConsentDescription\": \"Sign in to the CloudArc console as you.\"
    }]
  },
  \"optionalClaims\": {\"accessToken\": [{\"name\": \"email\", \"essential\": false}]},
  \"requiredResourceAccess\": [{\"resourceAppId\": \"00000003-0000-0000-c000-000000000000\",
    \"resourceAccess\": [{\"id\": \"e1fe6dd8-ba31-4d61-89e7-88639da4683d\", \"type\": \"Scope\"}]}]
}"

# 2) pre-authorize the console for its own scope (needs the scope to exist first)
az rest --method PATCH --uri "$GRAPH/applications/$OBJ" --headers "Content-Type=application/json" --body "{
  \"api\": {\"preAuthorizedApplications\": [{\"appId\": \"$CLIENT_ID\", \"delegatedPermissionIds\": [\"$SCOPE_ID\"]}]}
}"

# 3) enterprise application (service principal) so users in the tenant can sign in
az ad sp show --id "$CLIENT_ID" --output none 2>/dev/null || az ad sp create --id "$CLIENT_ID" --output none

# 4) switch the console to SSO
az webapp config appsettings set -n "$APP" -g "$RG" --output none --settings \
  CLOUDARC_AUTH_MODE=entra CLOUDARC_ENTRA_TENANT_ID="$TENANT" CLOUDARC_ENTRA_CLIENT_ID="$CLIENT_ID"

cat <<EOF

Entra ID SSO configured.
  Tenant ID : $TENANT
  Client ID : $CLIENT_ID
  Console   : https://$HOST  →  "Sign in with Microsoft"

Only users pre-provisioned in CloudArc (Administration › New user, by sign-in e-mail) get access;
signing in with Microsoft alone grants nothing.
Optional hardening: restrict sign-in to assigned users only —
  az ad sp update --id $CLIENT_ID --set appRoleAssignmentRequired=true
and assign users/groups under Enterprise applications › $NAME › Users and groups.
EOF
