"""Tenant-scoped REST API: costs, allocation, budgets, alerts, recommendations, inventory, reports (FR-1104)."""
from __future__ import annotations

import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import alerts, audit, budgets, inventory
from ..analytics import allocation, costs
from ..analytics.filters import Scope
from ..analytics.forecast import multi_month_forecast
from ..config import get_settings
from ..db import Database, new_id
from ..ingest.loader import ingest_files
from ..recommendations import engine
from ..reports import boq, builder, exports, render
from ..sync import post_sync
from ..tenants import get_tenant, list_accounts
from ..timeutil import add_months, month_end, month_start, parse_month
from .deps import TenantCtx, db_dep, scope_dep, tenant_access

router = APIRouter(prefix="/api/tenants/{tenant_id}")
VIEW, ANALYST, ADMIN = tenant_access("viewer"), tenant_access("analyst"), tenant_access("tenant_admin")
Fmt = Literal["json", "csv", "xlsx"]


def tabular(rows: list[dict], fmt: str, name: str):
    """Any list endpoint can be exported with ?format=csv|xlsx (FR-903)."""
    if fmt == "csv":
        return Response(exports.to_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    if fmt == "xlsx":
        return Response(exports.to_xlsx(rows, name), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
    return rows


def _default_range(db: Database, scope: Scope) -> Scope:
    if scope.date_from and scope.date_to:
        return scope
    as_of = costs.data_as_of(db, scope.tenant_id) or date.today()
    return scope.replace(date_from=scope.date_from or month_start(as_of), date_to=scope.date_to or as_of)


# ---- tenant ----------------------------------------------------------------------------------------

@router.get("")
def tenant_info(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    t = get_tenant(db, ctx.tenant_id)
    return {**t, "role": ctx.role, "data_as_of": costs.data_as_of(db, ctx.tenant_id), "accounts": list_accounts(db, ctx.tenant_id)}


# ---- costs -----------------------------------------------------------------------------------------

@router.get("/costs/summary")
def cost_summary(as_of: date | None = None, ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep),
                 db: Database = Depends(db_dep)):
    return costs.summary(db, scope.replace(date_from=None, date_to=None), as_of)


@router.get("/costs/trend")
def cost_trend(sensitivity: float = 3.5, ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep),
               db: Database = Depends(db_dep)):
    return costs.trend(db, _default_range(db, scope), sensitivity)


@router.get("/costs/month/{month}")
def cost_month(month: str, ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep), db: Database = Depends(db_dep)):
    return costs.month_view(db, scope.replace(date_from=None, date_to=None), parse_month(month))


@router.get("/costs/group")
def cost_group(dims: list[str] = Query(default=["service"]), limit: int | None = None, format: Fmt = "json",
               ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep), db: Database = Depends(db_dep)):
    return tabular(costs.group_by(db, _default_range(db, scope), dims, limit), format, "cost-" + "-".join(d.replace(":", "_") for d in dims))


@router.get("/costs/drilldown")
def cost_drilldown(level: str = "account", format: Fmt = "json", ctx: TenantCtx = Depends(VIEW),
                   scope: Scope = Depends(scope_dep), db: Database = Depends(db_dep)):
    return tabular(costs.drilldown(db, _default_range(db, scope), level), format, f"drilldown-{level}")


@router.get("/costs/unit-economics")
def unit_econ(format: Fmt = "json", ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep), db: Database = Depends(db_dep)):
    return tabular(costs.unit_economics(db, _default_range(db, scope)), format, "unit-economics")


@router.get("/costs/anomalies")
def cost_anomalies(dimension: str = "service", days: int = 45, sensitivity: float = 3.5, ctx: TenantCtx = Depends(VIEW),
                   scope: Scope = Depends(scope_dep), db: Database = Depends(db_dep)):
    as_of = scope.date_to or costs.data_as_of(db, ctx.tenant_id) or date.today()
    return costs.anomalies_by(db, scope.replace(date_from=None, date_to=None), dimension, as_of, days, sensitivity)


@router.get("/costs/forecast")
def cost_forecast(months: int = Query(6, ge=1, le=12), ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep),
                  db: Database = Depends(db_dep)):
    as_of = costs.data_as_of(db, ctx.tenant_id) or date.today()
    last_closed = month_start(as_of) if as_of == month_end(as_of) else add_months(month_start(as_of), -1)
    hist = costs.monthly_totals(db, scope.replace(date_from=add_months(last_closed, -11), date_to=month_end(last_closed)))
    return {"history": [{"month": m.strftime("%Y-%m"), "cost": v} for m, v in hist], "forecast": multi_month_forecast(hist, months),
            "method": "linear trend over closed months, 80% range"}


