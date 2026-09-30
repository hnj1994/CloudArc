"""Resource inventory, change tracking and utilization metrics (FR-301 … FR-304)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from .db import Database

# Resource types that never bill on their own; excluded from "no cost" idle checks.
FREE_TYPES = {
    "microsoft.network/networksecuritygroups",
    "microsoft.network/virtualnetworks",
    "microsoft.network/networkinterfaces",
    "microsoft.network/networkwatchers",
    "microsoft.network/routetables",
    "microsoft.resources/resourcegroups",
    "microsoft.resources/subscriptions/resourcegroups",
    "microsoft.insights/actiongroups",
    "microsoft.insights/metricalerts",
    "microsoft.sql/servers",
    "microsoft.compute/virtualmachines/extensions",
}


def _norm(r: dict) -> dict:
    sku = r.get("sku")
    props = r.get("properties") or {}
    if isinstance(sku, dict):
        sku = sku.get("name")
    if not sku and isinstance(props, dict):
        sku = (props.get("hardwareProfile") or {}).get("vmSize")
    return {
        "resource_id": str(r["id"]).lower(),
        "name": r.get("name"),
        "type": (r.get("type") or "").lower(),
        "resource_group": (r.get("resourceGroup") or r.get("resource_group") or "").lower() or None,
        "location": (r.get("location") or "").lower().replace(" ", "") or None,
        "sku": sku,
        "tags": json.dumps(r.get("tags") or {}),
        "properties": json.dumps({**props, **({"managedBy": r["managedBy"]} if r.get("managedBy") else {})}),
        "created_time": props.get("timeCreated") if isinstance(props, dict) else None,
    }


def upsert_resources(db: Database, tenant_id: str, account_id: str, items: list[dict], full_snapshot: bool = True) -> dict:
    """Merge a Resource Graph snapshot; records added / removed / resized changes."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    existing = {
        r["resource_id"]: r
        for r in db.query(
            "SELECT resource_id, sku, removed_at FROM resources WHERE tenant_id = ? AND account_id = ?",
            [tenant_id, account_id],
        )
    }
    seen, added, resized = set(), 0, 0
    with db.transaction() as cur:
        for raw in items:
            r = _norm(raw)
            rid = r["resource_id"]
            seen.add(rid)
            prev = existing.get(rid)
            if prev is None or prev["removed_at"] is not None:
                added += 1
                cur.execute("INSERT INTO resource_changes (tenant_id, resource_id, change_type, detail) VALUES (?, ?, 'added', ?)",
                            [tenant_id, rid, r["type"]])
            elif prev["sku"] and r["sku"] and prev["sku"] != r["sku"]:
                resized += 1
                cur.execute("INSERT INTO resource_changes (tenant_id, resource_id, change_type, detail) VALUES (?, ?, 'resized', ?)",
                            [tenant_id, rid, f"{prev['sku']} -> {r['sku']}"])
            cur.execute(
                """
                INSERT INTO resources (tenant_id, account_id, resource_id, name, type, resource_group, location, sku, tags,
                                       properties, created_time, first_seen, last_seen, removed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRY_CAST(? AS TIMESTAMP), ?, ?, NULL)
                ON CONFLICT (tenant_id, resource_id) DO UPDATE SET
                    name = excluded.name, type = excluded.type, resource_group = excluded.resource_group,
                    location = excluded.location, sku = excluded.sku, tags = excluded.tags,
                    properties = excluded.properties, last_seen = excluded.last_seen, removed_at = NULL
                """,
                [tenant_id, account_id, rid, r["name"], r["type"], r["resource_group"], r["location"], r["sku"],
                 r["tags"], r["properties"], r["created_time"], now, now],
            )
        removed = 0
        if full_snapshot:
            for rid, prev in existing.items():
                if rid not in seen and prev["removed_at"] is None:
                    removed += 1
                    cur.execute("UPDATE resources SET removed_at = ? WHERE tenant_id = ? AND resource_id = ?", [now, tenant_id, rid])
                    cur.execute("INSERT INTO resource_changes (tenant_id, resource_id, change_type) VALUES (?, ?, 'removed')",
                                [tenant_id, rid])
    return {"resources": len(seen), "added": added, "removed": removed, "resized": resized}


