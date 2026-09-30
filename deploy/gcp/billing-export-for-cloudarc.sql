-- Flatten the GCP Cloud Billing export (BigQuery) into the CSV layout CloudArc ingests.
-- BigQuery cannot export repeated fields (labels, credits) to CSV, so they are serialized as JSON here.
-- Replace the table with yours: <project>.<dataset>.gcp_billing_export_v1_<BILLING_ACCOUNT_ID>
-- (or ..._resource_v1_... for the detailed export, then uncomment resource_global_name).
-- Run it, then "Save results" -> CSV (or EXPORT DATA to Cloud Storage for large months) and upload
-- the file in CloudArc under Accounts & data -> Upload a billing export.
SELECT
  billing_account_id,
  service.description            AS service_description,
  sku.description                AS sku_description,
  usage_start_time,
  project.id                     AS project_id,
  project.name                   AS project_name,
  TO_JSON_STRING(labels)         AS labels,
  location.region                AS location_region,
  cost,
  currency,
  usage.amount_in_pricing_units  AS usage_amount_in_pricing_units,
  usage.pricing_unit             AS usage_pricing_unit,
  TO_JSON_STRING(credits)        AS credits,
  cost_type
  -- , resource.global_name      AS resource_global_name   -- detailed (resource-level) export only
FROM `my-project.billing_export.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX`
WHERE DATE(usage_start_time) BETWEEN DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH) AND CURRENT_DATE()
  AND project.id IS NOT NULL;
