# Requirements traceability: BRD/FRD v0.1

Status legend: ✅ implemented and tested · 🟡 partial (see note) · ⏳ not yet (planned phase in brackets)

## Onboarding & integration

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-101 | Onboard Azure subscription via Entra app registration, read-only roles | ✅ | `api/admin.py` onboarding, `connectors/azure.py` | `test_azure_sync.py::test_onboarding_wizard_api` |
| FR-102 | Guided wizard listing roles; validates before completing | ✅ | `/api/onboarding/requirements`, `/onboarding/validate`, console › Accounts | same |
| FR-103 | Encrypted secret store; never logged or displayed | ✅ | `security/secrets.py` (AES-256-GCM), `tenants.py`, `RedactingFilter` | `test_isolation_security.py` (secret tests) |
| FR-104 | Auto-discover subscriptions; selective enablement | ✅ | `AzureClient.list_subscriptions`, wizard step 3, file-based auto-registration | onboarding test |
| FR-105 | Integration health: last sync, duration, errors, permission drift | ✅ | `cloud_accounts` health columns, permission re-check on every sync, `/accounts` | `test_sync_account_end_to_end…` |
| FR-106 | Secret rotation without re-onboarding | ✅ | `/credentials/{id}/rotate` (new secret validated first) | onboarding test |

## Cost data ingestion

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-201 | Daily cost at resource-meter granularity | ✅ | Cost Details API → `ingest/azure.py` | `test_ingest.py`, `test_azure_sync.py` |
| FR-202 | Date, subscription, RG, resource, service, meter cat/sub/name, qty, unit, unit price, cost, currency, tags, location | ✅ | `cost_records` schema | `test_azure_ea_normalization` |
| FR-203 | Scheduled daily sync and on-demand sync | ✅ | `sync.Scheduler`, `/accounts/{id}/sync`, `cloudarc sync` | retry test |
| FR-204 | Late/restated data via idempotent 3–5 day look-back | ✅ | `ingest/loader.py` window replace; `SYNC_LOOKBACK_DAYS=5` | `test_reingest_is_idempotent…`, re-sync test |
| FR-205 | Billing currency stored; INR display; configurable FX | 🟡 | `cost` + `cost_base`; dated `fx_rates` (as-of join) plus defaults. *No automated FX feed yet: rates are loaded via `POST /api/fx-rates`.* | `test_azure_mca_camelcase_columns_and_fx` |
| FR-206 | Configurable retention (default 24 months) | ✅ | `sync.apply_retention`, `RETENTION_MONTHS` | — |
| FR-207 | Reservation / savings-plan utilization | ⏳ (P3) | Pricing model captured per line; the utilization API is not yet ingested | — |

## Inventory

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-301 | Resource Graph discovery | ✅ | `AzureClient.inventory` | sync test |
| FR-302 | Link inventory to cost | ✅ | `costs.resource_explorer` | report/export tests |
| FR-303 | Flag no-cost (idle) and cost-without-tags resources | ✅ | `inventory.insights` | — |
| FR-304 | Track added/removed/resized | ✅ | `resource_changes` | — |

## Dashboards & analysis

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-401 | Org dashboard: MTD, previous month, forecast, MoM, top services/resources, trend | ✅ | `costs.summary`, console › Dashboard | `test_summary_labels…` |
| FR-402 | Drill-down subscription → RG → resource → meter | ✅ | `/costs/drilldown`, console › Cost explorer | — |
| FR-403 | Daily trend with min/max/avg and anomaly markers | ✅ | `costs.trend` | anomaly tests |
| FR-404 | MTD always labelled; never compared with a full month | ✅ | `is_partial` and labels; like-for-like MoM | `test_summary_labels…` |
| FR-405 | Filter by date, subscription, service, location, tag, RG | ✅ | `api/deps.scope_dep` | — |
| FR-406 | Unit economics (₹/unit per meter) | ✅ | `costs.unit_economics` | export test |
| FR-407 | Saved views / shareable links | ⏳ (P2) | — | — |

