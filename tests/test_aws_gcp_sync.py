"""AWS (Cost Explorer + CUR in S3) and GCP (BigQuery billing export) connectors and their sync paths."""
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import boto3
import httpx
import jwt
import pytest
from botocore.stub import Stubber
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from cloudarc import sync, tenants
from cloudarc.connectors.aws import AwsClient, AwsError
from cloudarc.connectors.gcp import GcpClient, GcpError, billing_account_from_table, export_query, parse_table

TODAY = date(2026, 10, 2)
PAYER, DEV = "111111111111", "222222222222"
KEY_ID, SECRET = "AKIAABCDEFGHIJKLMNOP", "s" * 40


def total(db, tenant, where="", params=()):
    return round(db.scalar(f"SELECT COALESCE(SUM(cost), 0) FROM cost_records WHERE tenant_id = ? {where}", [tenant, *params]), 2)


# ---- AWS --------------------------------------------------------------------------------------------

def cur_csv(period: str, cost: float) -> bytes:
    y, m = map(int, period.split("-"))
    d, rows = date(y, m, 1), []
    while d.month == m:
        rows.append(f"{PAYER},{d}T00:00:00Z,{cost},Usage,i-0abc,AmazonEC2,USD")
        d += timedelta(days=1)
    head = "line_item_usage_account_id,line_item_usage_start_date,line_item_unblended_cost,line_item_line_item_type,line_item_resource_id,line_item_product_code,line_item_currency_code\n"
    return (head + "\n".join(rows) + "\n").encode()


class FakeAws:
    """Stands in for AwsClient: Cost Explorer returns 10 + 2.5 per day for the payer and 4 for a linked account."""

    def __init__(self, cur: dict | None = None, skew: float = 0.0):
        self.cur, self.skew = cur or {}, skew
        self.ce_calls = 0

    def check(self, bucket, prefix):
        checks = {"Cost Explorer": "ok", **({"CUR files": "ok"} if bucket else {})}
        return {"status": "ok", "checks": checks, "missing": [], "required": {}}

    def daily_cost(self, d0, d1):
        self.ce_calls += 1
        rows, d = [], d0
        while d <= d1:
            for acct, name, svc, amt in ((PAYER, "Payer", "Amazon EC2", 10.0), (PAYER, "Payer", "Amazon S3", 2.5), (DEV, "Dev", "Amazon EC2", 4.0)):
                rows.append({"line_item_usage_account_id": acct, "line_item_usage_account_name": name, "line_item_usage_start_date": d.isoformat(),
                             "line_item_product_code": svc, "product_product_name": svc, "line_item_line_item_type": "Usage",
                             "line_item_unblended_cost": str(amt), "line_item_currency_code": "USD"})
            d += timedelta(days=1)
        return rows

    def total_cost(self, d0, d1):
        return 16.5 * ((d1 - d0).days + 1) + self.skew

    def cur_files(self, bucket, prefix, period):
        return [f"{prefix}/cloudarc/data/BILLING_PERIOD={period}/part-0.csv.gz"] if period in self.cur else []

    def download(self, bucket, key, dest):
        import gzip

        period = key.split("BILLING_PERIOD=")[1][:7]
        Path(dest).write_bytes(gzip.compress(cur_csv(period, self.cur[period])))


def aws_account(db, cur_bucket=None):
    tenants.create_tenant(db, "AWS client", tenant_id="t-aws")
    cred = tenants.store_credential(db, "t-aws", "aws", None, KEY_ID, SECRET)
    return tenants.upsert_account(db, "t-aws", "aws", PAYER, "AWS payer", cred,
                                  config={"cur_bucket": cur_bucket, "cur_prefix": "cur" if cur_bucket else None})