@router.get("/costs/resources")
def cost_resources(limit: int = 500, format: Fmt = "json", ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep),
                   db: Database = Depends(db_dep)):
    return tabular(costs.resource_explorer(db, _default_range(db, scope), limit), format, "resource-explorer")


@router.get("/costs/tag-coverage")
def tag_cov(ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep), db: Database = Depends(db_dep)):
    return costs.tag_coverage(db, _default_range(db, scope), get_tenant(db, ctx.tenant_id)["required_tags"])


# ---- allocation ------------------------------------------------------------------------------------

class CostCenterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    rules: list[dict]
    percent: float = 100


@router.get("/cost-centers")
def cc_list(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return allocation.list_centers(db, ctx.tenant_id)


@router.post("/cost-centers", status_code=201)
def cc_create(body: CostCenterIn, ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    cid = allocation.create_center(db, ctx.tenant_id, body.name, body.rules, body.percent)
    audit.record(db, "cost_center.create", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=cid, detail=body.model_dump(), ip=ctx.ip)
    return {"id": cid}


@router.delete("/cost-centers/{cc_id}", status_code=204)
def cc_delete(cc_id: str, ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    db.execute("DELETE FROM cost_centers WHERE tenant_id = ? AND id = ?", [ctx.tenant_id, cc_id])
    audit.record(db, "cost_center.delete", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=cc_id, ip=ctx.ip)


@router.get("/allocation")
def alloc(format: Fmt = "json", ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep), db: Database = Depends(db_dep)):
    return tabular(allocation.allocate(db, _default_range(db, scope)), format, "cost-centers")


# ---- budgets & alerts ------------------------------------------------------------------------------

class BudgetIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scope_type: str = "tenant"
    scope_value: str | None = None
    period: str = "monthly"
    amount: float = Field(gt=0)
    thresholds: list[float] = [50, 80, 100]
    forecast_alert: bool = True


class BudgetPatch(BaseModel):
    name: str | None = None
    scope_type: str | None = None
    scope_value: str | None = None
    period: str | None = None
    amount: float | None = Field(default=None, gt=0)
    thresholds: list[float] | None = None
    forecast_alert: bool | None = None


@router.get("/budgets")
def budget_list(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    fy = get_tenant(db, ctx.tenant_id)["fiscal_year_start"]
    return [budgets.status(db, ctx.tenant_id, b, None, fy) for b in budgets.list_budgets(db, ctx.tenant_id)]


@router.get("/budgets/calibration")
def budget_calibration(scope_type: str = "tenant", scope_value: str | None = None, period: str = "monthly", amount: float = 0,
                       ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    """Preview used by the budget form: warns before a budget below trailing spend is saved (FR-603)."""
    as_of = costs.data_as_of(db, ctx.tenant_id) or date.today()
    budgets.check_scope(db, ctx.tenant_id, scope_type, scope_value)
    return budgets.calibration(db, ctx.tenant_id, scope_type, scope_value, period, amount, as_of)


@router.post("/budgets", status_code=201)
def budget_create(body: BudgetIn, ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    res = budgets.create_budget(db, ctx.tenant_id, created_by=ctx.principal.email, **body.model_dump())
    audit.record(db, "budget.create", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=res["id"], detail=body.model_dump(), ip=ctx.ip)
    return res


@router.patch("/budgets/{budget_id}")
def budget_update(budget_id: str, body: BudgetPatch, ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    res = budgets.update_budget(db, ctx.tenant_id, budget_id, **body.model_dump())
    audit.record(db, "budget.update", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=budget_id,
                 detail=body.model_dump(exclude_none=True), ip=ctx.ip)
    return res


@router.delete("/budgets/{budget_id}", status_code=204)
def budget_delete(budget_id: str, ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    budgets.delete_budget(db, ctx.tenant_id, budget_id)
    audit.record(db, "budget.delete", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=budget_id, ip=ctx.ip)


@router.post("/budgets/evaluate")
def budget_evaluate(ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    return {"raised": budgets.evaluate(db, ctx.tenant_id)}


@router.get("/alerts")
def alert_list(include_acknowledged: bool = True, ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return alerts.list_alerts(db, ctx.tenant_id, include_acknowledged)


@router.post("/alerts/{alert_id}/ack")
def alert_ack(alert_id: str, ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    if not alerts.acknowledge(db, ctx.tenant_id, alert_id, ctx.principal.email):
        raise HTTPException(404, "alert not found")
    return {"ok": True}


class ChannelIn(BaseModel):
    kind: Literal["email", "teams", "webhook"]
    target: str = Field(min_length=3, max_length=2000)


@router.get("/alert-channels")
def channel_list(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return alerts.list_channels(db, ctx.tenant_id)


@router.post("/alert-channels", status_code=201)
def channel_add(body: ChannelIn, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    cid = alerts.add_channel(db, ctx.tenant_id, body.kind, body.target)
    audit.record(db, "alert_channel.create", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=cid,
                 detail={"kind": body.kind}, ip=ctx.ip)
    return {"id": cid}


@router.delete("/alert-channels/{channel_id}", status_code=204)
def channel_delete(channel_id: str, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    alerts.delete_channel(db, ctx.tenant_id, channel_id)
    audit.record(db, "alert_channel.delete", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=channel_id, ip=ctx.ip)


@router.post("/alert-channels/test")
def channel_test(ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    item = {"id": new_id(), "kind": "test", "severity": "info", "message": "CloudArc test notification"}
    return alerts.dispatch(db, ctx.tenant_id, [item])


# ---- recommendations ---------------------------------------------------------------------------------

class RecStatus(BaseModel):
    status: Literal["open", "accepted", "implemented", "verified", "dismissed"]
    reason: str | None = None


@router.get("/recommendations")
def rec_list(status: str | None = None, format: Fmt = "json", ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return tabular(engine.list_recs(db, ctx.tenant_id, status), format, "recommendations")


@router.get("/recommendations/savings")
def rec_savings(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return engine.savings_summary(db, ctx.tenant_id)


@router.post("/recommendations/run")
def rec_run(ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    return engine.run(db, ctx.tenant_id)


@router.post("/recommendations/{rec_id}/status")
def rec_status(rec_id: str, body: RecStatus, ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    res = engine.transition(db, ctx.tenant_id, rec_id, body.status, body.reason)
    audit.record(db, "recommendation.status", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=rec_id,
                 detail=body.model_dump(), ip=ctx.ip)
    return res


# ---- inventory ---------------------------------------------------------------------------------------

@router.get("/inventory")
def inv_list(include_removed: bool = False, format: Fmt = "json", ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return tabular(inventory.list_resources(db, ctx.tenant_id, include_removed), format, "inventory")


@router.get("/inventory/changes")
def inv_changes(since: date | None = None, ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return inventory.changes(db, ctx.tenant_id, since)


@router.get("/inventory/insights")
def inv_insights(idle_days: int = 14, ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return inventory.insights(db, ctx.tenant_id, costs.data_as_of(db, ctx.tenant_id) or date.today(), idle_days)


@router.get("/inventory/metrics")
def inv_metrics(resource_id: str, metric: str, days: int = 30, ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    as_of = costs.data_as_of(db, ctx.tenant_id) or date.today()
    return inventory.metric_series(db, ctx.tenant_id, resource_id, metric, as_of - timedelta(days=days - 1), as_of)


# ---- ingestion -----------------------------------------------------------------------------------------

@router.post("/ingest")
async def ingest_upload(file: UploadFile = File(...), provider: Literal["azure", "aws", "gcp"] | None = None,
                        ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    suffix = Path(file.filename or "upload.csv").suffix.lower()
    if suffix not in {".csv", ".gz", ".parquet"}:
        raise HTTPException(400, "upload a .csv, .csv.gz or .parquet billing export")
    with tempfile.TemporaryDirectory(prefix="cloudarc-upload-") as tmp:
        path = Path(tmp) / ("upload" + ("".join(Path(file.filename or "").suffixes[-2:]) or suffix))
        with path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        res = ingest_files(db, ctx.tenant_id, [path], provider, source=f"upload:{file.filename}")
    audit.record(db, "ingest.upload", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=file.filename,
                 detail=res.__dict__, ip=ctx.ip)
    post_sync(db, ctx.tenant_id)
    return {**res.__dict__, "reconciled": res.reconciled}


@router.get("/ingestions")
def ingestion_runs(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return db.query("SELECT id, provider, source, status, rows_loaded, source_total, loaded_total, date_from, date_to, error, "
                    "started_at, finished_at FROM ingestion_runs WHERE tenant_id = ? ORDER BY started_at DESC LIMIT 100", [ctx.tenant_id])


class ReconcileIn(BaseModel):
    month: str
    provider_total: float = Field(description="Closed-month total shown by Azure Cost Management, in base currency")


@router.post("/reconcile")
def reconcile(body: ReconcileIn, ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep), db: Database = Depends(db_dep)):
    """Compare a closed month with the provider's own total; acceptance threshold ±0.5% (NFR-06)."""
    m = parse_month(body.month)
    ours = costs.total(db, scope.replace(date_from=m, date_to=month_end(m)))
    diff = ours - body.provider_total
    pct = 100 * diff / body.provider_total if body.provider_total else None
    return {"month": body.month, "platform_total": round(ours, 2), "provider_total": body.provider_total, "difference": round(diff, 2),
            "difference_pct": round(pct, 4) if pct is not None else None, "within_tolerance": pct is not None and abs(pct) <= 0.5}


# ---- reports -----------------------------------------------------------------------------------------

@router.post("/reports/monthly")
def report_monthly(month: str | None = None, format: Literal["docx", "pdf"] = "docx", ctx: TenantCtx = Depends(VIEW),
                   db: Database = Depends(db_dep)):
    m = parse_month(month) if month else None
    with tempfile.TemporaryDirectory(prefix="cloudarc-report-") as tmp:
        rep = builder.build(db, ctx.tenant_id, m, Path(tmp))
        out_dir = get_settings().reports_dir / ctx.tenant_id
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"Cost-Governance-Report-{rep.month}-{new_id()[:6]}.{format}"
        path = out_dir / name
        (render.to_docx if format == "docx" else render.to_pdf)(rep, path)
    rid = new_id()
    db.execute("INSERT INTO report_runs (id, tenant_id, month, format, path, created_by) VALUES (?, ?, ?, ?, ?, ?)",
               [rid, ctx.tenant_id, rep.month, format, str(path), ctx.principal.email])
    audit.record(db, "report.generate", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=rid,
                 detail={"month": rep.month, "format": format, "figures": rep.figures}, ip=ctx.ip)
    media = "application/pdf" if format == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return FileResponse(path, media_type=media, filename=name, headers={"X-Report-Id": rid})


@router.get("/reports")
def report_list(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return db.query("SELECT id, month, format, created_by, created_at FROM report_runs WHERE tenant_id = ? ORDER BY created_at DESC",
                    [ctx.tenant_id])


@router.get("/reports/{report_id}/download")
def report_download(report_id: str, ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    row = db.one("SELECT path, format FROM report_runs WHERE tenant_id = ? AND id = ?", [ctx.tenant_id, report_id])
    if not row or not Path(row["path"]).exists():
        raise HTTPException(404, "report not found")
    return FileResponse(row["path"], filename=Path(row["path"]).name)


@router.get("/exports/{name}")
def export_standard(name: str, format: Fmt = "csv", ctx: TenantCtx = Depends(VIEW), scope: Scope = Depends(scope_dep),
                    db: Database = Depends(db_dep)):
    if name not in exports.STANDARD_REPORTS:
        raise HTTPException(404, f"unknown report; expected one of {exports.STANDARD_REPORTS}")
    rows = exports.standard_report(db, name, _default_range(db, scope))
    audit.record(db, "report.export", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=name, detail={"format": format}, ip=ctx.ip)
    return tabular(rows, format, name.replace("_", "-"))


# ---- BOQ / estimate vs actual ------------------------------------------------------------------------

@router.get("/boq")
def boq_list(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return boq.list_boqs(db, ctx.tenant_id)


@router.post("/boq", status_code=201)
async def boq_upload(name: str, file: UploadFile = File(...), ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    with tempfile.NamedTemporaryFile(suffix=".xlsx") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp.flush()
        rows = boq.parse_calculator_xlsx(tmp.name)
    n = boq.import_boq_rows(db, ctx.tenant_id, name, rows)
    audit.record(db, "boq.import", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=name, detail={"items": n}, ip=ctx.ip)
    return {"boq_name": name, "items": n, "monthly_total": round(sum(r["monthly_cost"] for r in rows), 2)}


@router.get("/boq/variance")
def boq_variance(name: str, month: str | None = None, format: Fmt = "json", ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    m = parse_month(month) if month else builder.default_report_month(db, ctx.tenant_id)
    v = boq.variance(db, ctx.tenant_id, name, m)
    if format != "json":
        rows = [{k: val for k, val in line.items() if k != "actual_by_region"} | {f"actual_{r}": line["actual_by_region"].get(r, 0) for r in v["regions"]}
                for line in v["lines"]]
        return tabular(rows, format, "boq-variance")
    return v


# ---- audit -------------------------------------------------------------------------------------------

@router.get("/audit")
def tenant_audit(ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    return audit.query(db, ctx.tenant_id)
