"""Users, roles, API tokens and Microsoft Entra ID token validation (FR-1001 … FR-1003).

Roles (per tenant): viewer < analyst < tenant_admin. Platform admins manage
every tenant. A user sees only the tenants explicitly assigned to them; every
tenant-scoped API route checks this before touching data.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from functools import lru_cache

import jwt

from ..config import get_settings
from ..db import Database, new_id

ROLE_RANK = {"viewer": 0, "analyst": 1, "tenant_admin": 2}


class AuthError(Exception):
    pass


@dataclass
class Principal:
    user_id: str
    email: str
    name: str | None
    is_platform_admin: bool
    tenants: dict[str, str] = field(default_factory=dict)  # tenant_id -> role

    def role_in(self, tenant_id: str) -> str | None:
        if self.is_platform_admin:
            return "tenant_admin"
        return self.tenants.get(tenant_id)

    def can(self, tenant_id: str, min_role: str) -> bool:
        role = self.role_in(tenant_id)
        return role is not None and ROLE_RANK[role] >= ROLE_RANK[min_role]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(db: Database, email: str, name: str | None = None, is_platform_admin: bool = False) -> str:
    existing = db.scalar("SELECT id FROM users WHERE lower(email) = lower(?)", [email])
    if existing:
        return existing
    uid = new_id()
    db.execute("INSERT INTO users (id, email, name, is_platform_admin) VALUES (?, ?, ?, ?)", [uid, email.lower(), name, is_platform_admin])
    return uid


def assign_role(db: Database, user_id: str, tenant_id: str, role: str) -> None:
    if role not in ROLE_RANK:
        raise ValueError(f"role must be one of {sorted(ROLE_RANK)}")
    db.execute(
        "INSERT INTO user_tenants (user_id, tenant_id, role) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, tenant_id) DO UPDATE SET role = excluded.role",
        [user_id, tenant_id, role],
    )


def revoke_role(db: Database, user_id: str, tenant_id: str) -> None:
    db.execute("DELETE FROM user_tenants WHERE user_id = ? AND tenant_id = ?", [user_id, tenant_id])


def issue_token(db: Database, user_id: str, label: str = "api") -> str:
    """Returns the plaintext token once; only its SHA-256 hash is stored."""
    token = "ca_" + secrets.token_urlsafe(32)
    db.execute("INSERT INTO api_tokens (token_hash, user_id, label) VALUES (?, ?, ?)", [_hash(token), user_id, label])
    return token


def load_principal(db: Database, user_id: str) -> Principal:
    u = db.one("SELECT id, email, name, is_platform_admin FROM users WHERE id = ?", [user_id])
    if not u:
        raise AuthError("unknown user")
    tenants = {r["tenant_id"]: r["role"] for r in db.query("SELECT tenant_id, role FROM user_tenants WHERE user_id = ?", [user_id])}
    return Principal(u["id"], u["email"], u["name"], bool(u["is_platform_admin"]), tenants)


def principal_from_api_token(db: Database, token: str) -> Principal:
    uid = db.scalar("SELECT user_id FROM api_tokens WHERE token_hash = ?", [_hash(token)])
    if not uid:
        raise AuthError("invalid token")
    return load_principal(db, uid)


@lru_cache
def _jwks_client(tenant_id: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys", cache_keys=True)


def principal_from_entra_jwt(db: Database, token: str) -> Principal:
    """Validate an Entra ID access/ID token (signature, audience, issuer, expiry); MFA is enforced by Entra."""
    s = get_settings()
    if not (s.entra_tenant_id and s.entra_client_id):
        raise AuthError("Entra ID is not configured")
    try:
        key = _jwks_client(s.entra_tenant_id).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"],
            audience=[s.entra_client_id, f"api://{s.entra_client_id}"],
            issuer=[f"https://login.microsoftonline.com/{s.entra_tenant_id}/v2.0", f"https://sts.windows.net/{s.entra_tenant_id}/"],
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid Entra token: {type(exc).__name__}") from exc
    email = (claims.get("preferred_username") or claims.get("email") or claims.get("upn") or "").lower()
    uid = db.scalar("SELECT id FROM users WHERE lower(email) = ?", [email])
    if not uid:
        raise AuthError("user is not provisioned in CloudArc; ask a platform admin to add you")
    return load_principal(db, uid)


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and not token.startswith("ca_")
