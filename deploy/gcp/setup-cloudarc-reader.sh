#!/usr/bin/env bash
# Read-only service account CloudArc uses to query the Cloud Billing export in BigQuery.
# Run in Google Cloud Shell (gcloud and bq are preinstalled) as a user who can manage IAM on the project
# that holds the billing export dataset:
#
#   PROJECT=my-billing-project DATASET=billing_export ./deploy/gcp/setup-cloudarc-reader.sh
#
# Grants only: BigQuery Data Viewer on that one dataset, and BigQuery Job User on the project (to run
# queries). Then creates a JSON key, cloudarc-reader-key.json, which you upload in CloudArc
# (Accounts & data -> Connect AWS or GCP -> GCP) and then delete from Cloud Shell. Idempotent apart from
# the key. If your organization blocks key creation (iam.disableServiceAccountKeyCreation), ask an org
# admin for an exception on this project.
set -euo pipefail
: "${PROJECT:?set PROJECT to the project that holds the billing export dataset}"
: "${DATASET:?set DATASET to the billing export dataset name}"
SA_NAME="${SA_NAME:-cloudarc-reader}"
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"

gcloud services enable bigquery.googleapis.com --project "$PROJECT" --quiet
if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "CloudArc billing reader (read-only)"
  echo "Created service account $SA"
fi

gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role roles/bigquery.jobUser --condition None --quiet >/dev/null
echo "Granted BigQuery Job User on project $PROJECT"

# Dataset-level read: add a READER entry to the dataset ACL (keeps every existing entry).
tmp=$(mktemp)
bq show --format=prettyjson "$PROJECT:$DATASET" > "$tmp"
SA="$SA" python3 - "$tmp" <<'PY'
import json, os, sys
path = sys.argv[1]
ds = json.load(open(path))
entry = {"role": "READER", "userByEmail": os.environ["SA"]}
if entry not in ds.setdefault("access", []):
    ds["access"].append(entry)
json.dump({"access": ds["access"]}, open(path, "w"))
PY
bq update --source "$tmp" "$PROJECT:$DATASET" >/dev/null
rm -f "$tmp"
echo "Granted BigQuery Data Viewer on dataset $PROJECT:$DATASET"

gcloud iam service-accounts keys create cloudarc-reader-key.json --iam-account "$SA" --project "$PROJECT"

echo
echo "Export table(s) in $PROJECT:$DATASET:"
bq ls --format=csv --max_results 1000 "$PROJECT:$DATASET" | awk -F, -v p="$PROJECT" -v d="$DATASET" 'NR>1 && $1 ~ /^gcp_billing_export/ {print "  " p "." d "." $1}'
echo
echo "In CloudArc: Accounts & data -> Connect AWS or GCP -> GCP"
echo "  Export table : one of the tables above (prefer gcp_billing_export_resource_v1_... for resource-level detail)"
echo "  Key file     : cloudarc-reader-key.json  (download it via Cloud Shell's ⋮ menu -> Download, upload it, then delete it:"
echo "                 rm cloudarc-reader-key.json)"