## Allocation & tagging

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-501 | Allocate by subscription, RG, service, location, any tag | ✅ | `group_by` dimensions incl. `tag:<key>` | `test_group_by_tag…` |
| FR-502 | Rule-based cost centers with % splits | ✅ | `analytics/allocation.py` | `test_cost_center_allocation_sums_to_total` |
| FR-503 | Tag coverage and untagged spend | ✅ | `costs.tag_coverage` | `test_tag_coverage_lists_violations` |
| FR-504 | Required-tag policy per tenant; violations | ✅ | `tenants.required_tags`, Tag Compliance export | same |

## Budgets, alerts & anomalies

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-601 | Budgets per scope/period with multiple thresholds | ✅ | `budgets.py` (tenant, account, RG, service, tag, cost center; monthly, quarterly, annual on the fiscal year) | `test_budget_scopes`, fiscal test |
| FR-602 | Evaluate after each sync; e-mail and Teams; extensible | ✅ | `sync.post_sync`, `alerts.py` (SMTP, Teams Adaptive Card, generic webhook) | — |
| FR-603 | Calibration: warn when budget < trailing 3-month average | ✅ | `budgets.calibration`, live preview in the form, alert | `test_budget_calibration…` |
| FR-604 | Forecast-based alerting | ✅ | `budget_forecast` alerts | `test_forecast_alert_fires_before_actual_breach` |
| FR-605 | Anomaly detection per subscription and service, configurable sensitivity | ✅ | `analytics/anomaly.py` (robust z-score), `anomalies_by` | anomaly tests |
| FR-606 | Alert history and acknowledgement | ✅ | `alerts` table, `/alerts/{id}/ack` | `test_acknowledge_alert` |

## Forecasting

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-701 | Month-end forecast with low/high range | ✅ | `forecast.month_end_forecast` | `test_month_end_forecast_math` |
| FR-702 | 3/6/12-month forecast, subscription and tenant | ✅ | `/costs/forecast?months=` (with account filter) | `test_multi_month_forecast…` |
| FR-703 | Scenario modelling | ⏳ (P3) | — | — |

## Optimization recommendations

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-801 | VM right-sizing from 14–30 days of CPU/memory/credits; B→D series | ✅ | `rules.vm_rightsizing` | `test_expected_recommendations…`, `test_no_metrics_no_rightsizing` |
| FR-802 | RI / savings-plan candidates with 1y/3y savings from retail prices | ✅ | `rules.reserved_instances`, `price_catalog` | same |
| FR-803 | Unattached disks, unused IPs, stopped-allocated VMs, empty RGs (+ old snapshots) | ✅ | `rules.idle_resources` | same |
| FR-804 | SQL DTU tier review / serverless | ✅ | `rules.sql_tier_review` | same |
| FR-805 | Managed disk tier review | ✅ | `rules.disk_tier_review` | same |
| FR-806 | Advisor ingestion, de-duplicated | ✅ | `engine.merge_advisor` | `test_advisor_items_merge_into_native` |
| FR-807 | Lifecycle open → accepted → implemented → verified / dismissed with reason | ✅ | `engine.transition`, `verify_realized_savings` | lifecycle and realized-savings tests |
| FR-808 | AI narrative using platform figures only | ✅ | `reports/narrative.py` (deterministic; optional LLM with a number check) | — |

## Reporting & export

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-901 | Monthly Cost & Governance Report, DOCX and PDF, iSource structure | ✅ | `reports/builder.py`, `render.py` (all 10 sections + BOQ variance) | `test_report_reconciles_with_dashboard_and_renders` |
| FR-902 | Scheduled report e-mail | ⏳ (P2) | Report generation exists; the scheduled e-mail job is not yet wired | — |
| FR-903 | Export any table to CSV/XLSX | ✅ | `?format=csv\|xlsx` on list endpoints, console buttons | export test |
| FR-904 | Standard reports (Cost Allocation, Resource Explorer, Unit Economics, Budget vs Actual, Tag Compliance) | ✅ | `/exports/{name}` | `test_api_upload_reports_and_exports` |
| FR-905 | Report figures reconcile exactly with dashboards | ✅ | Shared analytics functions | report test |

