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

## AWS: Cost and Usage Report upload

CloudArc reads AWS cost from CUR files. There is no AWS API connector yet.

1. **Create a data export.** In the AWS Billing console, open **Data Exports › Create › Standard data export (CUR 2.0)**. Choose daily granularity, include resource IDs, use CSV (gzip) format, and write to an S3 bucket. The first delivery can take up to 24 hours. AWS can backfill earlier months through a support request.
2. **Download the files** from S3 at `…/data/BILLING_PERIOD=YYYY-MM/*.csv.gz`.
3. **Upload** them in CloudArc under **Accounts & data › Upload a billing export**. The provider is detected automatically. Re-uploading a month replaces it, so monthly re-uploads never duplicate cost.

Costs are loaded amortized: RI and Savings Plan usage shows its effective cost. USD is converted to INR using `CLOUDARC_FX_DEFAULTS`, or dated rates loaded through `POST /api/fx-rates`.

## GCP: Cloud Billing export upload

1. **Enable the BigQuery export.** In Cloud Billing, open **Billing export › BigQuery export** and enable *Detailed usage cost*, or at least *Standard usage cost*. Data accumulates from the day you enable it.
2. **Run the flattening query.** Open `deploy/gcp/billing-export-for-cloudarc.sql` in the BigQuery console, set your export table name, and run it. The query flattens labels and credits, which BigQuery cannot export as CSV otherwise.
3. **Save the results as CSV** (use `EXPORT DATA` to Cloud Storage for large months) and upload the file in CloudArc under **Accounts & data › Upload a billing export**.

Net cost is cost plus credits, which matches the Cloud Billing console's "cost after credits" figure.
