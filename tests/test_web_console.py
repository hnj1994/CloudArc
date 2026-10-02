"""Console page: Search Console verification tag and the SSO-first sign-in markup."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from cloudarc.api import app as app_module
from cloudarc.config import get_settings

META = '<meta name="google-site-verification"'


@pytest.fixture
def client_with(monkeypatch, db):
    def make(**overrides):
        settings = dataclasses.replace(get_settings(), **overrides)
        monkeypatch.setattr(app_module, "get_settings", lambda: settings)
        return TestClient(app_module.create_app(db, start_scheduler=False))
    return make


def test_no_verification_tag_by_default(client_with):
    body = client_with(google_site_verification=None).get("/").text
    assert META not in body


def test_verification_tag_served_in_head(client_with):
    body = client_with(google_site_verification="AbC_123-xyzVerificationToken").get("/").text
    head = body.split("</head>", 1)[0]
    assert f'{META} content="AbC_123-xyzVerificationToken">' in head


@pytest.mark.parametrize("bad", ['x"><script>alert(1)</script>', "short", "a b c d e f g h i j"])
def test_verification_tag_rejects_unexpected_values(client_with, bad):
    body = client_with(google_site_verification=bad).get("/").text
    assert META not in body and "<script>alert" not in body


def test_token_form_hidden_until_script_decides(client_with):
    body = client_with().get("/").text
    assert '<form id="token-form" class="hidden">' in body
    assert 'id="token-toggle"' in body
