"""Tenants (clients), cloud accounts and stored credentials."""
from __future__ import annotations

import json

from .db import Database, new_id
from .security.secrets import SecretBox, hint


class NotFound(LookupError):
    pass


def create_tenant(db: Database, name: str, currency: str = "INR", fiscal_year_start: int = 4,
                  required_tags: list[str] | None = None, tenant_id: str | None = None) -> str:
    tid = tenant_id or new_id()
    db.execute(
        "INSERT INTO tenants (id, name, currency, fiscal_year_start, required_tags) VALUES (?, ?, ?, ?, ?)",
        [tid, name, currency, fiscal_year_start, json.dumps(required_tags or ["Environment", "Department", "Application", "Owner"])],
    )
    return tid


def get_tenant(db: Database, tenant_id: str) -> dict:
    t = db.one("SELECT id, name, currency, fiscal_year_start, required_tags, created_at FROM tenants WHERE id = ?", [tenant_id])
    if not t:
        raise NotFound("tenant not found")
    t["required_tags"] = json.loads(t["required_tags"])
    return t


def update_tenant(db: Database, tenant_id: str, **changes) -> dict:
    t = get_tenant(db, tenant_id)
    t.update({k: v for k, v in changes.items() if v is not None})
    if not 1 <= int(t["fiscal_year_start"]) <= 12:
        raise ValueError("fiscal_year_start must be a month number 1-12")
    db.execute(
        "UPDATE tenants SET name = ?, currency = ?, fiscal_year_start = ?, required_tags = ? WHERE id = ?",
        [t["name"], t["currency"], int(t["fiscal_year_start"]), json.dumps(t["required_tags"]), tenant_id],
    )
    return get_tenant(db, tenant_id)


def list_tenants(db: Database, ids: list[str] | None = None) -> list[dict]:
    if ids is not None and not ids:
        return []
    cond, params = ("", []) if ids is None else (f" WHERE id IN ({', '.join('?' for _ in ids)})", ids)
    return db.query(f"SELECT id, name, currency, fiscal_year_start FROM tenants{cond} ORDER BY name", params)


def list_accounts(db: Database, tenant_id: str) -> list[dict]:
    return db.query(
        "SELECT id, provider, external_id, name, enabled, credential_id, permission_status, permission_detail, "
        "last_sync_at, last_sync_status, last_sync_duration_s, last_error, created_at "
        "FROM cloud_accounts WHERE tenant_id = ? ORDER BY provider, name",
        [tenant_id],
    )


def get_account(db: Database, tenant_id: str, account_id: str) -> dict:
    a = db.one("SELECT * FROM cloud_accounts WHERE tenant_id = ? AND id = ?", [tenant_id, account_id])
    if not a:
        raise NotFound("account not found")
    return a


def upsert_account(db: Database, tenant_id: str, provider: str, external_id: str, name: str | None,
                   credential_id: str | None, enabled: bool = True) -> str:
    existing = db.scalar(
        "SELECT id FROM cloud_accounts WHERE tenant_id = ? AND provider = ? AND external_id = ?",
        [tenant_id, provider, external_id.lower()],
    )
    if existing:
        db.execute(
            "UPDATE cloud_accounts SET name = COALESCE(?, name), credential_id = COALESCE(?, credential_id), enabled = ? WHERE id = ?",
            [name, credential_id, enabled, existing],
        )
        return existing
    aid = new_id()
    db.execute(
        "INSERT INTO cloud_accounts (id, tenant_id, provider, external_id, name, credential_id, enabled) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [aid, tenant_id, provider, external_id.lower(), name, credential_id, enabled],
    )
    return aid


def set_account_enabled(db: Database, tenant_id: str, account_id: str, enabled: bool) -> None:
    get_account(db, tenant_id, account_id)
    db.execute("UPDATE cloud_accounts SET enabled = ? WHERE tenant_id = ? AND id = ?", [enabled, tenant_id, account_id])


# ---- credentials ------------------------------------------------------------------------------

def store_credential(db: Database, tenant_id: str, provider: str, directory_id: str, client_id: str, secret: str) -> str:
    cid = new_id()
    sealed = SecretBox.from_settings().seal(secret, aad=f"cred:{tenant_id}:{cid}")
    db.execute(
        "INSERT INTO credentials (id, tenant_id, provider, directory_id, client_id, secret_ciphertext, secret_hint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [cid, tenant_id, provider, directory_id, client_id, sealed, hint(secret)],
    )
    return cid


def rotate_credential(db: Database, tenant_id: str, credential_id: str, new_secret: str) -> None:
    """Secret/certificate rotation without re-onboarding (FR-106)."""
    if not db.scalar("SELECT count(*) FROM credentials WHERE tenant_id = ? AND id = ?", [tenant_id, credential_id]):
        raise NotFound("credential not found")
    sealed = SecretBox.from_settings().seal(new_secret, aad=f"cred:{tenant_id}:{credential_id}")
    db.execute(
        "UPDATE credentials SET secret_ciphertext = ?, secret_hint = ?, rotated_at = now() WHERE tenant_id = ? AND id = ?",
        [sealed, hint(new_secret), tenant_id, credential_id],
    )


def list_credentials(db: Database, tenant_id: str) -> list[dict]:
    """Never returns the secret or its ciphertext."""
    return db.query(
        "SELECT id, provider, directory_id, client_id, secret_hint, created_at, rotated_at FROM credentials WHERE tenant_id = ?",
        [tenant_id],
    )


def load_credential(db: Database, tenant_id: str, credential_id: str) -> dict:
    """Internal use by connectors only — decrypts the secret in memory."""
    row = db.one(
        "SELECT id, provider, directory_id, client_id, secret_ciphertext FROM credentials WHERE tenant_id = ? AND id = ?",
        [tenant_id, credential_id],
    )
    if not row:
        raise NotFound("credential not found")
    secret = SecretBox.from_settings().open(row.pop("secret_ciphertext"), aad=f"cred:{tenant_id}:{credential_id}")
    return {**row, "secret": secret}
