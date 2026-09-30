"""Cost analytics: summaries, trends, group-by / drill-down, unit economics, tag coverage.

Dashboards, exports and the monthly report all call these functions, so a
figure shown anywhere is computed once, from one query path (FR-905).
All amounts are in the platform base currency (``cost_base``, INR by default).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from ..db import Database
from ..timeutil import (
    days_in_month,
    fmt_date,
    fmt_month,
    month_end,
    month_start,
    prev_month_start,
)
from .anomaly import detect_anomalies
from .filters import Scope, dimension_sql, tag_path
from .forecast import month_end_forecast

DRILL_LEVELS = ["account", "resource_group", "resource", "meter"]


def data_as_of(db: Database, tenant_id: str) -> date | None:
    return db.scalar("SELECT max(charge_date) FROM cost_records WHERE tenant_id = ?", [tenant_id])


def total(db: Database, scope: Scope) -> float:
    where, params = scope.where()
    return float(db.scalar(f"SELECT COALESCE(SUM(c.cost_base), 0) FROM cost_records c WHERE {where}", params))


def group_by(
    db: Database,
    scope: Scope,
    dims: list[str],
    limit: int | None = None,
    order_by_cost: bool = True,
) -> list[dict]:
    params: list = []
    exprs = [f"{dimension_sql(d, params)} AS \"{d}\"" for d in dims]
    where, wparams = scope.where()
    params += wparams
    group = ", ".join(str(i + 1) for i in range(len(dims)))
    order = "cost DESC" if order_by_cost else group
    sql = (
        f"SELECT {', '.join(exprs)}, SUM(c.cost_base) AS cost, SUM(c.quantity) AS quantity, "
        f"SUM(SUM(c.cost_base)) OVER () AS grand_total "
        f"FROM cost_records c WHERE {where} GROUP BY {group} ORDER BY {order}"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = db.query(sql, params)
    for r in rows:
        gt = r.pop("grand_total") or 0
        r["cost"] = round(r["cost"] or 0, 2)
        r["share_pct"] = round(100 * r["cost"] / gt, 2) if gt else 0.0
    _decorate_accounts(db, scope.tenant_id, rows)
    return rows


def _decorate_accounts(db: Database, tenant_id: str, rows: list[dict]) -> None:
    if not rows or "account" not in rows[0]:
        return
    names = {
        r["id"]: r["name"] or r["external_id"]
        for r in db.query("SELECT id, name, external_id FROM cloud_accounts WHERE tenant_id = ?", [tenant_id])
    }
    for r in rows:
        r["account_name"] = names.get(r["account"], r["account"])


def daily_series(db: Database, scope: Scope) -> list[tuple[date, float]]:
    if not (scope.date_from and scope.date_to):
        raise ValueError("daily_series needs a bounded date range")
    where, params = scope.where()
    rows = db.query(
        f"SELECT c.charge_date AS d, SUM(c.cost_base) AS cost FROM cost_records c WHERE {where} GROUP BY 1",
        params,
    )
    by_day = {r["d"]: float(r["cost"]) for r in rows}
    out, d = [], scope.date_from
    while d <= scope.date_to:
        out.append((d, by_day.get(d, 0.0)))
        d += timedelta(days=1)
    return out


def monthly_totals(db: Database, scope: Scope) -> list[tuple[date, float]]:
    where, params = scope.where()
    rows = db.query(
        f"SELECT date_trunc('month', c.charge_date)::DATE AS m, SUM(c.cost_base) AS cost "
        f"FROM cost_records c WHERE {where} GROUP BY 1 ORDER BY 1",
        params,
    )
    return [(r["m"], round(float(r["cost"]), 2)) for r in rows]


def trend(db: Database, scope: Scope, sensitivity: float = 3.5) -> dict:
    series = daily_series(db, scope)
    values = [v for _, v in series]
    stats: dict = {}
    if values:
        avg = sum(values) / len(values)
        hi = max(series, key=lambda p: p[1])
        lo = min(series, key=lambda p: p[1])
        var = sum((v - avg) ** 2 for v in values) / len(values)
        cv = (var ** 0.5) / avg if avg else 0.0
        stats = {
            "total": round(sum(values), 2),
            "average": round(avg, 2),
            "max": round(hi[1], 2),
            "max_date": hi[0].isoformat(),
            "min": round(lo[1], 2),
            "min_date": lo[0].isoformat(),
            "range": round(hi[1] - lo[1], 2),
            "coefficient_of_variation": round(cv, 4),
            "stability": "stable" if cv < 0.05 else "moderate" if cv < 0.2 else "volatile",
        }
    return {
        "series": [{"date": d.isoformat(), "cost": round(v, 2)} for d, v in series],
        "stats": stats,
        "anomalies": detect_anomalies(series, sensitivity=sensitivity),
    }


def _period(label: str, start: date, end: date, amount: float, partial: bool) -> dict:
    return {
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "amount": round(amount, 2),
        "is_partial": partial,
    }


def summary(db: Database, base: Scope, as_of: date | None = None, top_n: int = 10) -> dict:
    """Organization / scope dashboard (FR-401) with explicit month-to-date labelling (FR-404)."""
    as_of = as_of or data_as_of(db, base.tenant_id) or date.today()
    cur_start = month_start(as_of)
    complete = as_of == month_end(as_of)
    prev_start = prev_month_start(as_of)
    prev_end = month_end(prev_start)
    lfl_end = min(prev_start + timedelta(days=as_of.day - 1), prev_end)

    mtd_scope = base.replace(date_from=cur_start, date_to=as_of)
    mtd = total(db, mtd_scope)
    prev_full = total(db, base.replace(date_from=prev_start, date_to=prev_end))
    prev_lfl = total(db, base.replace(date_from=prev_start, date_to=lfl_end))

    daily = [v for _, v in daily_series(db, mtd_scope)]
    fc = month_end_forecast(cur_start, daily, fallback_daily=prev_full / days_in_month(prev_start) if prev_full else None)

    if complete:
        cur_label = f"{fmt_month(cur_start)} (full month, actual)"
    else:
        cur_label = f"{fmt_month(cur_start)} month-to-date ({fmt_date(cur_start)} – {fmt_date(as_of)}, partial)"

    mom = None
    if prev_lfl:
        mom = {
            "basis": f"like-for-like: {fmt_date(cur_start)}–{fmt_date(as_of)} vs {fmt_date(prev_start)}–{fmt_date(lfl_end)}",
            "previous_amount": round(prev_lfl, 2),
            "change": round(mtd - prev_lfl, 2),
            "change_pct": round(100 * (mtd - prev_lfl) / prev_lfl, 2),
        }

    return {
        "as_of": as_of.isoformat(),
        "current": _period(cur_label, cur_start, as_of, mtd, not complete),
        "previous_month": _period(f"{fmt_month(prev_start)} (full month, actual)", prev_start, prev_end, prev_full, False),
        "forecast": {**fc.as_dict(), "label": f"{fmt_month(cur_start)} forecast (month-end)"},
        "month_over_month": mom,
        "top_services": group_by(db, mtd_scope, ["service"], limit=top_n),
        "top_resources": group_by(db, mtd_scope, ["resource"], limit=top_n),
        "by_account": group_by(db, mtd_scope, ["account"]),
    }


def month_view(db: Database, base: Scope, month: date, top_n: int = 10) -> dict:
    """A single (normally closed) month: totals, services, daily pattern."""
    start, end = month_start(month), month_end(month)
    scope = base.replace(date_from=start, date_to=end)
    as_of = data_as_of(db, base.tenant_id)
    partial = as_of is None or as_of < end
    return {
        "month": start.strftime("%Y-%m"),
        "label": f"{fmt_month(start)} ({'month-to-date, partial' if partial else 'full month, actual'})",
        "is_partial": partial,
        "total": round(total(db, scope), 2),
        "top_services": group_by(db, scope, ["service"], limit=top_n),
        "top_meters": group_by(db, scope, ["service", "meter"], limit=top_n),
        "by_location": group_by(db, scope, ["location"]),
        "by_resource_group": group_by(db, scope, ["resource_group"]),
        "trend": trend(db, scope),
    }


def drilldown(db: Database, scope: Scope, level: str) -> list[dict]:
    """Subscription → resource group → resource → meter (FR-402)."""
    if level not in DRILL_LEVELS:
        raise ValueError(f"level must be one of {DRILL_LEVELS}")
    return group_by(db, scope, [level])


def unit_economics(db: Database, scope: Scope) -> list[dict]:
    """Cost per unit per meter, e.g. ₹/hour for a VM size or ₹/GB egress (FR-406)."""
    where, params = scope.where()
    rows = db.query(
        f"""
        SELECT c.service_name AS service, c.meter_subcategory AS meter_subcategory, c.meter_name AS meter,
               c.unit AS unit, SUM(c.quantity) AS quantity, SUM(c.cost_base) AS cost,
               count(DISTINCT c.resource_id) AS resources
        FROM cost_records c WHERE {where}
        GROUP BY 1, 2, 3, 4 ORDER BY cost DESC
        """,
        params,
    )
    for r in rows:
        q = r["quantity"] or 0
        r["cost"] = round(r["cost"], 2)
        r["quantity"] = round(q, 4)
        r["cost_per_unit"] = round(r["cost"] / q, 4) if q else None
    return rows


def tag_coverage(db: Database, scope: Scope, required_tags: list[str], top_untagged: int = 50) -> dict:
    """Share of cost carrying each required tag, plus the largest untagged resources (FR-503/504)."""
    grand = total(db, scope)
    where, params = scope.where()
    tags = []
    for key in required_tags:
        tagged = grand - total(db, scope.replace(tags={**scope.tags, key: ""}))
        tags.append(
            {
                "tag": key,
                "tagged_cost": round(tagged, 2),
                "untagged_cost": round(grand - tagged, 2),
                "coverage_pct": round(100 * tagged / grand, 2) if grand else 100.0,
            }
        )
    violations = []
    if required_tags:
        missing = " OR ".join("COALESCE(json_extract_string(c.tags, ?), '') = ''" for _ in required_tags)
        mparams = [tag_path(k) for k in required_tags]
        rows = db.query(
            f"""
            SELECT c.resource_id AS resource, any_value(c.resource_name) AS name,
                   any_value(c.resource_group) AS resource_group, any_value(c.service_name) AS service,
                   SUM(c.cost_base) AS cost, any_value(CAST(c.tags AS VARCHAR)) AS tags
            FROM cost_records c WHERE {where} AND c.resource_id IS NOT NULL AND ({missing})
            GROUP BY 1 ORDER BY cost DESC LIMIT {int(top_untagged)}
            """,
            params + mparams,
        )
        for r in rows:
            present = json.loads(r.pop("tags") or "{}")
            r["missing_tags"] = [k for k in required_tags if not present.get(k)]
            r["cost"] = round(r["cost"], 2)
            violations.append(r)
    return {"total": round(grand, 2), "required_tags": tags, "violations": violations}


def resource_explorer(db: Database, scope: Scope, limit: int = 500) -> list[dict]:
    """Per-resource cost joined to inventory (FR-302)."""
    where, params = scope.where()
    rows = db.query(
        f"""
        WITH cost AS (
            SELECT c.resource_id, any_value(c.resource_name) AS cost_name, any_value(c.resource_type) AS cost_type,
                   any_value(c.resource_group) AS cost_rg, any_value(c.location) AS cost_location,
                   any_value(c.account_id) AS account_id, any_value(c.service_name) AS service,
                   SUM(c.cost_base) AS cost
            FROM cost_records c WHERE {where} AND c.resource_id IS NOT NULL GROUP BY 1
        )
        SELECT cost.resource_id AS resource, COALESCE(r.name, cost.cost_name) AS name,
               COALESCE(r.type, cost.cost_type) AS type, COALESCE(r.resource_group, cost.cost_rg) AS resource_group,
               COALESCE(r.location, cost.cost_location) AS location, r.sku, cost.account_id AS account,
               cost.service, cost.cost, CAST(r.tags AS VARCHAR) AS tags, r.removed_at IS NOT NULL AS removed
        FROM cost LEFT JOIN resources r ON r.tenant_id = ? AND r.resource_id = cost.resource_id
        ORDER BY cost.cost DESC LIMIT {int(limit)}
        """,
        params + [scope.tenant_id],
    )
    for r in rows:
        r["cost"] = round(r["cost"], 2)
    _decorate_accounts(db, scope.tenant_id, rows)
    return rows


def anomalies_by(db: Database, base: Scope, dim: str, as_of: date, days: int = 45, sensitivity: float = 3.5) -> list[dict]:
    """Anomalies per subscription or per service over the trailing window (FR-605)."""
    scope = base.replace(date_from=as_of - timedelta(days=days - 1), date_to=as_of)
    params: list = []
    expr = dimension_sql(dim, params)
    where, wparams = scope.where()
    rows = db.query(
        f"SELECT {expr} AS k, c.charge_date AS d, SUM(c.cost_base) AS cost FROM cost_records c "
        f"WHERE {where} GROUP BY 1, 2",
        params + wparams,
    )
    series: dict[str, dict[date, float]] = {}
    for r in rows:
        series.setdefault(r["k"], {})[r["d"]] = float(r["cost"])
    out = []
    for key, by_day in series.items():
        pts, d = [], scope.date_from
        while d <= scope.date_to:
            pts.append((d, by_day.get(d, 0.0)))
            d += timedelta(days=1)
        for a in detect_anomalies(pts, sensitivity=sensitivity):
            out.append({"dimension": dim, "value": key, **a})
    return sorted(out, key=lambda a: a["date"])
