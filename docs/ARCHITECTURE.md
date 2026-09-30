# Architecture

```
            ┌──────────── Web console (vanilla JS + Chart.js) ────────────┐
 Entra ID ──┤  OIDC / MSAL          REST API (FastAPI, /docs)              │
            └──────────────────────────────┬───────────────────────────────┘
                                           │ tenant_access(role) on every tenant route
   ┌────────────┬────────────┬─────────────┼──────────────┬──────────────┬─────────────┐
   │ analytics  │ budgets &  │ recommend-  │ reports       │ inventory    │ onboarding, │
   │ (summary,  │ alerts     │ ations      │ (DOCX/PDF,    │ & metrics    │ credentials,│
   │ trend,     │ (calib.,   │ (rules +    │ exports, BOQ  │              │ audit       │
   │ forecast,  │ forecast,  │ Advisor,    │ variance)     │              │             │
   │ anomalies, │ anomalies, │ lifecycle)  │               │              │             │
   │ allocation)│ channels)  │             │               │              │             │
   └─────┬──────┴─────┬──────┴──────┬──────┴──────┬───────┴──────┬───────┴──────┬──────┘
         └────────────┴─────────────┴─── DuckDB (columnar, tenant_id on every table) ───┘
                                           ▲
             ingest/loader.py  ◄───────────┤ idempotent replace of (account, day) windows
     ┌─────────────┬──────────────┬────────┴────────┐
     │ Azure cost  │ AWS CUR /    │ GCP billing     │  adapters emit SQL → normalized schema
     │ details     │ CUR 2.0      │ export          │
     └──────▲──────┴──────────────┴─────────────────┘
            │  sync.py: scheduler thread → job queue → retry/back-off → post-sync (recs, budgets, anomalies)
     connectors/azure.py: Cost Details, Resource Graph, Monitor, Advisor, Authorization, Retail Prices
```

## Key design decisions

**Normalized, provider-neutral cost schema (BR-07).** The cost schema follows FOCUS concepts: account, charge date, resource, service, meter category/sub-category/name, location, quantity, unit, unit price, cost, currency, base-currency cost, pricing model, charge type and tags. Adding a cloud means adding an adapter (`cloudarc/ingest/*.py`) that emits one SQL `SELECT`. The rest of the platform does not change.

**Ingestion runs in SQL, not Python.** DuckDB reads CSV, gzip and Parquet exports directly, and each adapter maps columns with SQL expressions. Multi-GB CUR or Azure export files stream through the columnar engine without building Python objects per row.

**Idempotency (FR-204, NFR-05).** A load deletes and re-inserts every `(account, date range)` window present in the incoming data, in one transaction. A re-sync, a late restatement or a duplicated upload therefore never duplicates cost. Every load records `source_total` against `loaded_total` (reconciliation). `POST /reconcile` compares a closed month with the provider's own total against the ±0.5% acceptance criterion.

**Tenant isolation (FR-1001, NFR-02).**

1. Every tenant-owned table carries `tenant_id`.
2. All analytics go through `Scope.where()`, which always starts with `tenant_id = ?`.
3. Every tenant route depends on `tenant_access(min_role)`. An unassigned tenant returns the same 404 as a tenant that doesn't exist, so tenant IDs can't be probed.
4. `tests/test_isolation_security.py` walks the OpenAPI schema. It asserts that a user without access is refused on every tenant-scoped operation (all routes and methods, currently more than 40), so a newly added route can't skip the check unnoticed.

**Month-to-date honesty (FR-404).** Partial periods carry `is_partial` and a label such as "September 2026 month-to-date (01 Sep 2026 – 29 Sep 2026, partial)". Month-over-month change is only computed like-for-like: 1–29 Sep against 1–29 Aug.

**Single source of truth for figures (FR-905).** The report builder calls the same `analytics.costs` functions as the API. A test asserts that report and dashboard figures are identical.

**Recommendations need evidence.** Utilization rules emit nothing without at least 14 days of metrics ("no metrics → no recommendation"). Every item carries evidence, an estimated monthly saving (from retail prices when cached, otherwise a stated basis), confidence, effort and risk. Re-runs refresh open items, never reopen dismissed ones, and mark vanished conditions `resolved`. For implemented items, realized savings are measured by comparing 30 days of cost before and after implementation.

**Security.**

- Client secrets and alert webhook URLs are sealed with AES-256-GCM. The associated data binds each ciphertext to its tenant and credential.
- The API never returns secrets, only a masked hint.
- API tokens are stored as SHA-256 hashes.
- A redacting log filter masks secret-looking values.
- Entra ID JWTs are validated for signature, audience, issuer and expiry.
- The connector detects **over-privileged** credentials (write actions), not only missing read roles.

**Narrative text.** Report text is deterministic and generated from computed figures. An optional LLM polish step runs only when `CLOUDARC_LLM_DATA_POLICY_APPROVED=true`. Its output is rejected if it contains any number that is not in the supplied facts.

## Data model (DuckDB)

| Table | Purpose |
|---|---|
| `tenants`, `users`, `user_tenants`, `api_tokens` | Clients, identities and role assignments |
| `credentials`, `cloud_accounts` | Encrypted app registrations; subscriptions/accounts with sync health and permission drift |
| `cost_records` | Normalized daily cost lines (billing and base currency) |
| `ingestion_runs`, `sync_jobs` | Provenance, reconciliation totals, retry state |
| `resources`, `resource_changes`, `resource_metrics` | Inventory snapshots, added/removed/resized history, daily utilization |
| `price_catalog`, `fx_rates` | Retail and reservation prices; dated FX to INR (as-of join) |
| `budgets`, `alerts`, `alert_channels` | Budgets, de-duplicated alert history with acknowledgement, encrypted channel targets |
| `cost_centers` | Rule-based allocation with percentage splits |
| `recommendations` | Lifecycle-managed optimization items (native and Advisor) |
| `boq_items` | Imported pricing-calculator estimates |
| `report_runs`, `audit_log` | Generated reports; full audit trail |

## Scaling path

The Phase 1 targets are 20 tenants, 50 subscriptions and 12 months of data. That is on the order of 10⁷ cost rows, which DuckDB aggregates in milliseconds on one node.

DuckDB allows only one writer process, so the scheduler runs inside the API process. When the estate outgrows a single node:

1. Move `cost_records` to ClickHouse or to Parquet in object storage queried by DuckDB. The adapters and `Scope` already produce the SQL.
2. Move metadata tables to PostgreSQL.
3. Run the scheduler as a separate worker against the shared stores.

None of these steps change the API, the adapters or the analytics contract.