def upsert_metrics(db: Database, tenant_id: str, rows: list[dict]) -> int:
    """rows: {resource_id, metric, day, avg, max, min}"""
    with db.transaction() as cur:
        for m in rows:
            cur.execute(
                "INSERT INTO resource_metrics VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, resource_id, metric, day) "
                "DO UPDATE SET avg = excluded.avg, max = excluded.max, min = excluded.min",
                [tenant_id, str(m["resource_id"]).lower(), m["metric"], m["day"], m.get("avg"), m.get("max"), m.get("min")],
            )
    return len(rows)


def list_resources(db: Database, tenant_id: str, include_removed: bool = False) -> list[dict]:
    cond = "" if include_removed else " AND removed_at IS NULL"
    rows = db.query(
        f"SELECT account_id AS account, resource_id, name, type, resource_group, location, sku, CAST(tags AS VARCHAR) AS tags, "
        f"created_time, first_seen, last_seen, removed_at FROM resources WHERE tenant_id = ?{cond} ORDER BY type, name",
        [tenant_id],
    )
    for r in rows:
        r["tags"] = json.loads(r["tags"] or "{}")
    return rows


def changes(db: Database, tenant_id: str, since: date | None = None) -> list[dict]:
    since = since or (date.today() - timedelta(days=90))
    return db.query(
        "SELECT ch.resource_id, r.name, r.type, ch.change_type, ch.detail, ch.changed_at FROM resource_changes ch "
        "LEFT JOIN resources r ON r.tenant_id = ch.tenant_id AND r.resource_id = ch.resource_id "
        "WHERE ch.tenant_id = ? AND ch.changed_at >= ? ORDER BY ch.changed_at DESC",
        [tenant_id, since],
    )


def insights(db: Database, tenant_id: str, as_of: date, idle_days: int = 14) -> dict:
    """Billable resources with no cost for ``idle_days`` and cost-bearing resources with no tags (FR-303)."""
    since = as_of - timedelta(days=idle_days - 1)
    no_cost = db.query(
        f"""
        SELECT r.resource_id, r.name, r.type, r.resource_group, r.location FROM resources r
        WHERE r.tenant_id = ? AND r.removed_at IS NULL
          AND r.type NOT IN ({', '.join('?' for _ in FREE_TYPES)})
          AND NOT EXISTS (SELECT 1 FROM cost_records c WHERE c.tenant_id = r.tenant_id AND c.resource_id = r.resource_id
                          AND c.charge_date BETWEEN ? AND ? AND c.cost_base > 0)
        ORDER BY r.type, r.name
        """,
        [tenant_id, *sorted(FREE_TYPES), since, as_of],
    )
    untagged = db.query(
        """
        SELECT c.resource_id, any_value(c.resource_name) AS name, any_value(c.resource_type) AS type,
               SUM(c.cost_base) AS cost
        FROM cost_records c WHERE c.tenant_id = ? AND c.charge_date BETWEEN ? AND ? AND c.resource_id IS NOT NULL
          AND (c.tags IS NULL OR CAST(c.tags AS VARCHAR) IN ('{}', 'null'))
        GROUP BY 1 ORDER BY cost DESC
        """,
        [tenant_id, since, as_of],
    )
    for r in untagged:
        r["cost"] = round(r["cost"], 2)
    return {"window_days": idle_days, "no_cost_resources": no_cost, "untagged_cost_resources": untagged}


def metric_series(db: Database, tenant_id: str, resource_id: str, metric: str, d0: date, d1: date) -> list[dict]:
    return db.query(
        "SELECT day, avg, max, min FROM resource_metrics WHERE tenant_id = ? AND resource_id = ? AND metric = ? "
        "AND day BETWEEN ? AND ? ORDER BY day",
        [tenant_id, resource_id.lower(), metric, d0, d1],
    )
