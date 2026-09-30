# CloudArc

**Multi-tenant, multi-cloud cost management & governance platform.** It is an in-house replacement for ManageEngine CloudSpend, built to the *Cloud Cost Management & Governance Platform* BRD/FRD v0.1.

CloudArc connects to each client's cloud with **read-only** access and ingests daily cost, usage, inventory and utilization data. From that it produces:

- dashboards with drill-down
- cost allocation
- budgets with calibrated alerts
- month-end forecasts
- evidence-backed optimization recommendations
- the monthly client **Cost & Governance Report** (DOCX/PDF)

One deployment serves every client, with strict data isolation between them.

| Capability | Status |
|---|---|
| Azure (Cost Details API, Resource Graph, Monitor, Advisor, Retail Prices) | ✅ Phase 1 |
| AWS Cost and Usage Report (legacy CUR and CUR 2.0, amortized) | ✅ file ingestion |
| GCP Cloud Billing export (net of credits) | ✅ file ingestion |
| Dashboards, drill-down, unit economics, tag coverage | ✅ |
| Budgets (tenant / account / RG / service / tag / cost-center scope), calibration, forecast alerts, anomaly alerts | ✅ |
| Recommendations: VM right-sizing, B→D series, RI candidates, idle/orphaned resources, SQL tier, disk tier, Advisor merge, lifecycle and realized-savings tracking | ✅ |
| Monthly Cost & Governance Report (DOCX + PDF), standard CSV/XLSX exports | ✅ |
| Approved estimate (BOQ) vs actual variance, from an Azure Pricing Calculator export | ✅ |
| Entra ID SSO, roles, audit log, encrypted credentials | ✅ |

## Quick start (local)

```bash
make dev                  # venv + dependencies
make demo                 # synthetic demo tenants; prints API tokens (also saved to data/demo/demo_tokens.json)
make run                  # http://localhost:8080 — sign in with a printed token
make test                 # 49 tests, including the tenant-isolation suite
```

The demo loads three synthetic clients:

- **Client A:** a single-VM + SQL production subscription, shaped like the CloudSpend evaluation (≈ ₹375/day, stable, with a budget set far below real spend).
- **Client B:** a two-region production + DR estate with a pricing-calculator BOQ.
- **Client C:** an AWS CUR tenant billed in USD.

All names, IDs and figures in the demo are synthetic.

## Production (Ubuntu Server)

```bash
ADMIN_EMAIL=you@yourcompany.com ./deploy/install.sh
```

This installs Docker if it is missing and creates `.env` with a fresh AES-256 master key. It then starts CloudArc behind Caddy (TLS 1.2+) and prints the first admin's API token. Set `CLOUDARC_AUTH_MODE=entra` and the `CLOUDARC_ENTRA_*` values to enable SSO. See [docs/OPERATIONS.md](docs/OPERATIONS.md) for onboarding, backups, restores and monitoring.

## Common tasks

```bash
cloudarc create-tenant "Client name"                         # prints tenant id
cloudarc create-user eng@x.com --role <tenant_id>:analyst --token
cloudarc ingest --tenant <id> export.csv                     # Azure / AWS CUR / GCP, auto-detected; idempotent
cloudarc sync                                                # run API syncs now
cloudarc report --tenant <id> --month 2026-08 --format pdf
cloudarc backup --dir /data/backups
```

The web console covers the same tasks, plus the onboarding wizard. REST API docs are at `/docs` (OpenAPI).

## Documentation

- [Architecture](docs/ARCHITECTURE.md): components, data model, the multi-cloud adapter design and scaling path
- [Requirements traceability](docs/REQUIREMENTS_TRACEABILITY.md): every BRD/FRD requirement mapped to code and tests
- [Operations](docs/OPERATIONS.md): deployment, Azure onboarding permissions, backup/restore, monitoring
