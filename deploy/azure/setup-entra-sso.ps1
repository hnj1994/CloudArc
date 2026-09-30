# One-time Entra ID SSO setup for the CloudArc console - PowerShell version of setup-entra-sso.sh.
# Requires the Azure CLI (https://aka.ms/installazurecliwindows) and `az login` as a user who can
# create app registrations. From the repository folder:
#
#   .\deploy\azure\setup-entra-sso.ps1 -App cloudarc-console
#
# Request bodies go through temporary files, so Windows PowerShell's quoting of native arguments
# cannot mangle the JSON.
param(
    [Parameter(Mandatory = $true)][string]$App,
    [string]$ResourceGroup = "rg-cloudarc",
    [string]$Name = "CloudArc Console"
)
$ErrorActionPreference = "Stop"

function Invoke-Az {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $out
}

$hostName = Invoke-Az webapp show -n $App -g $ResourceGroup --query defaultHostName -o tsv
$tenant = Invoke-Az account show --query tenantId -o tsv
$graph = "https://graph.microsoft.com/v1.0"

$clientId = & az ad app list --display-name $Name --query "[0].appId" -o tsv
if (-not $clientId) {
    $clientId = Invoke-Az ad app create --display-name $Name --sign-in-audience AzureADMyOrg --query appId -o tsv
    Write-Host "Created app registration '$Name' ($clientId)"
}
$objectId = Invoke-Az ad app show --id $clientId --query id -o tsv
$scopeId = & az ad app show --id $clientId --query "api.oauth2PermissionScopes[?value=='access_as_user'].id | [0]" -o tsv
if (-not $scopeId) { $scopeId = [guid]::NewGuid().ToString() }

function Patch-App($body) {
    $file = New-TemporaryFile
    try {
        ($body | ConvertTo-Json -Depth 10) | Set-Content -Path $file -Encoding ascii
        Invoke-Az rest --method PATCH --uri "$graph/applications/$objectId" --headers "Content-Type=application/json" --body "@$file" | Out-Null
    } finally { Remove-Item $file -ErrorAction SilentlyContinue }
}

# 1) identifier URI, SPA redirects, scope, v2 tokens, email claim, User.Read for sign-in
Patch-App @{
    identifierUris = @("api://$clientId")
    spa = @{ redirectUris = @("https://$hostName/static/blank.html", "https://$hostName/") }
    api = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = @(@{
            id = $scopeId; value = "access_as_user"; type = "User"; isEnabled = $true
            adminConsentDisplayName = "Access CloudArc"
            adminConsentDescription = "Sign in to the CloudArc console and call its API as the signed-in user."
            userConsentDisplayName = "Access CloudArc"
            userConsentDescription = "Sign in to the CloudArc console as you."
        })
    }
    optionalClaims = @{ accessToken = @(@{ name = "email"; essential = $false }) }
    requiredResourceAccess = @(@{
        resourceAppId = "00000003-0000-0000-c000-000000000000"
        resourceAccess = @(@{ id = "e1fe6dd8-ba31-4d61-89e7-88639da4683d"; type = "Scope" })
    })
}

# 2) pre-authorize the console for its own scope (needs the scope to exist first)
Patch-App @{ api = @{ preAuthorizedApplications = @(@{ appId = $clientId; delegatedPermissionIds = @($scopeId) }) } }

# 3) enterprise application (service principal) so users in the tenant can sign in
& az ad sp show --id $clientId --output none 2>$null
if ($LASTEXITCODE -ne 0) { Invoke-Az ad sp create --id $clientId --output none | Out-Null }

# 4) switch the console to SSO
Invoke-Az webapp config appsettings set -n $App -g $ResourceGroup --output none --settings `
    "CLOUDARC_AUTH_MODE=entra" "CLOUDARC_ENTRA_TENANT_ID=$tenant" "CLOUDARC_ENTRA_CLIENT_ID=$clientId" | Out-Null

Write-Host ""
Write-Host "Entra ID SSO configured."
Write-Host "  Tenant ID : $tenant"
Write-Host "  Client ID : $clientId"
Write-Host "  Console   : https://$hostName  ->  'Sign in with Microsoft'"
Write-Host ""
Write-Host "Only users pre-provisioned in CloudArc (Administration > New user, by sign-in e-mail) get access."
