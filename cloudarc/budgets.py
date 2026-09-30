"""Budgets, calibration and evaluation (FR-601 … FR-604).

Calibration (FR-603) is the direct fix for the failure seen in the CloudSpend
evaluation, where the budget sat far below real spend and every alert was
noise. A budget below the trailing 3-closed-month average (scaled to its
period) is flagged when it is created or edited, and again on each evaluation.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import alerts
from .analytics.allocation import center_scope_condition
from .analytics.filters import FilterError, Scope
from .analytics.forecast import Z80
from .db import Database, new_id
from .tenants import NotFound
from .timeutil import add_months, fmt_money, month_start, period_bounds, period_months

SCOPE_TYPES = {"tenant", "account", "resource_group", "service", "tag", "cost_center"}
PERIODS = {"monthly", "quarterly", "annual"}


class BudgetError(ValueError):
    pass


def _scope_sql(db: Database, tenant_id: str, scope_type: str, scope_value: str | None, d0: date, d1: date):
    """(where, params, multiplier) for a budget scope and date range."""
    s = Scope(tenant_id=tenant_id, date_from=d0, date_to=d1)
    mult = 1.0
    extra, extra_params = "", []
    if scope_type == "account":
        s.accounts = [scope_value]
    elif scope_type == "resource_group":
        s.resource_groups = [scope_value]
    elif scope_type == "service":
        s.services = [scope_value]
    elif scope_type == "tag":
        key, sep, value = (scope_value or "").partition("=")
        if not sep or not key:
            raise BudgetError("tag scope must look like Key=Value")
        s.tags = {key.strip(): value.strip()}
    elif scope_type == "cost_center":
        cond, extra_params, mult = center_scope_condition(db, tenant_id, scope_value)
        extra = f" AND {cond}"
    elif scope_type != "tenant":
        raise BudgetError(f"scope_type must be one of {sorted(SCOPE_TYPES)}")
    where, params = s.where()
    return where + extra, params + extra_params, mult


def scope_total(db: Database, tenant_id: str, scope_type: str, scope_value: str | None, d0: date, d1: date) -> float:
    where, params, mult = _scope_sql(db, tenant_id, scope_type, scope_value, d0, d1)
    return mult * float(db.scalar(f"SELECT COALESCE(SUM(c.cost_base), 0) FROM cost_records c WHERE {where}", params))


def scope_daily(db: Database, tenant_id: str, scope_type: str, scope_value: str | None, d0: date, d1: date) -> list[float]:
    where, params, mult = _scope_sql(db, tenant_id, scope_type, scope_value, d0, d1)
    rows = db.query(
        f"SELECT c.charge_date AS d, SUM(c.cost_base) AS v FROM cost_records c WHERE {where} GROUP BY 1", params
    )
    by_day = {r["d"]: float(r["v"]) * mult for r in rows}
    out, d = [], d0
    while d <= d1:
        out.append(by_day.get(d, 0.0))
        d += timedelta(days=1)
    return out


def trailing_monthly_average(db: Database, tenant_id: str, scope_type: str, scope_value: str | None, as_of: date, months: int = 3) -> float | None:
    """Average of the last ``months`` *closed* months that have data."""
    this_month = month_start(as_of)
    totals = []
    for i in range(1, months + 1):
        m0 = add_months(this_month, -i)
        m1 = add_months(m0, 1) - timedelta(days=1)
        has_data = db.scalar(
            "SELECT count(*) FROM cost_records WHERE tenant_id = ? AND charge_date BETWEEN ? AND ?", [tenant_id, m0, m1]
        )
        if has_data:
            totals.append(scope_total(db, tenant_id, scope_type, scope_value, m0, m1))
    return sum(totals) / len(totals) if totals else None


def calibration(db: Database, tenant_id: str, scope_type: str, scope_value: str | None, period: str, amount: float, as_of: date) -> dict:
    avg = trailing_monthly_average(db, tenant_id, scope_type, scope_value, as_of)
    if avg is None:
        return {"status": "insufficient_history", "message": "No closed month of data yet; calibration will run once history exists."}
    expected = avg * period_months(period)
    if amount < expected:
        gap = 100 * (expected - amount) / expected
        return {
            "status": "below_trailing_spend",
            "trailing_3m_monthly_avg": round(avg, 2),
            "expected_period_spend": round(expected, 2),
            "gap_pct": round(gap, 1),
            "message": (
                f"Budget {fmt_money(amount)} is {gap:.1f}% below trailing 3-month actual spend "
                f"({fmt_money(expected)} per {period.removesuffix('ly') if period != 'annual' else 'year'}). "
                "Alerts will fire every period and lose meaning — recalibrate or confirm this is a savings target."
            ),
        }
    return {"status": "ok", "trailing_3m_monthly_avg": round(avg, 2), "expected_period_spend": round(expected, 2)}


def _validate(scope_type: str, period: str, amount: float, thresholds: list[float]) -> list[float]:
    if scope_type not in SCOPE_TYPES:
        raise BudgetError(f"scope_type must be one of {sorted(SCOPE_TYPES)}")
    if period not in PERIODS:
        raise BudgetError(f"period must be one of {sorted(PERIODS)}")
    if amount <= 0:
        raise BudgetError("amount must be positive")
    ts = sorted({float(t) for t in thresholds})
    if not ts or any(t <= 0 or t > 500 for t in ts):
        raise BudgetError("thresholds must be percentages between 0 and 500")
    return ts


def create_budget(db: Database, tenant_id: str, *, name: str, scope_type: str, scope_value: str | None, period: str,
                  amount: float, thresholds: list[float], forecast_alert: bool = True, created_by: str | None = None,
                  as_of: date | None = None) -> dict:
    ts = _validate(scope_type, period, amount, thresholds)
    check_scope(db, tenant_id, scope_type, scope_value)
    bid = new_id()
    db.execute(
        "INSERT INTO budgets (id, tenant_id, name, scope_type, scope_value, period, amount, thresholds, forecast_alert, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [bid, tenant_id, name, scope_type, scope_value, period, amount, json.dumps(ts), forecast_alert, created_by],
    )
    as_of = as_of or _as_of(db, tenant_id)
    return {"id": bid, "calibration": calibration(db, tenant_id, scope_type, scope_value, period, amount, as_of)}


def update_budget(db: Database, tenant_id: str, budget_id: str, **changes) -> dict:
    b = get_budget(db, tenant_id, budget_id)
    merged = {**b, **{k: v for k, v in changes.items() if v is not None}}
    ts = _validate(merged["scope_type"], merged["period"], merged["amount"], merged["thresholds"])
    db.execute(
        "UPDATE budgets SET name = ?, scope_type = ?, scope_value = ?, period = ?, amount = ?, thresholds = ?, "
        "forecast_alert = ?, updated_at = now() WHERE tenant_id = ? AND id = ?",
        [merged["name"], merged["scope_type"], merged["scope_value"], merged["period"], merged["amount"],
         json.dumps(ts), merged["forecast_alert"], tenant_id, budget_id],
    )
    return {"id": budget_id, "calibration": calibration(db, tenant_id, merged["scope_type"], merged["scope_value"],
                                                        merged["period"], merged["amount"], _as_of(db, tenant_id))}


def delete_budget(db: Database, tenant_id: str, budget_id: str) -> None:
    get_budget(db, tenant_id, budget_id)
    db.execute("DELETE FROM budgets WHERE tenant_id = ? AND id = ?", [tenant_id, budget_id])


def get_budget(db: Database, tenant_id: str, budget_id: str) -> dict:
    row = db.one(
        "SELECT id, name, scope_type, scope_value, period, amount, CAST(thresholds AS VARCHAR) AS thresholds, "
        "forecast_alert, created_by, created_at, updated_at FROM budgets WHERE tenant_id = ? AND id = ?",
        [tenant_id, budget_id],
    )
    if not row:
        raise NotFound("budget not found")
    row["thresholds"] = json.loads(row["thresholds"])
    return row


def list_budgets(db: Database, tenant_id: str) -> list[dict]:
    ids = [r["id"] for r in db.query("SELECT id FROM budgets WHERE tenant_id = ? ORDER BY name", [tenant_id])]
    return [get_budget(db, tenant_id, i) for i in ids]


def _as_of(db: Database, tenant_id: str) -> date:
    return db.scalar("SELECT max(charge_date) FROM cost_records WHERE tenant_id = ?", [tenant_id]) or date.today()


def status(db: Database, tenant_id: str, budget: dict, as_of: date | None = None, fiscal_year_start: int = 4) -> dict:
    as_of = as_of or _as_of(db, tenant_id)
    p0, p1 = period_bounds(budget["period"], as_of, fiscal_year_start)
    to_date = min(as_of, p1)
    daily = scope_daily(db, tenant_id, budget["scope_type"], budget["scope_value"], p0, to_date)
    actual = sum(daily)
    remaining = (p1 - to_date).days
    recent = daily[-7:] if daily else []
    rate = (0.6 * sum(recent) / len(recent) + 0.4 * actual / len(daily)) if daily else 0.0
    sd = (sum((v - actual / len(daily)) ** 2 for v in daily) / len(daily)) ** 0.5 if len(daily) > 1 else 0.0
    forecast = actual + rate * remaining
    amount = budget["amount"]
    return {
        **budget,
        "period_start": p0.isoformat(),
        "period_end": p1.isoformat(),
        "as_of": to_date.isoformat(),
        "is_partial": to_date < p1,
        "actual": round(actual, 2),
        "actual_pct": round(100 * actual / amount, 2),
        "forecast": round(forecast, 2),
        "forecast_low": round(actual + max(rate - Z80 * sd, 0) * remaining, 2),
        "forecast_high": round(actual + (rate + Z80 * sd) * remaining, 2),
        "forecast_pct": round(100 * forecast / amount, 2),
        "thresholds_crossed": [t for t in budget["thresholds"] if 100 * actual / amount >= t],
        "calibration": calibration(db, tenant_id, budget["scope_type"], budget["scope_value"], budget["period"], amount, as_of),
    }


def evaluate(db: Database, tenant_id: str, as_of: date | None = None, notify: bool = True) -> list[dict]:
    """Run after every sync. Alerts are de-duplicated per budget, period and threshold."""
    tenant = db.one("SELECT name, fiscal_year_start FROM tenants WHERE id = ?", [tenant_id]) or {"name": tenant_id, "fiscal_year_start": 4}
    raised = []
    for b in list_budgets(db, tenant_id):
        st = status(db, tenant_id, b, as_of, tenant["fiscal_year_start"])
        pkey = f"{b['id']}:{st['period_start']}"
        for t in st["thresholds_crossed"]:
            a = alerts.raise_alert(
                db, tenant_id, kind="budget_actual", severity="critical" if t >= 100 else "warning",
                message=f"{tenant['name']} · budget '{b['name']}' reached {st['actual_pct']:.1f}% "
                        f"({fmt_money(st['actual'])} of {fmt_money(b['amount'])}, threshold {t:g}%)",
                details=st, budget_id=b["id"], dedupe_key=f"budget:{pkey}:actual:{t:g}",
            )
            if a:
                raised.append(a)
        if b["forecast_alert"] and st["is_partial"] and st["forecast"] > b["amount"] and st["actual"] < b["amount"]:
            a = alerts.raise_alert(
                db, tenant_id, kind="budget_forecast", severity="warning",
                message=f"{tenant['name']} · budget '{b['name']}' is forecast to reach {fmt_money(st['forecast'])} "
                        f"({st['forecast_pct']:.1f}%) by {st['period_end']}",
                details=st, budget_id=b["id"], dedupe_key=f"budget:{pkey}:forecast",
            )
            if a:
                raised.append(a)
        cal = st["calibration"]
        if cal["status"] == "below_trailing_spend":
            a = alerts.raise_alert(
                db, tenant_id, kind="budget_calibration", severity="info",
                message=f"{tenant['name']} · budget '{b['name']}': {cal['message']}",
                details=cal, budget_id=b["id"], dedupe_key=f"budget:{pkey}:calibration:{b['amount']}",
            )
            if a:
                raised.append(a)
    if notify and raised:
        alerts.dispatch(db, tenant_id, raised)
    return raised


def check_scope(db: Database, tenant_id: str, scope_type: str, scope_value: str | None) -> None:
    try:
        _scope_sql(db, tenant_id, scope_type, scope_value, date.today(), date.today())
    except FilterError as e:
        raise BudgetError(str(e)) from e
