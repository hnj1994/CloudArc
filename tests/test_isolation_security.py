"""Tenant isolation (FR-1001, NFR-02) and secret handling (FR-103) — enforced and tested, not just configured."""
import logging
import re

import pytest
from cryptography.exceptions import InvalidTag

from cloudarc import tenants
from cloudarc.security.secrets import RedactingFilter, SecretBox

FILLER = {"budget_id": "x", "alert_id": "x", "channel_id": "x", "rec_id": "x", "report_id": "x", "cc_id": "x",
          "account_id": "x", "credential_id": "x", "month": "2026-08", "name": "cost_allocation"}


def tenant_routes(app):
    """Every tenant-scoped operation in the published API (derived from the OpenAPI schema, so new routes are covered)."""
    for path, ops in app.openapi()["paths"].items():
        if "{tenant_id}" in path:
            for method in ops:
                yield method.upper(), path


def fill(path: str, tenant: str) -> str:
    path = path.replace("{tenant_id}", tenant)
    return re.sub(r"\{(\w+)\}", lambda m: FILLER.get(m.group(1), "x"), path)


def test_every_tenant_route_denies_unassigned_tenant(client):
    """Analyst is assigned to demo-a and demo-c only: every route for demo-b must answer 404, whatever the method."""
    routes = list(tenant_routes(client.app))
    assert len(routes) > 40
    for method, path in routes:
        url = fill(path, "demo-b")
        r = client.request(method, url, headers=client.hdr("analyst"), json={})
        expected = 403 if path.startswith("/api/users/") else 404  # platform-admin-only routes refuse outright
        assert r.status_code == expected, f"{method} {url} -> {r.status_code}"
        assert "demo-b" not in r.text or "not found" in r.text


def test_unassigned_and_nonexistent_tenants_look_identical(client):
    a = client.get("/api/tenants/demo-b/costs/summary", headers=client.hdr("analyst"))
    b = client.get("/api/tenants/does-not-exist/costs/summary", headers=client.hdr("analyst"))
    assert a.status_code == b.status_code == 404 and a.json() == b.json()


def test_every_tenant_route_requires_authentication(client):
    routes = list(tenant_routes(client.app))
    assert len(routes) > 40
    for method, path in routes:
        r = client.request(method, fill(path, "demo-a"), json={})
        assert r.status_code == 401, f"{method} {path}"
    assert client.get("/api/tenants/demo-a/costs/summary", headers={"Authorization": "Bearer ca_forged"}).status_code == 401


def test_query_filters_cannot_reach_other_tenant_data(client, seeded):
    db, _ = seeded
    b_account = db.scalar("SELECT id FROM cloud_accounts WHERE tenant_id = 'demo-b'")
    r = client.get(f"/api/tenants/demo-a/costs/group?dims=account&account={b_account}", headers=client.hdr("analyst"))
    assert r.status_code == 200 and r.json() == []


def test_viewer_is_read_only(client):
    h = client.hdr("viewer")
    assert client.get("/api/tenants/demo-b/budgets", headers=h).status_code == 200
    assert client.post("/api/tenants/demo-b/budgets", json={"name": "x", "amount": 10}, headers=h).status_code == 403
    assert client.post("/api/tenants/demo-b/recommendations/run", headers=h).status_code == 403
    assert client.get("/api/tenants/demo-b/audit", headers=h).status_code == 403
    assert client.get("/api/users", headers=h).status_code == 403


def test_me_lists_only_assigned_tenants(client):
    ids = {t["id"] for t in client.get("/api/me", headers=client.hdr("analyst")).json()["tenants"]}
    assert ids == {"demo-a", "demo-c"}


def test_secretbox_roundtrip_and_tamper_detection():
    box = SecretBox(b"k" * 32)
    token = box.seal("s3cr3t-value", aad="cred:t:1")
    assert "s3cr3t" not in token
    assert box.open(token, aad="cred:t:1") == "s3cr3t-value"
    with pytest.raises(InvalidTag):
        box.open(token, aad="cred:OTHER:1")  # ciphertext is bound to its tenant/credential


def test_credentials_never_returned_by_api(client, seeded):
    db, _ = seeded
    secret = "super-secret-client-value-123"
    cid = tenants.store_credential(db, "demo-a", "azure", "dir-id-1234", "client-id-1234", secret)
    assert secret not in db.scalar("SELECT secret_ciphertext FROM credentials WHERE id = ?", [cid])
    r = client.get("/api/tenants/demo-a/credentials", headers=client.hdr("admin"))
    assert r.status_code == 200
    assert secret not in r.text and "ciphertext" not in r.text
    assert r.json()[0]["secret_hint"] == "••••-123"
    assert tenants.load_credential(db, "demo-a", cid)["secret"] == secret


def test_log_redaction():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, 'posting client_secret=abc123XYZ and "password": "hunter2"', None, None)
    RedactingFilter().filter(rec)
    msg = rec.getMessage()
    assert "abc123XYZ" not in msg and "hunter2" not in msg and "[REDACTED]" in msg


def test_api_tokens_are_stored_hashed(seeded):
    db, info = seeded
    token = list(info["tokens"].values())[0]
    assert db.scalar("SELECT count(*) FROM api_tokens WHERE token_hash = ?", [token]) == 0


def test_audit_log_records_actions(client):
    h = client.hdr("analyst")
    client.post("/api/session", headers=h)
    client.post("/api/tenants/demo-a/budgets", json={"name": "Audited", "amount": 20000}, headers=h)
    actions = [r["action"] for r in client.get("/api/audit", headers=client.hdr("admin")).json()]
    assert "login" in actions and "budget.create" in actions


def test_bootstrap_admin_writes_token_file_once(db, tmp_path):
    import os
    import stat

    from cloudarc.security.auth import principal_from_api_token
    from cloudarc.security.bootstrap import bootstrap_admin

    path = bootstrap_admin(db, "owner@example.com", tmp_path)
    assert path and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    email, token = path.read_text().split()
    assert email == "owner@example.com" and principal_from_api_token(db, token).is_platform_admin
    assert bootstrap_admin(db, "other@example.com", tmp_path) is None  # never runs once users exist
