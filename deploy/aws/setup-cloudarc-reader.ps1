# Create the read-only IAM identity CloudArc uses to read AWS cost (Cost Explorer, optionally CUR 2.0 in S3).
# Run with the AWS CLI (https://aws.amazon.com/cli/) signed in to the MANAGEMENT (payer) account, so Cost
# Explorer covers every linked account:
#
#   .\deploy\aws\setup-cloudarc-reader.ps1
#   .\deploy\aws\setup-cloudarc-reader.ps1 -CurBucket my-billing-exports -CurPrefix cur/cloudarc
#
# Creates IAM user "cloudarc-reader" with an inline policy allowing only ce:GetCostAndUsage and
# ce:GetDimensionValues (plus, with -CurBucket, listing that bucket and reading objects under the prefix),
# then an access key. The secret access key is printed once: paste it into CloudArc (Accounts & data ->
# Connect AWS or GCP), never into chat or email. Idempotent apart from the access key (an IAM user may hold
# two; delete old ones in IAM when you rotate).
param(
    [string]$UserName = "cloudarc-reader",
    [string]$CurBucket,
    [string]$CurPrefix = ""
)
$ErrorActionPreference = "Continue"
function Invoke-Aws {
    $out = & aws @args
    if ($LASTEXITCODE -ne 0) { throw "aws $($args -join ' ') failed" }
    return $out
}

$account = Invoke-Aws sts get-caller-identity --query Account --output text
Write-Host "AWS account: $account"

& aws iam get-user --user-name $UserName --output none 2>$null
if ($LASTEXITCODE -ne 0) {
    Invoke-Aws iam create-user --user-name $UserName --tags Key=purpose,Value=cloudarc-read-only --output none | Out-Null
    Write-Host "Created IAM user $UserName"
}

$statements = @(@{ Sid = "CostExplorerRead"; Effect = "Allow"; Action = @("ce:GetCostAndUsage", "ce:GetDimensionValues"); Resource = "*" })
if ($CurBucket) {
    $prefix = $CurPrefix.Trim("/")
    $objects = if ($prefix) { "arn:aws:s3:::$CurBucket/$prefix/*" } else { "arn:aws:s3:::$CurBucket/*" }
    $statements += @{ Sid = "CurList"; Effect = "Allow"; Action = @("s3:ListBucket", "s3:GetBucketLocation"); Resource = "arn:aws:s3:::$CurBucket" }
    $statements += @{ Sid = "CurRead"; Effect = "Allow"; Action = "s3:GetObject"; Resource = $objects }
}
$file = New-TemporaryFile
try {
    (@{ Version = "2012-10-17"; Statement = $statements } | ConvertTo-Json -Depth 6) | Set-Content -Path $file -Encoding ascii
    Invoke-Aws iam put-user-policy --user-name $UserName --policy-name cloudarc-cost-read --policy-document "file://$file" --output none | Out-Null
} finally { Remove-Item $file -ErrorAction SilentlyContinue }
Write-Host "Attached read-only policy cloudarc-cost-read"

$keys = @(Invoke-Aws iam list-access-keys --user-name $UserName --query "AccessKeyMetadata[].AccessKeyId" --output text) -split "\s+" | Where-Object { $_ }
if ($keys.Count -ge 2) { throw "$UserName already has two access keys; delete an unused one in IAM, then re-run" }
$key = Invoke-Aws iam create-access-key --user-name $UserName --query "AccessKey.[AccessKeyId,SecretAccessKey]" --output text
$id, $secret = $key -split "\s+"

Write-Host ""
Write-Host "Enter these in CloudArc: Accounts & data -> Connect AWS or GCP -> AWS"
Write-Host "  Access key ID     : $id"
Write-Host "  Secret access key : $secret   (shown once - do not share it anywhere else)"
if ($CurBucket) {
    Write-Host "  CUR 2.0 bucket    : $CurBucket"
    Write-Host "  CUR prefix        : $($CurPrefix.Trim('/'))"
}
Write-Host ""
Write-Host "If Cost Explorer has never been opened in this account, open it once in the console; AWS takes up to 24 h to enable it."
