"""Recommendation persistence, Advisor merge and lifecycle (FR-806, FR-807).

Lifecycle: open → accepted → implemented → verified, or → dismissed (reason
required). Re-running the engine refreshes the evidence of open and accepted
items, never reopens a dismissed one, and marks open items whose condition has
gone away as ``resolved``.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from ..db import Database, new_id
from .rules import MONTH_DAYS, RULES, Context, Rec

TRANSITIONS = {
    "open": {"accepted", "dismissed"},
    "accepted": {"implemented", "dismissed", "open"},
    "implemented": {"verified", "open"},
    "verified": set(),
    "dismissed": {"open"},
    "resolved": {"open"},
}
HORIZON_ORDER = {"immediate": 0, "short_term": 1, "ongoing": 2}


class LifecycleError(ValueError):
    pass


def _upsert(db: Database, tenant_id: str, rec: Rec) -> str:
    existing = db.one("SELECT id, status FROM recommendations WHERE tenant_id = ? AND dedupe_key = ?", [tenant_id, rec.dedupe_key])
    evidence = json.dumps(rec.evidence, default=str)
    if existing is None:
        db.execute(
            "INSERT INTO recommendations (id, tenant_id, account_id, dedupe_key, category, horizon, resource_id, resource_name, "
            "title, action, evidence, est_monthly_saving, confidence, effort, risk, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [new_id(), tenant_id, rec.account_id, rec.dedupe_key, rec.category, rec.horizon, rec.resource_id, rec.resource_name,
             rec.title, rec.action, evidence, rec.est_monthly_saving, rec.confidence, rec.effort, rec.risk, rec.source],
        )
        return "created"
    if existing["status"] in ("open", "accepted", "resolved"):
        db.execute(
            "UPDATE recommendations SET title = ?, action = ?, evidence = ?, est_monthly_saving = ?, confidence = ?, "
            "status = CASE WHEN status = 'resolved' THEN 'open' ELSE status END, status_reason = NULL, updated_at = now() "
            "WHERE id = ?",
            [rec.title, rec.action, evidence, rec.est_monthly_saving, rec.confidence, existing["id"]],
        )
        return "updated"
    return "unchanged"


def run(db: Database, tenant_id: str, as_of: date | None = None) -> dict:
    as_of = as_of or db.scalar("SELECT max(charge_date) FROM cost_records WHERE tenant_id = ?", [tenant_id]) or date.today()
    ctx = Context(db, tenant_id, as_of)
    recs: list[Rec] = []
    for rule in RULES:
        recs.extend(rule(ctx))
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    keys = set()
    for r in recs:
        counts[_upsert(db, tenant_id, r)] += 1
        keys.add(r.dedupe_key)
    stale = db.query(
        "SELECT id, dedupe_key FROM recommendations WHERE tenant_id = ? AND status = 'open' AND source = 'native'", [tenant_id]
    )
    resolved = 0
    for s in stale:
        if s["dedupe_key"] not in keys:
            db.execute(
                "UPDATE recommendations SET status = 'resolved', status_reason = 'condition no longer detected', updated_at = now() WHERE id = ?",
                [s["id"]],
            )
            resolved += 1
    verified = verify_realized_savings(db, tenant_id, as_of)
    return {**counts, "resolved": resolved, "verified": verified, "as_of": as_of.isoformat()}


def merge_advisor(db: Database, tenant_id: str, account_id: str, items: list[dict], fx_to_base: float = 1.0) -> dict:
    """Normalize Azure Advisor cost recommendations; fold them into matching native items (de-duplication)."""
    merged = inserted = 0
    for item in items:
        props = item.get("properties", item)
        rid = str(props.get("resourceMetadata", {}).get("resourceId") or props.get("impactedValue") or "").lower() or None
        ext = props.get("extendedProperties") or {}
        annual = float(ext.get("annualSavingsAmount") or ext.get("savingsAmount") or 0)
        monthly = annual / 12 * fx_to_base
        problem = (props.get("shortDescription") or {}).get("problem") or "Azure Advisor cost recommendation"
        solution = (props.get("shortDescription") or {}).get("solution") or problem
        native = db.one(
            "SELECT id, CAST(evidence AS VARCHAR) AS evidence FROM recommendations WHERE tenant_id = ? AND resource_id = ? "
            "AND source = 'native' AND status IN ('open', 'accepted')",
            [tenant_id, rid],
        ) if rid else None
        if native:
            ev = json.loads(native["evidence"])
            ev["azure_advisor"] = {"problem": problem, "solution": solution, "est_monthly_saving": round(monthly, 2)}
            db.execute("UPDATE recommendations SET evidence = ?, updated_at = now() WHERE id = ?", [json.dumps(ev), native["id"]])
            merged += 1
            continue
        rec = Rec(
            "advisor", "short_term", rid, (rid or "").rsplit("/", 1)[-1] or None, account_id,
            problem, solution, {"advisor": {k: v for k, v in ext.items() if isinstance(v, (str, int, float))}},
            round(monthly, 2), "medium", "medium", "medium", variant=str(props.get("recommendationTypeId", "")), source="advisor",
        )
        _upsert(db, tenant_id, rec)
        inserted += 1
    return {"merged_into_native": merged, "inserted": inserted}


def list_recs(db: Database, tenant_id: str, status: str | None = None) -> list[dict]:
    cond, params = "", [tenant_id]
    if status:
        cond, params = " AND status = ?", [tenant_id, status]
    rows = db.query(
        f"SELECT id, account_id, category, horizon, resource_id, resource_name, title, action, CAST(evidence AS VARCHAR) AS evidence, "
        f"est_monthly_saving, confidence, effort, risk, source, status, status_reason, implemented_at, realized_monthly_saving, "
        f"created_at, updated_at FROM recommendations WHERE tenant_id = ?{cond}",
        params,
    )
    for r in rows:
        r["evidence"] = json.loads(r["evidence"])
    rows.sort(key=lambda r: (HORIZON_ORDER.get(r["horizon"], 9), -r["est_monthly_saving"]))
    return rows


def transition(db: Database, tenant_id: str, rec_id: str, new_status: str, reason: str | None = None) -> dict:
    row = db.one("SELECT status FROM recommendations WHERE tenant_id = ? AND id = ?", [tenant_id, rec_id])
    if not row:
        raise LifecycleError("recommendation not found")
    if new_status not in TRANSITIONS.get(row["status"], set()):
        raise LifecycleError(f"cannot move from {row['status']} to {new_status}")
    if new_status == "dismissed" and not (reason and reason.strip()):
        raise LifecycleError("a reason is required to dismiss a recommendation")
    db.execute(
        "UPDATE recommendations SET status = ?, status_reason = ?, updated_at = now(), "
        "implemented_at = CASE WHEN ? = 'implemented' THEN now() ELSE implemented_at END WHERE tenant_id = ? AND id = ?",
        [new_status, reason, new_status, tenant_id, rec_id],
    )
    return {"id": rec_id, "status": new_status}


def verify_realized_savings(db: Database, tenant_id: str, as_of: date, settle_days: int = 14) -> int:
    """Compare the resource's daily cost 30 days before vs after implementation."""
    rows = db.query(
        "SELECT id, resource_id, implemented_at FROM recommendations WHERE tenant_id = ? AND status = 'implemented' "
        "AND resource_id IS NOT NULL AND implemented_at IS NOT NULL",
        [tenant_id],
    )
    verified = 0
    for r in rows:
        impl = r["implemented_at"].date()
        if (as_of - impl).days < settle_days:
            continue

        def avg(d0: date, d1: date, resource_id: str = r["resource_id"]) -> float:
            v = db.scalar(
                "SELECT COALESCE(SUM(cost_base), 0) FROM cost_records WHERE tenant_id = ? AND resource_id = ? "
                "AND charge_date BETWEEN ? AND ?",
                [tenant_id, resource_id, d0, d1],
            )
            return float(v) / max((d1 - d0).days + 1, 1)

        before = avg(impl - timedelta(days=30), impl - timedelta(days=1))
        after = avg(impl + timedelta(days=1), min(as_of, impl + timedelta(days=30)))
        realized = round((before - after) * MONTH_DAYS, 2)
        db.execute(
            "UPDATE recommendations SET realized_monthly_saving = ?, status = CASE WHEN ? > 0 THEN 'verified' ELSE status END, "
            "updated_at = now() WHERE id = ?",
            [realized, realized, r["id"]],
        )
        verified += realized > 0
    return verified


def savings_summary(db: Database, tenant_id: str) -> dict:
    rows = db.query(
        "SELECT status, count(*) AS n, SUM(est_monthly_saving) AS est, SUM(realized_monthly_saving) AS realized "
        "FROM recommendations WHERE tenant_id = ? GROUP BY 1",
        [tenant_id],
    )
    return {r["status"]: {"count": r["n"], "est_monthly_saving": round(r["est"] or 0, 2),
                          "realized_monthly_saving": round(r["realized"] or 0, 2)} for r in rows}

