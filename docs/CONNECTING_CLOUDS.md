# Connecting Azure, AWS and GCP

Each cloud account belongs to a **client** (tenant) in CloudArc. Create the client first: **Administration › New client**. Platform admins see every client; other users need access granted under Administration.

## Azure: API connection with daily sync

1. **Create a read-only app registration.** Do this once per Entra tenant, as an Owner of the subscription. In PowerShell or Cloud Shell:
   ```powershell
   $sub = "<subscription-id>"
   az ad sp create-for-rbac --name "CloudArc Reader" --role Reader --scopes /subscriptions/$sub
   # note appId, password, tenant from the output — do not share the password
   az role assignment create --assignee <appId> --role "Cost Management Reader" --scope /subscriptions/$sub
   az role assignment create --assignee <appId> --role "Billing Reader" --scope /subscriptions/$sub
   ```
   Use a dedicated read-only identity, not a deployment identity with Contributor. CloudArc flags credentials that have write access as *over-privileged*.
2. **Optional:** register the providers that feed right-sizing and Advisor recommendations. Cost data works without them, and the sync shows a *partial* status until they are registered:
   ```powershell
   az provider register -n Microsoft.Insights
   az provider register -n Microsoft.Advisor
   ```
3. **Connect the subscription.** In CloudArc, select the client, then go to **Accounts & data › Connect an Azure subscription**. Enter the tenant ID, app ID and password; the password is encrypted and never shown again. Select **Validate**, tick the subscription, then **Enable & start sync**.
4. **Wait for the first sync.** It starts within about a minute and backfills 3 months. Progress shows in the integration health table on the same page.

## AWS: API connection with daily sync

CloudArc reads AWS cost with a read-only IAM identity from two sources:

| Source | What it gives | Needs |
|---|---|---|
| **Cost Explorer API** | Daily amortized cost by linked account and service. About 12 months of history on the first sync. | IAM permissions only. AWS bills $0.01 per request; a daily sync makes a handful. |
| **CUR 2.0 files in S3** (optional) | Every line item, with resource IDs and tags, for resource-level views and recommendations. | A Data Export delivering to S3. |

For each billing month, CloudArc uses the CUR files when they exist and Cost Explorer otherwise. Every load replaces the days it covers, so switching sources never double-counts.

1. **Create the read-only identity.** Sign in to the **management (payer) account**, so Cost Explorer covers every linked account. Then run, in PowerShell with the AWS CLI or in AWS CloudShell:
   ```powershell
   .\deploy\aws\setup-cloudarc-reader.ps1                                   # Cost Explorer only
   .\deploy\aws\setup-cloudarc-reader.ps1 -CurBucket my-billing-exports -CurPrefix cur   # plus CUR files
   ```
   It creates the IAM user `cloudarc-reader`. Its policy allows only `ce:GetCostAndUsage`, `ce:GetDimensionValues` and, with a bucket, listing and reading that bucket's export files. It then prints an access key. The secret is shown once: paste it into CloudArc only. `setup-cloudarc-reader.sh` is the bash equivalent.
2. **Optional: CUR 2.0 export.** In **Billing and Cost Management › Data Exports › Create**:
   - Choose **Standard data export, CUR 2.0**.
   - Tick **include resource IDs**.
   - Set **daily** granularity and **Parquet** (or CSV gzip) format.
   - Choose **Overwrite existing data export file**.
   - Write to the bucket and prefix you gave the script.
   
   The first delivery can take up to 24 hours. Until then CloudArc uses Cost Explorer.
3. **Connect.** In CloudArc, go to **Accounts & data › Connect AWS or GCP › AWS**. Enter the access key, the secret, and the bucket and prefix if you have them. Select **Validate**, then **Connect & start sync**.

If Cost Explorer has never been opened in the account, open it once in the console; AWS takes up to 24 hours to enable it. To use a role in another account instead, give its ARN (and external ID, if set) in the form. CloudArc then assumes it for every sync.

## GCP: API connection with daily sync

Google has no API that returns billing cost; the **BigQuery billing export** is the source. CloudArc queries the export table daily through the BigQuery API with a read-only service account. You don't need to export or upload anything.

1. **Enable the export.** In Cloud Billing, open **Billing export › BigQuery export** and enable **Detailed usage cost**, which gives resource-level detail (Standard also works). Data accumulates from the day you enable it, so do this first.
2. **Create the read-only service account.** In Cloud Shell:
   ```bash
   PROJECT=my-billing-project DATASET=billing_export ./deploy/gcp/setup-cloudarc-reader.sh
   ```
   It grants only **BigQuery Data Viewer** on that dataset and **BigQuery Job User** on the project. It creates a JSON key and lists the export table names.
3. **Connect.** In CloudArc, go to **Accounts & data › Connect AWS or GCP › GCP**. Enter the export table, for example `my-billing-project.billing_export.gcp_billing_export_resource_v1_XXXXXX_XXXXXX_XXXXXX`, and choose the key file. Select **Validate**, then **Connect & start sync**. Afterwards, delete the key file from Cloud Shell.

Net cost is cost plus credits, which matches the Cloud Billing console's "cost after credits". Charges with no project, such as Support, appear under the billing account itself. Queries filter on the table's partitions, so each daily sync scans only recent data.

## Without API access: file uploads

AWS CUR files (CSV, CSV gzip or Parquet) and GCP exports can still be uploaded under **Accounts & data › Upload a billing export**:
- For GCP, run `deploy/gcp/billing-export-for-cloudarc.sql` in BigQuery and save the result as CSV.
- Re-uploading a month replaces it.
- USD costs are converted to INR with `CLOUDARC_FX_DEFAULTS`, or with dated rates loaded through `POST /api/fx-rates`.