def test_aws_backfill_uses_cur_where_present_and_resync_is_idempotent(db):
    aid = aws_account(db, cur_bucket="billing")
    fake = FakeAws(cur={"2026-09": 20.0, "2026-10": 20.0})
    res = sync.sync_account(db, "t-aws", aid, aws_factory=lambda c, cfg: fake, today=TODAY)

    assert res["window"] == ["2025-10-01", "2026-10-01"]
    assert res["sources"]["2026-09"] == "cur" and res["sources"]["2026-08"] == "cost-explorer"
    sep = ("AND charge_date BETWEEN ? AND ?", (date(2026, 9, 1), date(2026, 9, 30)))
    # September is the CUR line items (payer 20/day, resource-level), not the Cost Explorer summary.
    assert total(db, "t-aws", *sep) == 600.0
    assert db.scalar("SELECT count(*) FROM cost_records WHERE tenant_id = 't-aws' AND resource_id = 'i-0abc' AND charge_date < '2026-10-01'") == 30
    # Linked accounts are registered from the data; August came from Cost Explorer for both accounts.
    names = {r["external_id"]: r["name"] for r in tenants.list_accounts(db, "t-aws")}
    assert names[DEV] == "Dev" and names[PAYER] == "Payer"  # AWS's own account name, as for Azure subscriptions
    aug = ("AND charge_date BETWEEN ? AND ?", (date(2026, 8, 1), date(2026, 8, 31)))
    assert total(db, "t-aws", *aug) == round(16.5 * 31, 2)
    assert tenants.get_account(db, "t-aws", aid)["last_sync_status"] == "succeeded"

    before = (total(db, "t-aws"), db.scalar("SELECT count(*) FROM cost_records WHERE tenant_id = 't-aws'"))
    again = sync.sync_account(db, "t-aws", aid, aws_factory=lambda c, cfg: fake, today=TODAY)
    assert again["window"][0] == "2026-09-26"  # look-back after the first successful load
    assert (total(db, "t-aws"), db.scalar("SELECT count(*) FROM cost_records WHERE tenant_id = 't-aws'")) == before


def test_aws_cost_explorer_total_mismatch_fails_the_sync(db):
    aid = aws_account(db)
    with pytest.raises(RuntimeError, match="reconciliation mismatch"):
        sync.sync_account(db, "t-aws", aid, aws_factory=lambda c, cfg: FakeAws(skew=50.0), today=TODAY)


def test_aws_missing_cur_files_for_current_month_is_a_warning(db):
    aid = aws_account(db, cur_bucket="billing")
    res = sync.sync_account(db, "t-aws", aid, aws_factory=lambda c, cfg: FakeAws(), today=TODAY)
    assert res["sources"]["2026-10"] == "cost-explorer"
    assert any("no CUR files yet for 2026-10" in w for w in res["warnings"])
    assert tenants.get_account(db, "t-aws", aid)["last_sync_status"] == "partial"


class StubSession:
    def __init__(self, clients):
        self.clients = clients

    def client(self, name, region_name=None, config=None):
        return self.clients[name]


