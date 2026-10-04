"""Browser smoke test: the real app with demo data, driven in Chromium.

Opens the landing page, signs in with a demo token, visits every console view and fails on any
JavaScript error, Content-Security-Policy violation or error card. Skipped when Playwright or a
Chromium build is not installed (CI installs both).
"""
import os
import socket
import threading
import time

import httpx
import pytest

playwright = pytest.importorskip("playwright.sync_api")

VIEWS = ["dashboard", "explorer", "allocation", "budgets", "alerts", "recommendations", "inventory", "reports", "accounts", "admin"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(seeded):
    import uvicorn

    from cloudarc.api.app import create_app

    db, info = seeded
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(db, start_scheduler=False), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", list(info["tokens"].values())[0]
    srv.should_exit = True
    t.join(5)


@pytest.fixture
def page():
    exe = os.environ.get("CLOUDARC_CHROMIUM")  # e.g. a preinstalled Chromium when Playwright's own build is absent
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - no browser available locally
            pytest.skip(f"Chromium not available: {exc}")
        pg = browser.new_page(viewport={"width": 1360, "height": 900})
        problems: list[str] = []
        pg.on("pageerror", lambda e: problems.append(f"page error: {e}"))
        pg.on("console", lambda m: m.type == "error" and "Content Security Policy" in m.text and problems.append(f"CSP: {m.text}"))
        pg.problems = problems
        yield pg
        browser.close()


def test_landing_sign_in_and_every_view(server, page):
    base, token = server
    # Chart.js comes from cdnjs; serve an empty script so the test needs no network (charts then skip rendering).
    page.route("https://cdnjs.cloudflare.com/**", lambda r: r.fulfill(body="", content_type="application/javascript"))
    page.goto(base + "/")
    page.wait_for_selector("#login:not(.hidden)")
    assert page.is_visible("text=Master your multi") and page.is_visible("#token-form")

    page.fill("#token", token)
    page.click("#token-form button[type=submit]")
    page.wait_for_selector("#shell:not(.hidden)")
    for view in VIEWS:
        page.evaluate(f"location.hash = '#{view}'")
        page.wait_for_function("() => !document.querySelector('#view .empty')?.textContent.includes('Loading')", timeout=15000)
        errors = page.locator("#view .card.error").all_inner_texts()
        assert not errors, f"{view}: {errors}"
    assert not page.problems, page.problems
