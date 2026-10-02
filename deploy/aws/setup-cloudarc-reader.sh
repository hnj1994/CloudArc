#!/usr/bin/env bash
# Bash version of setup-cloudarc-reader.ps1: read-only IAM user for CloudArc (Cost Explorer, optional CUR 2.0 in S3).
# Run in AWS CloudShell or with the AWS CLI signed in to the MANAGEMENT (payer) account:
#
#   ./deploy/aws/setup-cloudarc-reader.sh
#   CUR_BUCKET=my-billing-exports CUR_PREFIX=cur/cloudarc ./deploy/aws/setup-cloudarc-reader.sh
#
# The secret access key is printed once: paste it into CloudArc, never into chat or email.
set -euo pipefail
USER_NAME="${USER_NAME:-cloudarc-reader}"
CUR_BUCKET="${CUR_BUCKET:-}"
CUR_PREFIX="${CUR_PREFIX:-}"
CUR_PREFIX="${CUR_PREFIX#/}"; CUR_PREFIX="${CUR_PREFIX%/}"

echo "AWS account: $(aws sts get-caller-identity --query Account --output text)"
if ! aws iam get-user --user-name "$USER_NAME" --output text >/dev/null 2>&1; then
  aws iam create-user --user-name "$USER_NAME" --tags Key=purpose,Value=cloudarc-read-only --output text >/dev/null
  echo "Created IAM user $USER_NAME"
fi

statements='{"Sid":"CostExplorerRead","Effect":"Allow","Action":["ce:GetCostAndUsage","ce:GetDimensionValues"],"Resource":"*"}'
if [[ -n "$CUR_BUCKET" ]]; then
  objects="arn:aws:s3:::$CUR_BUCKET/${CUR_PREFIX:+$CUR_PREFIX/}*"
  statements+=",{\"Sid\":\"CurList\",\"Effect\":\"Allow\",\"Action\":[\"s3:ListBucket\",\"s3:GetBucketLocation\"],\"Resource\":\"arn:aws:s3:::$CUR_BUCKET\"}"
  statements+=",{\"Sid\":\"CurRead\",\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":\"$objects\"}"
fi
aws iam put-user-policy --user-name "$USER_NAME" --policy-name cloudarc-cost-read \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[$statements]}"
echo "Attached read-only policy cloudarc-cost-read"

if [[ $(aws iam list-access-keys --user-name "$USER_NAME" --query 'length(AccessKeyMetadata)' --output text) -ge 2 ]]; then
  echo "$USER_NAME already has two access keys; delete an unused one in IAM, then re-run" >&2
  exit 1
fi
read -r key_id secret < <(aws iam create-access-key --user-name "$USER_NAME" --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)

echo
echo "Enter these in CloudArc: Accounts & data -> Connect AWS or GCP -> AWS"
echo "  Access key ID     : $key_id"
echo "  Secret access key : $secret   (shown once - do not share it anywhere else)"
if [[ -n "$CUR_BUCKET" ]]; then
  echo "  CUR 2.0 bucket    : $CUR_BUCKET"
  echo "  CUR prefix        : $CUR_PREFIX"
fi
echo
echo "If Cost Explorer has never been opened in this account, open it once in the console; AWS takes up to 24 h to enable it."
