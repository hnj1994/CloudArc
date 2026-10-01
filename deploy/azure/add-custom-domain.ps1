# Put the console on your own domain (e.g. cloudarc.yourcompany.com) with a free App Service managed
# certificate, and allow Entra ID SSO sign-in from it. Requires the Azure CLI and `az login` as a user who
# can manage the web app and the "CloudArc Console" app registration.
#
#   .\deploy\azure\add-custom-domain.ps1 -App cloudarc-console -Domain cloudarc.yourcompany.com
#
# First run prints the two DNS records to create at your DNS provider and stops. Run it again once they
# resolve; it then binds the domain, issues and binds the certificate, and adds the SSO redirect URIs.
# Idempotent: safe to re-run. Use a subdomain - App Service managed certificates for an apex domain need
# an A record instead of the CNAME below.
param(
    [Parameter(Mandatory = $true)][string]$App,
    [Parameter(Mandatory = $true)][string]$Domain,
    [string]$ResourceGroup = "rg-cloudarc",
    [string]$SsoAppName = "CloudArc Console"
)
$ErrorActionPreference = "Continue"
function Invoke-Az {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    return $out
}

$Domain = $Domain.Trim().TrimEnd(".").ToLower()
$defaultHost = Invoke-Az webapp show -n $App -g $ResourceGroup --query defaultHostName -o tsv
$verificationId = Invoke-Az webapp show -n $App -g $ResourceGroup --query customDomainVerificationId -o tsv

# 1) DNS: CNAME for traffic, TXT asuid.<domain> to prove ownership to App Service.
$cname = (Resolve-DnsName -Name $Domain -Type CNAME -ErrorAction SilentlyContinue | Where-Object { $_.Type -eq "CNAME" }).NameHost
$txt = (Resolve-DnsName -Name "asuid.$Domain" -Type TXT -ErrorAction SilentlyContinue | Where-Object { $_.Type -eq "TXT" }).Strings
$cnameOk = $cname -and ($cname.TrimEnd(".").ToLower() -eq $defaultHost)
$txtOk = $txt -contains $verificationId
if (-not ($cnameOk -and $txtOk)) {
    Write-Host "Create these DNS records for $Domain, wait for them to resolve, then run this script again:`n"
    Write-Host ("  {0,-6} {1,-40} {2}" -f "Type", "Name", "Value")
    Write-Host ("  {0,-6} {1,-40} {2}   {3}" -f "CNAME", $Domain, $defaultHost, $(if ($cnameOk) { "(found)" } else { "(missing)" }))
    Write-Host ("  {0,-6} {1,-40} {2}   {3}" -f "TXT", "asuid.$Domain", $verificationId, $(if ($txtOk) { "(found)" } else { "(missing)" }))
    Write-Host "`nAt most DNS providers the Name field is relative to your zone, e.g. 'cloudarc' and 'asuid.cloudarc'."
    exit 1
}

# 2) Bind the hostname, then issue and bind a managed certificate (SNI).
$bound = Invoke-Az webapp config hostname list --webapp-name $App -g $ResourceGroup --query "[?name=='$Domain'].name | [0]" -o tsv
if (-not $bound) {
    Invoke-Az webapp config hostname add --webapp-name $App -g $ResourceGroup --hostname $Domain --output none | Out-Null
    Write-Host "Bound $Domain to $App"
}
$thumb = Invoke-Az webapp config ssl list -g $ResourceGroup --query "[?subjectName=='$Domain'].thumbprint | [0]" -o tsv
if (-not $thumb) {
    Write-Host "Issuing a managed certificate for $Domain (takes a few minutes)..."
    $thumb = Invoke-Az webapp config ssl create -g $ResourceGroup -n $App --hostname $Domain --query thumbprint -o tsv
}
Invoke-Az webapp config ssl bind -g $ResourceGroup -n $App --certificate-thumbprint $thumb --ssl-type SNI --output none | Out-Null
Invoke-Az webapp update -g $ResourceGroup -n $App --https-only true --output none | Out-Null

# 3) SSO: allow sign-in redirects to the new domain (existing redirect URIs are kept).
$clientId = Invoke-Az ad app list --display-name $SsoAppName --query "[0].appId" -o tsv
if ($clientId) {
    $objectId = Invoke-Az ad app show --id $clientId --query id -o tsv
    $uris = @(Invoke-Az ad app show --id $clientId --query "spa.redirectUris" -o tsv) | Where-Object { $_ }
    $wanted = @("https://$Domain/static/blank.html", "https://$Domain/")
    $missing = $wanted | Where-Object { $uris -notcontains $_ }
    if ($missing) {
        $file = New-TemporaryFile
        try {
            (@{ spa = @{ redirectUris = @($uris + $missing) } } | ConvertTo-Json -Depth 5) | Set-Content -Path $file -Encoding ascii
            Invoke-Az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$objectId" `
                --headers "Content-Type=application/json" --body "@$file" | Out-Null
        } finally { Remove-Item $file -ErrorAction SilentlyContinue }
        Write-Host "Added SSO redirect URIs for $Domain to '$SsoAppName'"
    }
} else {
    Write-Host "No app registration named '$SsoAppName' found; skipped SSO redirect URIs."
}

Write-Host "`nDone. Console: https://$Domain  (the azurewebsites.net address keeps working)."
