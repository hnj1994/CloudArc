"""Platform, identity, onboarding and account-management routes."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import __version__, audit, sync, tenants
from ..config import get_settings
from ..connectors.aws import AwsError
from ..connectors.azure import AzureClient, AzureError
from ..connectors.gcp import GcpError, billing_account_from_table, parse_key, parse_table
from ..db import Database
from ..ingest.loader import set_fx_rate
from ..security import auth
from ..security.auth import Principal
from .deps import TenantCtx, current_principal, db_dep, platform_admin, tenant_access

router = APIRouter(prefix="/api")
ADMIN, ANALYST, VIEW = tenant_access("tenant_admin"), tenant_access("analyst"), tenant_access("viewer")


def azure_factory(request: Request) -> Callable[[str, str, str], AzureClient]:
    return getattr(request.app.state, "azure_factory", None) or (lambda d, c, s: AzureClient(d, c, s))


def aws_factory(request: Request):
    return getattr(request.app.state, "aws_factory", None) or sync.aws_client


def gcp_factory(request: Request):
    return getattr(request.app.state, "gcp_factory", None) or sync.gcp_client


# ---- platform ------------------------------------------------------------------------------------------

@router.get("/health")
def health(db: Database = Depends(db_dep)):
    """Unauthenticated liveness/readiness for external monitoring (FR-1103)."""
    db.scalar("SELECT 1")
    counts = {r["s"] or "never": r["n"] for r in db.query(
        "SELECT last_sync_status AS s, count(*) AS n FROM cloud_accounts WHERE enabled GROUP BY 1")}
    failed_jobs = db.scalar("SELECT count(*) FROM sync_jobs WHERE status = 'failed' AND created_at > now() - INTERVAL 1 DAY")
    return {"status": "ok", "version": __version__, "accounts_by_sync_status": counts, "failed_sync_jobs_24h": failed_jobs}


@router.get("/config")
def public_config():
    s = get_settings()
    return {"auth_mode": s.auth_mode, "entra_tenant_id": s.entra_tenant_id, "entra_client_id": s.entra_client_id,
            "base_currency": s.base_currency, "version": __version__}


@router.get("/me")
def me(p: Principal = Depends(current_principal), db: Database = Depends(db_dep)):
    ts = tenants.list_tenants(db, None if p.is_platform_admin else list(p.tenants))
    for t in ts:
        t["role"] = p.role_in(t["id"])
    return {"id": p.user_id, "email": p.email, "name": p.name, "is_platform_admin": p.is_platform_admin, "tenants": ts}


@router.post("/session")
def session_start(request: Request, p: Principal = Depends(current_principal), db: Database = Depends(db_dep)):
    audit.record(db, "login", user_id=p.user_id, ip=request.client.host if request.client else None,
                 detail={"user_agent": request.headers.get("user-agent", "")[:200]})
    return {"ok": True}


@router.get("/audit")
def platform_audit(p: Principal = Depends(platform_admin), db: Database = Depends(db_dep)):
    return audit.query(db, None, 1000)


class FxIn(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    rate_date: date
    rate_to_base: float = Field(gt=0)


@router.post("/fx-rates", status_code=201)
def fx_rate(body: FxIn, p: Principal = Depends(platform_admin), db: Database = Depends(db_dep)):
    set_fx_rate(db, body.currency, body.rate_date.isoformat(), body.rate_to_base)
    audit.record(db, "fx.set", user_id=p.user_id, detail=body.model_dump())
    return {"ok": True}


# ---- tenants & users (platform admin) -------------------------------------------------------------

class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    currency: str = "INR"
    fiscal_year_start: int = Field(4, ge=1, le=12)
    required_tags: list[str] | None = None


class TenantPatch(BaseModel):
    name: str | None = None
    currency: str | None = None
    fiscal_year_start: int | None = Field(None, ge=1, le=12)
    required_tags: list[str] | None = None


@router.post("/tenants", status_code=201)
def tenant_create(body: TenantIn, p: Principal = Depends(platform_admin), db: Database = Depends(db_dep)):
    tid = tenants.create_tenant(db, body.name, body.currency, body.fiscal_year_start, body.required_tags)
    audit.record(db, "tenant.create", user_id=p.user_id, tenant_id=tid, detail=body.model_dump())
    return {"id": tid}


@router.patch("/tenants/{tenant_id}")
def tenant_update(body: TenantPatch, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    t = tenants.update_tenant(db, ctx.tenant_id, **body.model_dump())
    audit.record(db, "tenant.update", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, detail=body.model_dump(exclude_none=True), ip=ctx.ip)
    return t


class UserIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    name: str | None = None
    is_platform_admin: bool = False


class RoleIn(BaseModel):
    role: str


@router.get("/users")
def user_list(p: Principal = Depends(platform_admin), db: Database = Depends(db_dep)):
    users = db.query("SELECT id, email, name, is_platform_admin, created_at FROM users ORDER BY email")
    roles = db.query("SELECT user_id, tenant_id, role FROM user_tenants")
    for u in users:
        u["tenants"] = {r["tenant_id"]: r["role"] for r in roles if r["user_id"] == u["id"]}
    return users


@router.post("/users", status_code=201)
def user_create(body: UserIn, p: Principal = Depends(platform_admin), db: Database = Depends(db_dep)):
    uid = auth.create_user(db, body.email, body.name, body.is_platform_admin)
    audit.record(db, "user.create", user_id=p.user_id, target=uid, detail=body.model_dump())
    return {"id": uid}


@router.put("/users/{user_id}/tenants/{tenant_id}")
def user_assign(user_id: str, tenant_id: str, body: RoleIn, p: Principal = Depends(platform_admin), db: Database = Depends(db_dep)):
    tenants.get_tenant(db, tenant_id)
    auth.assign_role(db, user_id, tenant_id, body.role)
    audit.record(db, "user.assign_role", user_id=p.user_id, tenant_id=tenant_id, target=user_id, detail=body.model_dump())
    return {"ok": True}


@router.delete("/users/{user_id}/tenants/{tenant_id}", status_code=204)
def user_revoke(user_id: str, tenant_id: str, p: Principal = Depends(platform_admin), db: Database = Depends(db_dep)):
    auth.revoke_role(db, user_id, tenant_id)
    audit.record(db, "user.revoke_role", user_id=p.user_id, tenant_id=tenant_id, target=user_id)


@router.post("/users/{user_id}/tokens", status_code=201)
def user_token(user_id: str, p: Principal = Depends(platform_admin), db: Database = Depends(db_dep)):
    if not db.scalar("SELECT count(*) FROM users WHERE id = ?", [user_id]):
        raise HTTPException(404, "user not found")
    token = auth.issue_token(db, user_id, "api")
    audit.record(db, "token.issue", user_id=p.user_id, target=user_id)
    return {"token": token, "note": "shown once; store it securely"}


# ---- onboarding (FR-101 … FR-106) -------------------------------------------------------------------

class AzureCredentialIn(BaseModel):
    directory_id: str = Field(min_length=8, description="Entra tenant (directory) ID")
    client_id: str = Field(min_length=8, description="App registration (client) ID")
    secret: str = Field(min_length=8, description="Client secret; stored encrypted, never returned")


class Selection(BaseModel):
    subscription_id: str
    name: str | None = None


class OnboardIn(AzureCredentialIn):
    subscriptions: list[Selection] = Field(min_length=1)


REQUIRED_ROLES = [
    {"role": "Reader", "scope": "Subscription", "why": "Resource inventory (Resource Graph) and Azure Monitor metrics"},
    {"role": "Cost Management Reader", "scope": "Subscription", "why": "Cost details / usage data"},
    {"role": "Billing Reader", "scope": "Subscription", "why": "Billing periods, reservations and invoices"},
]


@router.get("/onboarding/requirements")
def onboarding_requirements(p: Principal = Depends(current_principal)):
    return {
        "roles": REQUIRED_ROLES,
        "steps": [
            "Entra ID → App registrations → New registration (single tenant). Record the Application (client) ID and Directory (tenant) ID.",
            "Certificates & secrets → New client secret. Copy the value (shown once).",
            "For each subscription: Access control (IAM) → Add role assignment → assign the three roles above to the app.",
            "Do NOT grant Contributor/Owner or any write role — CloudArc flags over-privileged credentials.",
        ],
    }


@router.post("/tenants/{tenant_id}/onboarding/validate")
def onboarding_validate(body: AzureCredentialIn, request: Request, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    client = azure_factory(request)(body.directory_id, body.client_id, body.secret)
    try:
        subs = client.list_subscriptions()
        for s in subs:
            s["permissions"] = client.check_permissions(s["subscription_id"])
    except AzureError as exc:
        audit.record(db, "onboarding.validate_failed", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id,
                     detail={"client_id": body.client_id, "error": str(exc)[:300]}, ip=ctx.ip)
        raise HTTPException(400, f"validation failed: {exc}") from exc
    audit.record(db, "onboarding.validate", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id,
                 detail={"client_id": body.client_id, "subscriptions": len(subs)}, ip=ctx.ip)
    return {"subscriptions": subs, "required_roles": REQUIRED_ROLES}


@router.post("/tenants/{tenant_id}/onboarding/complete", status_code=201)
def onboarding_complete(body: OnboardIn, request: Request, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    client = azure_factory(request)(body.directory_id, body.client_id, body.secret)
    try:
        visible = {s["subscription_id"].lower(): s for s in client.list_subscriptions()}
    except AzureError as exc:
        raise HTTPException(400, f"credential check failed: {exc}") from exc
    missing = [s.subscription_id for s in body.subscriptions if s.subscription_id.lower() not in visible]
    if missing:
        raise HTTPException(400, f"credential cannot see subscriptions: {missing}")
    cred_id = tenants.store_credential(db, ctx.tenant_id, "azure", body.directory_id, body.client_id, body.secret)
    accounts = []
    for sel in body.subscriptions:
        perm = client.check_permissions(sel.subscription_id)
        aid = tenants.upsert_account(db, ctx.tenant_id, "azure", sel.subscription_id,
                                     sel.name or visible[sel.subscription_id.lower()].get("name"), cred_id)
        db.execute("UPDATE cloud_accounts SET permission_status = ?, permission_detail = ? WHERE id = ?",
                   [perm["status"], json.dumps(perm), aid])
        job = sync.enqueue(db, ctx.tenant_id, aid, ctx.principal.email)
        accounts.append({"account_id": aid, "subscription_id": sel.subscription_id, "permissions": perm, "sync_job": job})
    audit.record(db, "onboarding.complete", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=cred_id,
                 detail={"client_id": body.client_id, "subscriptions": [s.subscription_id for s in body.subscriptions]}, ip=ctx.ip)
    return {"credential_id": cred_id, "accounts": accounts}


# ---- AWS & GCP connections ---------------------------------------------------------------------------

class AwsConnectIn(BaseModel):
    access_key_id: str = Field(pattern=r"^AKIA[A-Z0-9]{16}$", description="Access key of the read-only IAM user")
    secret_access_key: str = Field(min_length=16, description="Stored encrypted, never returned")
    role_arn: str | None = Field(None, description="Optional role to assume (e.g. in the payer account)")
    external_id: str | None = Field(None, max_length=1224)
    cur_bucket: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    cur_prefix: str | None = Field(None, max_length=512)
    name: str | None = None


class GcpConnectIn(BaseModel):
    service_account_key: str = Field(min_length=100, description="JSON key file contents; stored encrypted, never returned")
    table: str = Field(description="project.dataset.gcp_billing_export_..._v1_XXXXXX_XXXXXX_XXXXXX")
    job_project: str | None = Field(None, pattern=r"^[a-z][a-z0-9-]{4,29}$", description="Project queries run in (default: the table's)")
    name: str | None = None


def _aws_parts(body: AwsConnectIn) -> tuple[dict, dict]:
    cred = {"client_id": body.access_key_id, "secret": body.secret_access_key, "directory_id": body.role_arn or None}
    config = {"cur_bucket": body.cur_bucket or None, "cur_prefix": (body.cur_prefix or "").strip("/") or None,
              "external_id": body.external_id or None}
    return cred, config


def _aws_probe(request: Request, body: AwsConnectIn) -> dict:
    cred, config = _aws_parts(body)
    try:
        client = aws_factory(request)(cred, config)
        ident = client.identity()
        perm = client.check(config["cur_bucket"], config["cur_prefix"])
    except AwsError as exc:
        raise HTTPException(400, f"AWS rejected the credential: {exc}") from exc
    return {"account_id": ident["account_id"], "arn": ident["arn"], "permissions": perm}


def _gcp_probe(request: Request, body: GcpConnectIn) -> dict:
    try:
        key = parse_key(body.service_account_key)
        parse_table(body.table)
        client = gcp_factory(request)({"secret": body.service_account_key}, {})
        perm = client.check(body.table, body.job_project)
    except GcpError as exc:
        raise HTTPException(400, f"GCP check failed: {exc}") from exc
    return {"client_email": key["client_email"], "billing_account": billing_account_from_table(body.table), "permissions": perm}


@router.post("/tenants/{tenant_id}/connect/aws/validate")
def aws_validate(body: AwsConnectIn, request: Request, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    res = _aws_probe(request, body)
    audit.record(db, "onboarding.validate", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id,
                 detail={"provider": "aws", "access_key_id": body.access_key_id, "status": res["permissions"]["status"]}, ip=ctx.ip)
    return res


@router.post("/tenants/{tenant_id}/connect/aws", status_code=201)
def aws_connect(body: AwsConnectIn, request: Request, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    res = _aws_probe(request, body)
    checks = res["permissions"]["checks"]
    if checks.get("Cost Explorer") != "ok" and checks.get("CUR files") != "ok":
        raise HTTPException(400, f"the credential can read neither Cost Explorer nor the CUR bucket: {checks}")
    cred, config = _aws_parts(body)
    # With a role, cost belongs to the role's account (normally the payer), not the IAM user's.
    account = body.role_arn.split(":")[4] if body.role_arn else res["account_id"]
    cred_id = tenants.store_credential(db, ctx.tenant_id, "aws", cred["directory_id"], cred["client_id"], cred["secret"])
    aid = tenants.upsert_account(db, ctx.tenant_id, "aws", account, body.name or f"AWS {account}", cred_id, config=config)
    db.execute("UPDATE cloud_accounts SET permission_status = ?, permission_detail = ? WHERE id = ?",
               [res["permissions"]["status"], json.dumps(res["permissions"]), aid])
    job = sync.enqueue(db, ctx.tenant_id, aid, ctx.principal.email)
    audit.record(db, "onboarding.complete", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=cred_id,
                 detail={"provider": "aws", "account": account, "access_key_id": body.access_key_id, "cur_bucket": body.cur_bucket}, ip=ctx.ip)
    return {"credential_id": cred_id, "account_id": aid, "aws_account": account, "permissions": res["permissions"], "sync_job": job}


@router.post("/tenants/{tenant_id}/connect/gcp/validate")
def gcp_validate(body: GcpConnectIn, request: Request, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    res = _gcp_probe(request, body)
    audit.record(db, "onboarding.validate", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id,
                 detail={"provider": "gcp", "client_email": res["client_email"], "status": res["permissions"]["status"]}, ip=ctx.ip)
    return res


@router.post("/tenants/{tenant_id}/connect/gcp", status_code=201)
def gcp_connect(body: GcpConnectIn, request: Request, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    res = _gcp_probe(request, body)
    if res["permissions"]["status"] != "ok":
        raise HTTPException(400, f"missing permission: {res['permissions']['checks']}")
    key = parse_key(body.service_account_key)
    table = body.table.strip().strip("`")
    external = res["billing_account"] or table
    cred_id = tenants.store_credential(db, ctx.tenant_id, "gcp", body.job_project or parse_table(table)["project"], key["client_email"],
                                       body.service_account_key, secret_hint=f"key ••••{(key.get('private_key_id') or '')[-4:]}")
    aid = tenants.upsert_account(db, ctx.tenant_id, "gcp", external, body.name or f"GCP billing {external}", cred_id,
                                 config={"table": table, "job_project": body.job_project or None})
    db.execute("UPDATE cloud_accounts SET permission_status = ?, permission_detail = ? WHERE id = ?",
               [res["permissions"]["status"], json.dumps(res["permissions"]), aid])
    job = sync.enqueue(db, ctx.tenant_id, aid, ctx.principal.email)
    audit.record(db, "onboarding.complete", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=cred_id,
                 detail={"provider": "gcp", "billing_account": external, "client_email": key["client_email"], "table": table}, ip=ctx.ip)
    return {"credential_id": cred_id, "account_id": aid, "billing_account": external, "permissions": res["permissions"], "sync_job": job}


@router.get("/tenants/{tenant_id}/accounts")
def account_list(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    """Integration health: last sync, duration, errors, permission drift (FR-105)."""
    return tenants.list_accounts(db, ctx.tenant_id)


class AccountPatch(BaseModel):
    enabled: bool


@router.patch("/tenants/{tenant_id}/accounts/{account_id}")
def account_update(account_id: str, body: AccountPatch, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    tenants.set_account_enabled(db, ctx.tenant_id, account_id, body.enabled)
    audit.record(db, "account.update", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=account_id, detail=body.model_dump(), ip=ctx.ip)
    return {"ok": True}


@router.post("/tenants/{tenant_id}/accounts/{account_id}/sync", status_code=202)
def account_sync(account_id: str, ctx: TenantCtx = Depends(ANALYST), db: Database = Depends(db_dep)):
    acct = tenants.get_account(db, ctx.tenant_id, account_id)
    if not acct["credential_id"]:
        raise HTTPException(400, "this account is file-based; upload a new billing export instead")
    job = sync.enqueue(db, ctx.tenant_id, account_id, ctx.principal.email)
    audit.record(db, "sync.request", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=account_id, ip=ctx.ip)
    return {"job_id": job}


@router.get("/tenants/{tenant_id}/sync-jobs")
def sync_jobs(ctx: TenantCtx = Depends(VIEW), db: Database = Depends(db_dep)):
    return db.query("SELECT id, account_id, status, attempts, next_run_at, last_error, requested_by, created_at, finished_at "
                    "FROM sync_jobs WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 100", [ctx.tenant_id])


@router.get("/tenants/{tenant_id}/credentials")
def credential_list(ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    return tenants.list_credentials(db, ctx.tenant_id)


class RotateIn(BaseModel):
    secret: str = Field(min_length=8)


@router.post("/tenants/{tenant_id}/credentials/{credential_id}/rotate")
def credential_rotate(credential_id: str, body: RotateIn, request: Request, ctx: TenantCtx = Depends(ADMIN), db: Database = Depends(db_dep)):
    cur = tenants.load_credential(db, ctx.tenant_id, credential_id)
    secret_hint = None
    try:
        if cur["provider"] == "aws":  # new secret access key for the same access key id
            aws_factory(request)({**cur, "secret": body.secret, "directory_id": None}, {}).identity()
        elif cur["provider"] == "gcp":  # a new JSON key for the same service account
            key = parse_key(body.secret)
            if key["client_email"] != cur["client_id"]:
                raise HTTPException(400, f"the new key is for {key['client_email']}, not {cur['client_id']}")
            gcp_factory(request)({"secret": body.secret}, {}).token()
            secret_hint = f"key ••••{(key.get('private_key_id') or '')[-4:]}"
        else:
            azure_factory(request)(cur["directory_id"], cur["client_id"], body.secret).token()
    except (AzureError, AwsError, GcpError) as exc:
        raise HTTPException(400, f"new secret rejected by {cur['provider'].upper()}: {exc}") from exc
    tenants.rotate_credential(db, ctx.tenant_id, credential_id, body.secret, secret_hint)
    audit.record(db, "credential.rotate", user_id=ctx.principal.user_id, tenant_id=ctx.tenant_id, target=credential_id, ip=ctx.ip)
    return {"ok": True}
