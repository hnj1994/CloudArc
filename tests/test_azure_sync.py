"""Azure connector + sync pipeline against a mocked Azure REST surface."""
import json
from datetime import date

import httpx
import pytest

from cloudarc import sync, tenants
from cloudarc.connectors.azure import AzureClient, evaluate_permissions

SUB = "11111111-2222-3333-4444-555555555555"
VM = f"/subscriptions/{SUB}/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"
HEADER = "SubscriptionId,SubscriptionName,Date,ResourceGroup,ResourceId,MeterCategory,MeterSubCategory,MeterName,ResourceLocation,Quantity,UnitOfMeasure,EffectivePrice,CostInBillingCurrency,BillingCurrency,PricingModel,ChargeType,Tags\n"


def cost_csv(start: str, end: str, vm_cost: float) -> bytes:
    rows, d = [], date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        rows.append(f'{SUB},Prod,{d:%m/%d/%Y},rg1,{VM},Virtual Machines,Dv5 Series,D4s v5,Central India,24,1 Hour,10,{vm_cost},INR,OnDemand,Usage,"""env"": ""prod"""')
        d = date.fromordinal(d.toordinal() + 1)
    return (HEADER + "\n".join(rows) + "\n").encode()


class FakeAzure:
    def __init__(self, write_access=False, vm_cost=100.0):
        self.write_access, self.vm_cost = write_access, vm_cost
        self.polls = 0
        self.requests: list[str] = []
        self.pending: dict[str, tuple[str, str]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(f"{request.method} {url}")
        if "oauth2/v2.0/token" in url:
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            if form.get("client_secret") != "good-secret-value":
                return httpx.Response(401, json={"error_description": "AADSTS7000215: Invalid client secret"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers.get("Authorization") == "Bearer tok" or "blob" in url or "prices.azure.com" in url
        if url.startswith("https://management.azure.com/subscriptions?"):
            return httpx.Response(200, json={"value": [{"subscriptionId": SUB, "displayName": "Prod", "state": "Enabled"}]})
        if "Microsoft.Authorization/permissions" in url:
            actions = ["*/read"] + (["*"] if self.write_access else [])
            return httpx.Response(200, json={"value": [{"actions": actions, "notActions": []}]})
        if "generateCostDetailsReport" in url:
            body = json.loads(request.content)
            op = f"op{len(self.pending)}"
            self.pending[op] = (body["timePeriod"]["start"], body["timePeriod"]["end"])
            return httpx.Response(202, headers={"Location": f"https://management.azure.com/ops/{op}", "Retry-After": "0"})
        if "/ops/" in url:
            op = url.rsplit("/", 1)[-1]
            self.polls += 1
            if self.polls % 2:
                return httpx.Response(202, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"status": "Completed", "manifest": {"blobs": [{"blobLink": f"https://blob.example/{op}.csv"}]}})
        if url.startswith("https://blob.example/"):
            start, end = self.pending[url.rsplit("/", 1)[-1][:-4]]
            return httpx.Response(200, content=cost_csv(start, end, self.vm_cost))
        if "Microsoft.ResourceGraph" in url:
            q = json.loads(request.content)["query"]
            if q.startswith("ResourceContainers"):
                return httpx.Response(200, json={"data": [{"id": f"/subscriptions/{SUB}/resourceGroups/rg1", "name": "rg1",
                                                            "type": "microsoft.resources/subscriptions/resourcegroups", "resourceGroup": "rg1"}]})
            return httpx.Response(200, json={"data": [{"id": VM, "name": "vm1", "type": "microsoft.compute/virtualmachines", "resourceGroup": "rg1",
                                                        "location": "centralindia", "tags": {"env": "prod"},
                                                        "properties": {"hardwareProfile": {"vmSize": "Standard_D4s_v5"}}}]})
        if "Microsoft.Insights/metrics" in url:
            data = [{"timeStamp": f"2026-09-{d:02d}T00:00:00Z", "average": 5.0, "maximum": 20.0, "minimum": 1.0} for d in range(1, 30)]
            return httpx.Response(200, json={"value": [{"name": {"value": "Percentage CPU"}, "timeseries": [{"data": data}]}]})
        if "Microsoft.Advisor" in url:
            return httpx.Response(200, json={"value": []})
        if "prices.azure.com" in url:
            return httpx.Response(200, json={"Items": [
                {"type": "Consumption", "retailPrice": 20.0, "meterName": "D4s v5", "productName": "Virtual Machines Dsv5 Series"},
                {"type": "Consumption", "retailPrice": 10.0, "meterName": "D2s v5", "productName": "Virtual Machines Dsv5 Series"},
                {"type": "Reservation", "retailPrice": 105120.0, "reservationTerm": "1 Year", "meterName": "D4s v5", "productName": "Virtual Machines Dsv5 Series"},
            ], "NextPageLink": None})
        return httpx.Response(404, json={"error": url})


def make_client(fake, directory="dir", client_id="app", secret="good-secret-value"):
    return AzureClient(directory, client_id, secret, http=httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda _: None)


def test_permission_evaluation():
    assert evaluate_permissions([{"actions": ["*/read"], "notActions": []}])["status"] == "ok"
    over = evaluate_permissions([{"actions": ["*"], "notActions": []}])
    assert over["status"] == "over_privileged" and over["write_actions"]
    assert evaluate_permissions([{"actions": ["Microsoft.CostManagement/*/read"], "notActions": []}])["status"] == "missing_roles"


def test_client_never_leaks_secret_in_repr():
    assert "good-secret-value" not in repr(make_client(FakeAzure()))


def test_sync_account_end_to_end_and_resync_is_idempotent(db):
    tenants.create_tenant(db, "Client", tenant_id="t1")
    cred = tenants.store_credential(db, "t1", "azure", "dir", "app", "good-secret-value")
    aid = tenants.upsert_account(db, "t1", "azure", SUB, "Prod", cred)
    fake = FakeAzure()
    factory = lambda c: make_client(fake, c["directory_id"], c["client_id"], c["secret"])  # noqa: E731

    res = sync.sync_account(db, "t1", aid, factory, today=date(2026, 9, 29))
    assert res["permissions"] == "ok" and res["cost_rows"] > 0
    assert res["window"][0] == "2026-06-01"  # initial backfill: 3 months before the current month
    first = db.scalar("SELECT SUM(cost) FROM cost_records WHERE tenant_id = 't1'")
    days = db.scalar("SELECT count(DISTINCT charge_date) FROM cost_records WHERE tenant_id = 't1'")
    assert days == (date(2026, 9, 29) - date(2026, 6, 1)).days + 1
    assert db.scalar("SELECT count(*) FROM resources WHERE tenant_id = 't1'") == 2
    assert db.scalar("SELECT count(*) FROM resource_metrics WHERE tenant_id = 't1'") == 29
    assert db.scalar("SELECT hourly_price FROM price_catalog WHERE sku = 'Standard_D4s_v5' AND pricing = 'reservation_1y'") == pytest.approx(12.0)

    # Re-sync with a restated price: the 5-day look-back window is replaced, nothing is duplicated.
    fake.vm_cost = 110.0
    res2 = sync.sync_account(db, "t1", aid, factory, today=date(2026, 9, 29))
    assert res2["window"][0] == "2026-09-24"
    assert db.scalar("SELECT count(DISTINCT charge_date) FROM cost_records WHERE tenant_id = 't1'") == days
    assert db.scalar("SELECT count(*) FROM cost_records WHERE tenant_id = 't1'") == days
    assert db.scalar("SELECT SUM(cost) FROM cost_records WHERE tenant_id = 't1'") == pytest.approx(first + 6 * 10.0)

    out = sync.post_sync(db, "t1", date(2026, 9, 29), notify=False)
    assert out["recommendations"]["created"] >= 1


def test_failed_sync_retries_with_backoff_then_alerts(db):
    from datetime import datetime, timedelta

    tenants.create_tenant(db, "Client", tenant_id="t1")
    cred = tenants.store_credential(db, "t1", "azure", "dir", "app", "revoked-secret-000")
    aid = tenants.upsert_account(db, "t1", "azure", SUB, "Prod", cred)
    fake = FakeAzure()
    factory = lambda c: make_client(fake, c["directory_id"], c["client_id"], c["secret"])  # noqa: E731
    sync.enqueue(db, "t1", aid, "test")
    now = datetime.utcnow()
    for i in range(sync.MAX_ATTEMPTS):
        assert sync.run_due_jobs(db, factory, now=now + timedelta(hours=i)) == 1
    job = db.one("SELECT status, attempts, last_error FROM sync_jobs")
    assert job["status"] == "failed" and job["attempts"] == sync.MAX_ATTEMPTS and "Invalid client secret" in job["last_error"]
    assert db.scalar("SELECT kind FROM alerts WHERE tenant_id = 't1'") == "sync_failure"
    assert db.scalar("SELECT last_sync_status FROM cloud_accounts WHERE id = ?", [aid]) == "failed"


def test_onboarding_wizard_api(client):
    fake = FakeAzure(write_access=True)
    client.app.state.azure_factory = lambda d, c, s: make_client(fake, d, c, s)
    h = client.hdr("admin")
    body = {"directory_id": "dir-00000001", "client_id": "app-00000001", "secret": "good-secret-value"}
    v = client.post("/api/tenants/demo-a/onboarding/validate", json=body, headers=h)
    assert v.status_code == 200
    sub = v.json()["subscriptions"][0]
    assert sub["subscription_id"] == SUB and sub["permissions"]["status"] == "over_privileged"
    assert client.post("/api/tenants/demo-a/onboarding/validate", json={**body, "secret": "wrong-secret-1"}, headers=h).status_code == 400
    done = client.post("/api/tenants/demo-a/onboarding/complete", json={**body, "subscriptions": [{"subscription_id": SUB}]}, headers=h)
    assert done.status_code == 201
    accounts = client.get("/api/tenants/demo-a/accounts", headers=h).json()
    assert any(a["external_id"] == SUB and a["credential_id"] for a in accounts)
    assert "good-secret-value" not in client.get("/api/tenants/demo-a/credentials", headers=h).text
    # analysts cannot onboard
    assert client.post("/api/tenants/demo-a/onboarding/validate", json=body, headers=client.hdr("analyst")).status_code == 403
    # rotation validates the new secret before storing it
    cred = done.json()["credential_id"]
    assert client.post(f"/api/tenants/demo-a/credentials/{cred}/rotate", json={"secret": "bad-secret-99"}, headers=h).status_code == 400
    assert client.post(f"/api/tenants/demo-a/credentials/{cred}/rotate", json={"secret": "good-secret-value"}, headers=h).status_code == 200


def test_api_upload_reports_and_exports(client, tmp_path):
    h = client.hdr("analyst")
    csv_bytes = cost_csv("2026-09-01", "2026-09-03", 50.0)
    r = client.post("/api/tenants/demo-a/ingest", files={"file": ("costs.csv", csv_bytes, "text/csv")}, headers=h)
    assert r.status_code == 200 and r.json()["reconciled"] and r.json()["rows_loaded"] == 3
    r = client.post("/api/tenants/demo-a/reports/monthly?month=2026-08&format=docx", headers=h)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    runs = client.get("/api/tenants/demo-a/reports", headers=h).json()
    assert runs and client.get(f"/api/tenants/demo-a/reports/{runs[0]['id']}/download", headers=h).status_code == 200
    for name in ("cost_allocation", "resource_explorer", "unit_economics", "budget_vs_actual", "tag_compliance"):
        x = client.get(f"/api/tenants/demo-a/exports/{name}?format=xlsx", headers=h)
        assert x.status_code == 200 and x.content[:2] == b"PK", name
    rec = client.post("/api/tenants/demo-a/reconcile", json={"month": "2026-08", "provider_total": 11421.49}, headers=h).json()
    assert rec["within_tolerance"] is True


def test_sync_survives_unregistered_insights_and_advisor(db):
    """Pay-as-you-go subscriptions often lack Microsoft.Insights / Microsoft.Advisor registration."""

    class Unregistered(FakeAzure):
        def __call__(self, request):
            url = str(request.url)
            if "Microsoft.Insights/metrics" in url or "Microsoft.Advisor" in url:
                self.requests.append(f"{request.method} {url}")
                return httpx.Response(409, json={"error": {"code": "MissingSubscriptionRegistration",
                                                           "message": "The subscription is not registered to use namespace"}})
            return super().__call__(request)

    tenants.create_tenant(db, "Client", tenant_id="t1")
    cred = tenants.store_credential(db, "t1", "azure", "dir", "app", "good-secret-value")
    aid = tenants.upsert_account(db, "t1", "azure", SUB, "Prod", cred)
    fake = Unregistered()
    res = sync.sync_account(db, "t1", aid, lambda c: make_client(fake, c["directory_id"], c["client_id"], c["secret"]),
                            today=date(2026, 9, 29))
    assert res["cost_rows"] > 0 and res["metric_points"] == 0
    assert len(res["warnings"]) == 2 and all("MissingSubscriptionRegistration" in w for w in res["warnings"])
    acct = db.one("SELECT last_sync_status, last_error FROM cloud_accounts WHERE id = ?", [aid])
    assert acct["last_sync_status"] == "partial" and "register the resource provider" in acct["last_error"]
    # only one metrics call was attempted: the cause is subscription-wide
    assert sum("Microsoft.Insights/metrics" in r for r in fake.requests) == 1