def stubbed(service):
    c = boto3.client(service, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    return c, Stubber(c)


def test_aws_client_pages_cost_explorer_and_names_accounts():
    ce, stub = stubbed("ce")
    stub.add_response("get_dimension_values", {"DimensionValues": [{"Value": DEV, "Attributes": {"description": "Dev"}}],
                                               "ReturnSize": 1, "TotalSize": 1})
    day = lambda d, groups: {"TimePeriod": {"Start": d, "End": d}, "Total": {}, "Estimated": False,  # noqa: E731
                             "Groups": [{"Keys": k, "Metrics": {"AmortizedCost": {"Amount": a, "Unit": "USD"}}} for k, a in groups]}
    expected = {"TimePeriod": {"Start": "2026-09-01", "End": "2026-09-03"}, "Granularity": "DAILY", "Metrics": ["AmortizedCost"],
                "GroupBy": [{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}, {"Type": "DIMENSION", "Key": "SERVICE"}]}
    stub.add_response("get_cost_and_usage", {"ResultsByTime": [day("2026-09-01", [([DEV, "Amazon EC2"], "4.5"), ([DEV, "Tax"], "0")])],
                                             "NextPageToken": "p2"}, expected)
    stub.add_response("get_cost_and_usage", {"ResultsByTime": [day("2026-09-02", [([DEV, "Amazon EC2"], "5.25")])]},
                      {**expected, "NextPageToken": "p2"})
    with stub:
        client = AwsClient(KEY_ID, SECRET, session_factory=lambda **kw: StubSession({"ce": ce}))
        rows = client.daily_cost(date(2026, 9, 1), date(2026, 9, 2))
    assert [(r["line_item_usage_start_date"], r["line_item_unblended_cost"]) for r in rows] == [("2026-09-01", "4.5"), ("2026-09-02", "5.25")]
    assert rows[0]["line_item_usage_account_name"] == "Dev"  # zero-cost groups are dropped
    assert SECRET not in repr(client)


def test_aws_cur_files_use_latest_delivery_folder_only():
    s3, stub = stubbed("s3")
    t = lambda h: datetime(2026, 10, 1, h, tzinfo=UTC)  # noqa: E731
    stub.add_response("list_objects_v2", {"IsTruncated": True, "NextContinuationToken": "c2", "Contents": [
        {"Key": "cur/cloudarc/data/BILLING_PERIOD=2026-09/v1/part-0.parquet", "LastModified": t(1)},
        {"Key": "cur/cloudarc/data/BILLING_PERIOD=2026-08/part-0.parquet", "LastModified": t(9)},
        {"Key": "cur/cloudarc/metadata/BILLING_PERIOD=2026-09/manifest.json", "LastModified": t(9)}]},
        {"Bucket": "billing", "Prefix": "cur/"})
    stub.add_response("list_objects_v2", {"IsTruncated": False, "Contents": [
        {"Key": "cur/cloudarc/data/BILLING_PERIOD=2026-09/v2/part-0.parquet", "LastModified": t(5)},
        {"Key": "cur/cloudarc/data/BILLING_PERIOD=2026-09/v2/part-1.parquet", "LastModified": t(5)}]},
        {"Bucket": "billing", "Prefix": "cur/", "ContinuationToken": "c2"})
    with stub:
        client = AwsClient(KEY_ID, SECRET, session_factory=lambda **kw: StubSession({"s3": s3}))
        keys = client.cur_files("billing", "/cur/", "2026-09")
    assert keys == ["cur/cloudarc/data/BILLING_PERIOD=2026-09/v2/part-0.parquet", "cur/cloudarc/data/BILLING_PERIOD=2026-09/v2/part-1.parquet"]


def test_aws_rejects_malformed_role_arn():
    with pytest.raises(AwsError):
        AwsClient(KEY_ID, SECRET, role_arn="arn:aws:iam::123:user/x")


# ---- GCP --------------------------------------------------------------------------------------------

PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SA = "cloudarc-reader@proj-123.iam.gserviceaccount.com"
KEY = json.dumps({"type": "service_account", "project_id": "proj-123", "client_email": SA, "private_key_id": "abcd1234ef",
                  "token_uri": "https://oauth2.googleapis.com/token",
                  "private_key": PRIVATE.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()).decode()})
TABLE = "proj-123.billing_export.gcp_billing_export_resource_v1_0123AB_4567CD_89EF01"
ENABLED = date(2026, 9, 15)  # the export only has data from this day
FIELDS = ["billing_account_id", "service_description", "sku_description", "usage_start_time", "project_id", "project_name", "labels",
          "location_region", "cost", "currency", "usage_amount_in_pricing_units", "usage_pricing_unit", "credits", "cost_type",
          "resource_global_name"]


