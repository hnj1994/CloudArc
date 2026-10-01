#!/usr/bin/env bash
# Put the console on your own domain (e.g. cloudarc.yourcompany.com) with a free App Service managed
# certificate, and allow Entra ID SSO sign-in from it. Bash version of add-custom-domain.ps1.
#
#   APP=cloudarc-console DOMAIN=cloudarc.yourcompany.com ./deploy/azure/add-custom-domain.sh
#
# First run prints the two DNS records to create and stops; run again once they resolve. Idempotent.
set -euo pipefail
: "${APP:?set APP to the web app name}"
: "${DOMAIN:?set DOMAIN, e.g. cloudarc.yourcompany.com}"
RG="${RG:-rg-cloudarc}"
SSO_APP_NAME="${SSO_APP_NAME:-CloudArc Console}"
DOMAIN="$(echo "${DOMAIN%.}" | tr '[:upper:]' '[:lower:]')"

default_host=$(az webapp show -n "$APP" -g "$RG" --query defaultHostName -o tsv)
verification_id=$(az webapp show -n "$APP" -g "$RG" --query customDomainVerificationId -o tsv)

# 1) DNS: CNAME for traffic, TXT asuid.<domain> to prove ownership to App Service.
# dig when available, otherwise DNS-over-HTTPS (dns.google) so the check also works where dig is missing.
lookup() {
  if command -v dig >/dev/null; then dig +short "$2" "$1"; return; fi
  curl -fsS "https://dns.google/resolve?name=$1&type=$2" | python3 -c '
import json, sys
t = {"CNAME": 5, "TXT": 16}[sys.argv[1]]
print("\n".join(a["data"] for a in json.load(sys.stdin).get("Answer", []) if a["type"] == t))' "$2"
}
cname=$(lookup "$DOMAIN" CNAME | sed 's/\.$//' | tr '[:upper:]' '[:lower:]')
txt=$(lookup "asuid.$DOMAIN" TXT | tr -d '"')
if [[ "$cname" != "$default_host" || "$txt" != *"$verification_id"* ]]; then
  echo "Create these DNS records for $DOMAIN, wait for them to resolve, then run this script again:"
  echo
  printf '  %-6s %-40s %s\n' Type Name Value CNAME "$DOMAIN" "$default_host" TXT "asuid.$DOMAIN" "$verification_id"
  echo
  echo "At most DNS providers the Name field is relative to your zone, e.g. 'cloudarc' and 'asuid.cloudarc'."
  exit 1
fi

# 2) Bind the hostname, then issue and bind a managed certificate (SNI).
if [[ -z "$(az webapp config hostname list --webapp-name "$APP" -g "$RG" --query "[?name=='$DOMAIN'].name | [0]" -o tsv)" ]]; then
  az webapp config hostname add --webapp-name "$APP" -g "$RG" --hostname "$DOMAIN" --output none
  echo "Bound $DOMAIN to $APP"
fi
thumb=$(az webapp config ssl list -g "$RG" --query "[?subjectName=='$DOMAIN'].thumbprint | [0]" -o tsv)
if [[ -z "$thumb" ]]; then
  echo "Issuing a managed certificate for $DOMAIN (takes a few minutes)..."
  thumb=$(az webapp config ssl create -g "$RG" -n "$APP" --hostname "$DOMAIN" --query thumbprint -o tsv)
fi
az webapp config ssl bind -g "$RG" -n "$APP" --certificate-thumbprint "$thumb" --ssl-type SNI --output none
az webapp update -g "$RG" -n "$APP" --https-only true --output none

# 3) SSO: allow sign-in redirects to the new domain (existing redirect URIs are kept).
client_id=$(az ad app list --display-name "$SSO_APP_NAME" --query "[0].appId" -o tsv)
if [[ -n "$client_id" ]]; then
  object_id=$(az ad app show --id "$client_id" --query id -o tsv)
  body=$(az ad app show --id "$client_id" --query "spa.redirectUris" -o json | DOMAIN="$DOMAIN" python3 -c '
import json, os, sys
uris = json.load(sys.stdin) or []
d = os.environ["DOMAIN"]
for u in (f"https://{d}/static/blank.html", f"https://{d}/"):
    if u not in uris:
        uris.append(u)
print(json.dumps({"spa": {"redirectUris": uris}}))')
  az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$object_id" \
    --headers "Content-Type=application/json" --body "$body" --output none
  echo "SSO redirect URIs for $DOMAIN present on '$SSO_APP_NAME'"
else
  echo "No app registration named '$SSO_APP_NAME' found; skipped SSO redirect URIs."
fi

echo
echo "Done. Console: https://$DOMAIN  (the azurewebsites.net address keeps working)."
