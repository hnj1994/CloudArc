"""Entra ID access-token validation (FR-1003) with a locally generated signing key."""
import dataclasses
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cloudarc.config import get_settings
from cloudarc.security import auth

TENANT, CLIENT = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Jwks:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


@pytest.fixture
def entra(monkeypatch, db):
    settings = dataclasses.replace(get_settings(), auth_mode="entra", entra_tenant_id=TENANT, entra_client_id=CLIENT)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "_jwks_client", lambda tenant: _Jwks())
    auth.create_user(db, "owner@example.com", "Owner", is_platform_admin=True)
    return db


def token(**overrides):
    claims = {"aud": f"api://{CLIENT}", "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
              "exp": int(time.time()) + 600, "iat": int(time.time()), "preferred_username": "Owner@Example.com"}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def test_valid_v2_token_maps_to_provisioned_user(entra):
    p = auth.principal_from_entra_jwt(entra, token())
    assert p.email == "owner@example.com" and p.is_platform_admin


def test_v1_guest_token_uses_unique_name(entra):
    t = token(iss=f"https://sts.windows.net/{TENANT}/", preferred_username=None, unique_name="live.com#owner@example.com")
    assert auth.principal_from_entra_jwt(entra, t).email == "owner@example.com"


@pytest.mark.parametrize("bad", [
    {"aud": "api://someone-else"},
    {"iss": "https://login.microsoftonline.com/33333333-3333-3333-3333-333333333333/v2.0"},
    {"exp": int(time.time()) - 60},
])
def test_rejects_wrong_audience_issuer_or_expired(entra, bad):
    with pytest.raises(auth.AuthError):
        auth.principal_from_entra_jwt(entra, token(**bad))


def test_rejects_unprovisioned_user(entra):
    with pytest.raises(auth.AuthError, match="not provisioned"):
        auth.principal_from_entra_jwt(entra, token(preferred_username="stranger@example.com"))


def test_rejects_token_signed_by_another_key(entra):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"aud": f"api://{CLIENT}", "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
                         "exp": int(time.time()) + 600, "preferred_username": "owner@example.com"}, other, algorithm="RS256")
    with pytest.raises(auth.AuthError):
        auth.principal_from_entra_jwt(entra, forged)