## Multi-tenancy, access & operations

| ID | Requirement | Status | Where | Tests |
|---|---|---|---|---|
| FR-1001 | Tenant isolation at API and query level | ✅ | `tenant_access`, `Scope.where` | `test_every_tenant_route_denies_unassigned_tenant` and others |
| FR-1002 | Platform Admin, Tenant Admin, Analyst, Viewer | ✅ | `security/auth.py` | `test_viewer_is_read_only` |
| FR-1003 | Entra ID SSO; MFA enforced by the IdP | 🟡 | JWT validation (JWKS, aud, iss, exp) plus MSAL sign-in in the console. *Needs validation against the iSource Entra tenant.* | — |
| FR-1004 | Audit log (login, onboarding, budgets, credentials, reports) | ✅ | `audit.py` | `test_audit_log_records_actions` |
| FR-1005 | Client-facing read-only portal | 🟡 (P3) | Client users can be given the Viewer role on their tenant; no separate branding yet | — |
| FR-1101 | Sync monitoring, retry, back-off, failure notification | ✅ | `sync.run_due_jobs` (3 attempts, 5/10 min back-off, `sync_failure` alert) | `test_failed_sync_retries…` |
| FR-1102 | Configurable currency, fiscal year, retention, alert channels | ✅ | Tenant settings, `.env`, `/alert-channels` | — |
| FR-1103 | Health endpoint | ✅ | `GET /api/health` | — |
| FR-1104 | REST API for all reads | ✅ | OpenAPI at `/docs` | — |

## Non-functional requirements

| ID | Requirement | Status | Notes |
|---|---|---|---|
| NFR-01 | Security | ✅ | AES-256-GCM secrets; TLS 1.2+ via Caddy; parameterized SQL and whitelisted dimensions; security headers and CSP; `pip-audit` in CI |
| NFR-02 | Data isolation verified by automated tests | ✅ | Isolation suite runs in CI |
| NFR-03 | Dashboard < 3 s; 50-subscription sync < 60 min | 🟡 | Architecture sized for it; a load test with production-scale data is still outstanding |
| NFR-04 | 50 subscriptions / 20 tenants | 🟡 | See the scaling path in ARCHITECTURE.md; not yet load-tested |
| NFR-05 | No lost or duplicated data on sync failure | ✅ | Transactional window replace; retry queue |
| NFR-06 | Reconcile within ±0.5% for a closed month | 🟡 | Per-load reconciliation plus the `/reconcile` check are built; **acceptance needs one real client subscription** |
| NFR-07 | Containerized, single-command Ubuntu deployment | ✅ | `Dockerfile`, `docker-compose.yml`, `deploy/install.sh` |
| NFR-08 | Structured logs, metrics, alerting on errors | 🟡 | JSON logs with request IDs, health endpoint, sync-failure alerts; no Prometheus endpoint yet |
| NFR-09 | Daily backups; documented restore | ✅ | `deploy/backup.sh`, `cloudarc backup/restore`, OPERATIONS.md |
| NFR-10 | Automated tests + CI; API docs | ✅ | 49 tests; GitHub Actions; OpenAPI |
| NFR-11 | INR ₹ formatting, dd MMM yyyy | ✅ | `timeutil.fmt_inr` (lakh/crore grouping), `fmt_date` |

## Beyond the BRD

- **Approved estimate (BOQ) vs actual.** This automates the "Azure Cost Review — BOQ vs first month" workbook. It imports an Azure Pricing Calculator `.xlsx` and shows variance by component and region, including actual spend with no BOQ line.
- **AWS CUR (legacy and 2.0) and GCP billing export ingestion** (BR-07 / Phase 3 groundwork).
- **Over-privilege detection** during onboarding and on every sync.