class FakeBigQuery:
    def __init__(self, lie_about_total=False):
        self.jobs: dict[str, list] = {}
        self.sql: list[str] = []
        self.lie = lie_about_total

    def rows(self, d0: date, d1: date) -> list:
        out, d = [], max(d0, ENABLED)
        while d <= d1:
            ts = f"{d}T05:00:00Z"
            out.append(["0123AB-4567CD-89EF01", "Compute Engine", "N2 core", ts, "web-prod", "Web prod", '[{"key":"env","value":"prod"}]',
                        "asia-south1", "12.5", "USD", "24", "hour", '[{"name":"SUD","amount":-2.5}]', "regular",
                        "//compute.googleapis.com/projects/web-prod/zones/asia-south1-a/instances/web-1"])
            out.append(["0123AB-4567CD-89EF01", "Support", "Enhanced", ts, "0123ab-4567cd-89ef01", None, "[]",
                        None, "3", "USD", None, None, "[]", "regular", None])
            d += timedelta(days=1)
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            claims = jwt.decode(form["assertion"][0], PRIVATE.public_key(), algorithms=["RS256"], audience="https://oauth2.googleapis.com/token")
            assert claims["iss"] == SA
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        if "/tables/" in url:
            return httpx.Response(200, json={"location": "US", "timePartitioning": {"type": "DAY"}, "numRows": "10",
                                             "schema": {"fields": [{"name": n} for n in ("billing_account_id", "service", "sku", "usage_start_time",
                                                                                         "project", "cost", "currency", "resource", "labels")]}})
        if url.endswith("/jobs") and request.method == "POST":
            assert json.loads(request.content)["configuration"]["dryRun"] is True
            return httpx.Response(200, json={"statistics": {"totalBytesProcessed": "1024"}})
        if url.endswith("/queries") and request.method == "POST":
            body = json.loads(request.content)
            self.sql.append(body["query"])
            p = {x["name"]: date.fromisoformat(x["parameterValue"]["value"]) for x in body["queryParameters"]}
            jid = f"job{len(self.jobs)}"
            self.jobs[jid] = self.rows(p["d0"], p["d1"])
            return httpx.Response(200, json={"jobComplete": False, "jobReference": {"jobId": jid, "location": "US"}})
        if "/queries/job" in url:
            jid = request.url.path.rsplit("/", 1)[1]
            rows = self.jobs[jid]
            half = len(rows) // 2
            page = rows[half:] if request.url.params.get("pageToken") else rows[:half]
            body = {"jobComplete": True, "totalRows": str(len(rows) + (1 if self.lie else 0)),
                    "schema": {"fields": [{"name": n} for n in FIELDS]}, "rows": [{"f": [{"v": v} for v in r]} for r in page]}
            if not request.url.params.get("pageToken") and half:
                body["pageToken"] = "next"
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": {"message": f"unexpected {request.method} {url}"}})


def gcp(fake: FakeBigQuery) -> GcpClient:
    return GcpClient(KEY, http=httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=lambda s: None)


def gcp_account(db):
    tenants.create_tenant(db, "GCP client", tenant_id="t-gcp")
    cred = tenants.store_credential(db, "t-gcp", "gcp", "proj-123", SA, KEY, secret_hint="key ••••34ef")
    return tenants.upsert_account(db, "t-gcp", "gcp", "0123AB-4567CD-89EF01", "GCP billing", cred, config={"table": TABLE})


def test_gcp_sync_reads_export_net_of_credits_and_is_idempotent(db):
    aid = gcp_account(db)
    fake = FakeBigQuery()
    res = sync.sync_account(db, "t-gcp", aid, gcp_factory=lambda c, cfg: gcp(fake), today=TODAY)
    days = (TODAY - timedelta(days=1) - ENABLED).days + 1  # 15 Sep .. 1 Oct
    assert res["cost_rows"] == 2 * days and not res["warnings"]
    assert total(db, "t-gcp") == round(days * (12.5 - 2.5 + 3), 2)  # net of credits, plus project-less support charges
    # Support charges land on the connection itself (lower-cased billing account), projects on their own rows.
    accts = {a["external_id"]: a for a in tenants.list_accounts(db, "t-gcp")}
    assert set(accts) == {"0123ab-4567cd-89ef01", "web-prod"} and accts["web-prod"]["name"] == "Web prod"
    assert accts["0123ab-4567cd-89ef01"]["name"] == "GCP billing"
    assert db.scalar("SELECT count(*) FROM cost_records WHERE tenant_id = 't-gcp' AND resource_name = 'web-1'") == days
    assert "_PARTITIONTIME >= TIMESTAMP(@d0)" in fake.sql[0] and "resource.global_name" in fake.sql[0]

    before = total(db, "t-gcp")
    again = sync.sync_account(db, "t-gcp", aid, gcp_factory=lambda c, cfg: gcp(fake), today=TODAY)
    assert again["window"][0] == "2026-09-26" and total(db, "t-gcp") == before


