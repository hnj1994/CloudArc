# One-time setup for .github/workflows/deploy-azure.yml (GitHub Actions -> Azure App Service, OIDC).
# Requires the Azure CLI and `az login` as a user who can create app registrations and assign roles on
# the web app. Optional: GitHub CLI (`gh auth login`) to set the repository secrets automatically.
#
#   .\deploy\azure\setup-github-deploy.ps1 -App cloudarc-console -Repo hnj1994/CloudArc
#
# Creates the Entra app "CloudArc GitHub Deploy" with a federated credential that trusts only workflow
# runs on the repository's main branch (no client secret exists), and grants it Website Contributor on
# the one web app. Idempotent: safe to re-run.
param(
    [Parameter(Mandatory = $true)][string]$App,
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$ResourceGroup = "rg-cloudarc",
    [string]$Branch = "main",
    [string]$Name = "CloudArc GitHub Deploy"
)
# "Continue": Windows PowerShell 5 turns az's stderr warnings into terminating errors under "Stop".
$ErrorActionPreference = "Continue"
function Invoke-Az {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $out
}

$webappId = Invoke-Az webapp show -n $App -g $ResourceGroup --query id -o tsv
$tenant = Invoke-Az account show --query tenantId -o tsv
$subscription = Invoke-Az account show --query id -o tsv

$clientId = Invoke-Az ad app list --display-name $Name --query "[0].appId" -o tsv
if (-not $clientId) {
    $clientId = Invoke-Az ad app create --display-name $Name --sign-in-audience AzureADMyOrg --query appId -o tsv
    Write-Host "Created app registration '$Name' ($clientId)"
}
$spId = Invoke-Az ad sp list --filter "appId eq '$clientId'" --query "[0].id" -o tsv
if (-not $spId) { $spId = Invoke-Az ad sp create --id $clientId --query id -o tsv }

# Federated credential: only workflow runs on $Repo@$Branch can obtain a token for this app.
$subject = "repo:${Repo}:ref:refs/heads/$Branch"
$existing = Invoke-Az ad app federated-credential list --id $clientId --query "[?subject=='$subject'].name | [0]" -o tsv
if (-not $existing) {
    $file = New-TemporaryFile
    try {
        (@{
            name        = "github-" + ($Repo -replace '[^A-Za-z0-9-]', '-') + "-$Branch"
            issuer      = "https://token.actions.githubusercontent.com"
            subject     = $subject
            audiences   = @("api://AzureADTokenExchange")
            description = "GitHub Actions deploy from $Repo ($Branch)"
        } | ConvertTo-Json) | Set-Content -Path $file -Encoding ascii
        Invoke-Az ad app federated-credential create --id $clientId --parameters "@$file" --output none | Out-Null
    } finally { Remove-Item $file -ErrorAction SilentlyContinue }
    Write-Host "Added federated credential for $subject"
}

# Least privilege: deploy rights on this one web app only.
$has = Invoke-Az role assignment list --assignee $spId --scope $webappId --role "Website Contributor" --query "[0].id" -o tsv
if (-not $has) {
    Invoke-Az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal `
        --role "Website Contributor" --scope $webappId --output none | Out-Null
}

$values = [ordered]@{
    "secret AZURE_CLIENT_ID"       = $clientId
    "secret AZURE_TENANT_ID"       = $tenant
    "secret AZURE_SUBSCRIPTION_ID" = $subscription
    "var AZURE_WEBAPP_NAME"        = $App
    "var AZURE_RESOURCE_GROUP"     = $ResourceGroup
}

if (Get-Command gh -ErrorAction SilentlyContinue) {
    foreach ($k in $values.Keys) {
        $kind, $key = $k.Split(" ")
        & gh $kind set $key --repo $Repo --body $values[$k]
        if ($LASTEXITCODE -ne 0) { throw "gh $kind set $key failed (run 'gh auth login' first)" }
    }
    Write-Host "`nGitHub secrets and variables set on $Repo. Next push to $Branch deploys to $App."
} else {
    Write-Host "`nGitHub CLI not found. Add these in GitHub: $Repo > Settings > Secrets and variables > Actions"
    foreach ($k in $values.Keys) {
        $kind, $key = $k.Split(" ")
        $tab = if ($kind -eq "secret") { "Secrets" } else { "Variables" }
        Write-Host ("  [{0,-9}] {1,-22} = {2}" -f $tab, $key, $values[$k])
    }
}
