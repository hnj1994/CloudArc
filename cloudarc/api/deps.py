"""Authentication and tenant-access dependencies shared by every route."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from fastapi import Depends, HTTPException, Query, Request

from ..analytics.filters import Scope
from ..config import get_settings
from ..db import Database, get_db
from ..security.auth import AuthError, Principal, looks_like_jwt, principal_from_api_token, principal_from_entra_jwt


def db_dep() -> Database:
    return get_db()


def current_principal(request: Request, db: Database = Depends(db_dep)) -> Principal:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    token = header[7:].strip()
    try:
        if looks_like_jwt(token) and get_settings().auth_mode == "entra":
            return principal_from_entra_jwt(db, token)
        return principal_from_api_token(db, token)
    except AuthError as exc:
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc


def platform_admin(p: Principal = Depends(current_principal)) -> Principal:
    if not p.is_platform_admin:
        raise HTTPException(403, "platform admin role required")
    return p


@dataclass
class TenantCtx:
    tenant_id: str
    principal: Principal
    role: str
    ip: str | None


def tenant_access(min_role: str = "viewer"):
    """Dependency: the caller must be assigned to ``tenant_id`` with at least ``min_role``.

    Unassigned tenants answer 404 exactly like non-existent ones, so tenant IDs
    cannot be probed (FR-1001, NFR-02).
    """

    def dep(tenant_id: str, request: Request, p: Principal = Depends(current_principal), db: Database = Depends(db_dep)) -> TenantCtx:
        role = p.role_in(tenant_id)
        if role is None or not db.scalar("SELECT count(*) FROM tenants WHERE id = ?", [tenant_id]):
            raise HTTPException(404, "tenant not found")
        if not p.can(tenant_id, min_role):
            raise HTTPException(403, f"{min_role} role required for this tenant")
        return TenantCtx(tenant_id, p, role, request.client.host if request.client else None)

    return dep


def scope_dep(
    tenant_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    account: list[str] = Query(default=[]),
    resource_group: list[str] = Query(default=[]),
    service: list[str] = Query(default=[]),
    location: list[str] = Query(default=[]),
    resource: list[str] = Query(default=[]),
    meter: list[str] = Query(default=[]),
    provider: list[str] = Query(default=[]),
    tag: list[str] = Query(default=[], description="Key=Value; Key= matches resources missing the tag"),
) -> Scope:
    tags = {}
    for t in tag:
        k, sep, v = t.partition("=")
        if not sep or not k:
            raise HTTPException(400, "tag filters look like Key=Value")
        tags[k] = v
    return Scope(tenant_id, date_from, date_to, account, resource_group, service, location, resource, meter, provider, tags)