def test_gcp_short_page_read_fails_instead_of_loading_partial_data(db):
    aid = gcp_account(db)
    with pytest.raises(GcpError, match="returned"):
        sync.sync_account(db, "t-gcp", aid, gcp_factory=lambda c, cfg: gcp(FakeBigQuery(lie_about_total=True)), today=TODAY)
    assert total(db, "t-gcp") == 0


def test_gcp_table_names_are_validated_before_use():
    assert billing_account_from_table(TABLE) == "0123AB-4567CD-89EF01"
    for bad in ("proj-123.ds.t` WHERE 1=1; --", "ds.table", "proj.ds.t"):
        with pytest.raises(GcpError):
            parse_table(bad)
    sql, params = export_query(TABLE, {"resource_level": False, "ingestion_partitioned": False}, date(2026, 9, 1), date(2026, 9, 30))
    assert "_PARTITIONTIME" not in sql and "CAST(NULL AS STRING) AS resource_global_name" in sql
    assert [p["parameterValue"]["value"] for p in params] == ["2026-09-01", "2026-09-30"]
    assert "private_key" not in repr(GcpClient(KEY))


# ---- API --------------------------------------------------------------------------------------------

class ApiAws:
    def __init__(self, cred, cfg):
        if cred["secret"] != SECRET:
            raise AwsError("InvalidClientTokenId: bad key")
        self.cfg = cfg

    def identity(self):
        return {"account_id": PAYER, "arn": f"arn:aws:iam::{PAYER}:user/cloudarc-reader"}

    def check(self, bucket, prefix):
        return FakeAws().check(bucket, prefix)


def test_connect_aws_and_gcp_through_the_api(client):
    h = client.hdr("admin")
    client.app.state.aws_factory = ApiAws
    body = {"access_key_id": KEY_ID, "secret_access_key": SECRET, "cur_bucket": "billing", "cur_prefix": "/cur/"}
    v = client.post("/api/tenants/demo-a/connect/aws/validate", json=body, headers=h)
    assert v.status_code == 200 and v.json()["account_id"] == PAYER
    assert client.post("/api/tenants/demo-a/connect/aws", json={**body, "secret_access_key": "x" * 40}, headers=h).status_code == 400
    assert client.post("/api/tenants/demo-a/connect/aws", json=body, headers=client.hdr("analyst")).status_code == 403
    done = client.post("/api/tenants/demo-a/connect/aws", json=body, headers=h)
    assert done.status_code == 201 and done.json()["aws_account"] == PAYER

    fake = FakeBigQuery()
    client.app.state.gcp_factory = lambda cred, cfg: gcp(fake)
    g = client.post("/api/tenants/demo-a/connect/gcp", json={"service_account_key": KEY, "table": TABLE}, headers=h)
    assert g.status_code == 201 and g.json()["billing_account"] == "0123AB-4567CD-89EF01"

    accounts = {a["external_id"]: a for a in client.get("/api/tenants/demo-a/accounts", headers=h).json()}
    assert json.loads(accounts[PAYER]["config"]) == {"cur_bucket": "billing", "cur_prefix": "cur", "external_id": None}
    assert json.loads(accounts["0123ab-4567cd-89ef01"]["config"])["table"] == TABLE
    creds = client.get("/api/tenants/demo-a/credentials", headers=h)
    assert SECRET not in creds.text and "PRIVATE KEY" not in creds.text
    assert {c["secret_hint"] for c in creds.json() if c["provider"] == "gcp"} == {"key ••••34ef"}

    # rotation is validated with the provider before it replaces the stored secret
    aws_cred = done.json()["credential_id"]
    assert client.post(f"/api/tenants/demo-a/credentials/{aws_cred}/rotate", json={"secret": "y" * 40}, headers=h).status_code == 400
    assert client.post(f"/api/tenants/demo-a/credentials/{aws_cred}/rotate", json={"secret": SECRET}, headers=h).status_code == 200
    other = json.loads(KEY) | {"client_email": "someone-else@proj-123.iam.gserviceaccount.com"}
    gcp_cred = g.json()["credential_id"]
    r = client.post(f"/api/tenants/demo-a/credentials/{gcp_cred}/rotate", json={"secret": json.dumps(other)}, headers=h)
    assert r.status_code == 400 and "someone-else" in r.text
