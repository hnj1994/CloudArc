"""Rule-based cost centers with percentage splits (FR-502).

A cost center owns a list of rules; a cost record belongs to the center when
*any* rule matches, and a rule matches when *all* of its conditions hold:

    {"tags": {"Department": "IT"}, "resource_group_pattern": "app-prod-*",
     "accounts": ["<account id>"], "services": ["Virtual Machines"]}

``percent`` lets shared resources be split (e.g. a shared gateway 60/40
between two centers). Whatever is not allocated is reported as
"Unallocated", so the allocation always sums back to the scope total.
"""
from __future__ import annotations

import json

from ..db import Database, new_id
from .filters import FilterError, Scope, tag_path


def _rule_sql(rule: dict, params: list) -> str:
    conds = []
    for key, value in (rule.get("tags") or {}).items():
        conds.append("json_extract_string(c.tags, ?) = ?")
        params.extend([tag_path(key), str(value)])
    if rule.get("resource_group_pattern"):
        conds.append("c.resource_group LIKE ?")
        params.append(str(rule["resource_group_pattern"]).lower().replace("*", "%"))
    for field, col in (("accounts", "c.account_id"), ("services", "c.service_name"), ("locations", "c.location")):
        values = rule.get(field) or []
        if values:
            conds.append(f"{col} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
    if not conds:
        raise FilterError("a cost-center rule needs at least one condition")
    return "(" + " AND ".join(conds) + ")"


def validate_rules(rules: list[dict]) -> None:
    if not rules:
        raise FilterError("at least one rule is required")
    for r in rules:
        _rule_sql(r, [])


def center_condition(rules: list[dict], params: list) -> str:
    return "(" + " OR ".join(_rule_sql(r, params) for r in rules) + ")"


def list_centers(db: Database, tenant_id: str) -> list[dict]:
    rows = db.query(
        "SELECT id, name, CAST(rules AS VARCHAR) AS rules, percent, created_at FROM cost_centers WHERE tenant_id = ? ORDER BY name",
        [tenant_id],
    )
    for r in rows:
        r["rules"] = json.loads(r["rules"])
    return rows


def create_center(db: Database, tenant_id: str, name: str, rules: list[dict], percent: float = 100) -> str:
    validate_rules(rules)
    if not 0 < percent <= 100:
        raise FilterError("percent must be in (0, 100]")
    cid = new_id()
    db.execute(
        "INSERT INTO cost_centers (id, tenant_id, name, rules, percent) VALUES (?, ?, ?, ?, ?)",
        [cid, tenant_id, name, json.dumps(rules), percent],
    )
    return cid


def allocate(db: Database, scope: Scope) -> list[dict]:
    centers = list_centers(db, scope.tenant_id)
    where, wparams = scope.where()
    total = float(db.scalar(f"SELECT COALESCE(SUM(c.cost_base), 0) FROM cost_records c WHERE {where}", wparams))
    out, allocated = [], 0.0
    for cc in centers:
        params: list = []
        cond = center_condition(cc["rules"], params)
        matched = float(
            db.scalar(
                f"SELECT COALESCE(SUM(c.cost_base), 0) FROM cost_records c WHERE {where} AND {cond}",
                wparams + params,
            )
        )
        share = matched * cc["percent"] / 100
        allocated += share
        out.append({"cost_center": cc["name"], "id": cc["id"], "percent": cc["percent"], "matched_cost": round(matched, 2), "cost": round(share, 2)})
    out.sort(key=lambda r: r["cost"], reverse=True)
    out.append({"cost_center": "Unallocated", "id": None, "percent": None, "matched_cost": None, "cost": round(total - allocated, 2)})
    for r in out:
        r["share_pct"] = round(100 * r["cost"] / total, 2) if total else 0.0
    return out


def center_scope_condition(db: Database, tenant_id: str, center_id: str) -> tuple[str, list, float]:
    row = db.one(
        "SELECT CAST(rules AS VARCHAR) AS rules, percent FROM cost_centers WHERE tenant_id = ? AND id = ?",
        [tenant_id, center_id],
    )
    if not row:
        raise FilterError("unknown cost center")
    params: list = []
    return center_condition(json.loads(row["rules"]), params), params, row["percent"] / 100
