"""Audit log of logins, onboarding, credential, budget and report actions (FR-1004)."""
from __future__ import annotations

import json

from .db import Database, new_id


def record(db: Database, action: str, *, user_id: str | None = None, tenant_id: str | None = None,
           target: str | None = None, detail: dict | None = None, ip: str | None = None) -> None:
    db.execute(
        "INSERT INTO audit_log (id, user_id, tenant_id, action, target, detail, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [new_id(), user_id, tenant_id, action, target, json.dumps(detail or {}, default=str), ip],
    )


def query(db: Database, tenant_id: str | None = None, limit: int = 500) -> list[dict]:
    cond, params = ("WHERE a.tenant_id = ?", [tenant_id]) if tenant_id else ("", [])
    rows = db.query(
        f"SELECT a.ts, a.action, a.target, CAST(a.detail AS VARCHAR) AS detail, a.ip, a.tenant_id, u.email AS user "
        f"FROM audit_log a LEFT JOIN users u ON u.id = a.user_id {cond} ORDER BY a.ts DESC LIMIT {int(limit)}",
        params,
    )
    for r in rows:
        r["detail"] = json.loads(r["detail"] or "{}")
    return rows
