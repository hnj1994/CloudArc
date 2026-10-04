"""Response security headers, real client IPs behind App Service, and rate limits."""
from starlette.requests import Request

from cloudarc.security.ratelimit import SlidingWindow, client_ip


def test_security_headers_on_pages_and_api(client):
    for path in ("/", "/api/health", "/static/app.js"):
        h = client.get(path).headers
        assert "script-src 'self' https://cdnjs.cloudflare.com" in h["content-security-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["strict-transport-security"].startswith("max-age=")
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert "content-security-policy" not in client.get("/docs").headers  # Swagger UI loads its own CDN assets


def test_repeated_bad_tokens_are_throttled_but_valid_users_are_not(client):
    bad = {"Authorization": "Bearer ca_wrong-token-value"}
    codes = [client.get("/api/me", headers=bad).status_code for _ in range(21)]
    assert codes[:20] == [401] * 20 and codes[20] == 429
    r = client.get("/api/me", headers=client.hdr("admin"))
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0  # same IP: blocked until the window passes
    client.app.state.limits.auth_failures = SlidingWindow(20, 300)
    assert client.get("/api/me", headers=client.hdr("admin")).status_code == 200


def test_cloud_credential_checks_are_limited(client):
    h = client.hdr("admin")
    body = {"access_key_id": "AKIAABCDEFGHIJKLMNOP", "secret_access_key": "x" * 40}
    client.app.state.aws_factory = lambda cred, cfg: (_ for _ in ()).throw(RuntimeError("must not be called"))
    client.app.state.limits.cloud_checks = SlidingWindow(2, 600)
    client.app.state.limits.cloud_checks.hit("testclient")
    client.app.state.limits.cloud_checks.hit("testclient")
    assert client.post("/api/tenants/demo-a/connect/aws/validate", json=body, headers=h).status_code == 429


def _req(peer: str, xff: str | None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "client": (peer, 1234), "headers": headers})


def test_client_ip_uses_the_entry_app_service_appended():
    assert client_ip(_req("169.254.129.1", "6.6.6.6, 203.0.113.9:51234")) == "203.0.113.9"  # spoofed first entry ignored
    assert client_ip(_req("169.254.129.1", "[2001:db8::1]:443")) == "2001:db8::1"
    assert client_ip(_req("8.8.8.8", "203.0.113.9")) == "8.8.8.8"  # direct public peer: header not trusted
    assert client_ip(_req("10.0.0.4", "not-an-ip")) == "10.0.0.4"
