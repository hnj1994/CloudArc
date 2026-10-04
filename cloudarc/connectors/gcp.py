"""GCP read-only connector: queries the Cloud Billing export in BigQuery.

Google has no API that returns billing cost; the BigQuery billing export (standard or detailed usage
cost) is the source of truth, and this connector reads it with the BigQuery REST API. It
authenticates as a service account (JSON key) holding BigQuery Data Viewer on the export dataset and
BigQuery Job User on the project the queries run in. Queries only read; a partition filter keeps the
bytes scanned (and so BigQuery cost) proportional to the sync window.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from datetime import date

import httpx
import jwt

BQ = "https://bigquery.googleapis.com/bigquery/v2"
SCOPE = "https://www.googleapis.com/auth/bigquery"  # access is still limited by the read-only IAM roles
TABLE = re.compile(r"(?P<project>[a-z][a-z0-9-]{4,29})\.(?P<dataset>\w{1,1024})\.(?P<table>[\w$-]{1,1024})")
BILLING_ID = re.compile(r"gcp_billing_export(?:_resource)?_v1_([0-9A-F]{6})_([0-9A-F]{6})_([0-9A-F]{6})$", re.I)
REQUIRED_ROLES = {
    "BigQuery Data Viewer": "on the billing export dataset (read the export table)",
    "BigQuery Job User": "on the project queries run in (run read-only queries)",
}
COLUMNS = ["billing_account_id", "service_description", "sku_description", "usage_start_time", "project_id", "project_name",
           "labels", "location_region", "cost", "currency", "usage_amount_in_pricing_units", "usage_pricing_unit",
           "credits", "cost_type", "resource_global_name"]


class GcpError(RuntimeError):
    pass


def parse_table(table: str) -> dict:
    m = TABLE.fullmatch((table or "").strip().strip("`"))
    if not m:
        raise GcpError("export table must be project.dataset.table, e.g. my-proj.billing_export.gcp_billing_export_v1_0123AB_4567CD_89EF01")
    return m.groupdict()


def billing_account_from_table(table: str) -> str | None:
    m = BILLING_ID.search(parse_table(table)["table"])
    return "-".join(g.upper() for g in m.groups()) if m else None


def parse_key(key_json: str) -> dict:
    try:
        key = json.loads(key_json)
    except (TypeError, ValueError) as exc:
        raise GcpError("service account key must be the JSON key file contents") from exc
    if key.get("type") != "service_account" or not key.get("private_key") or not key.get("client_email"):
        raise GcpError("not a service account key (expected type, client_email and private_key)")
    return key


class GcpClient:
    def __init__(self, key_json: str, http: httpx.Client | None = None, sleep=time.sleep, max_retries: int = 5):
        self._key = parse_key(key_json)
        self.client_email = self._key["client_email"]
        self.http = http or httpx.Client(timeout=120)
        self.sleep, self.max_retries = sleep, max_retries
        self._token, self._token_exp = None, 0.0

    def __repr__(self) -> str:  # never leak the key through reprs / logs
        return f"GcpClient(client_email={self.client_email!r})"

    # ---- plumbing -----------------------------------------------------------------------------
    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        uri = self._key.get("token_uri") or "https://oauth2.googleapis.com/token"
        now = int(time.time())
        assertion = jwt.encode({"iss": self.client_email, "scope": SCOPE, "aud": uri, "iat": now, "exp": now + 3600},
                               self._key["private_key"], algorithm="RS256", headers={"kid": self._key.get("private_key_id")})
        r = self.http.post(uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        if r.status_code != 200:
            raise GcpError(f"token request failed ({r.status_code}): {_message(r)}")
        body = r.json()
        self._token, self._token_exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _request(self, method: str, url: str, **kw) -> dict:
        for attempt in range(self.max_retries):
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {self.token()}"}, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                self.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code >= 400:
                raise GcpError(f"{method} {url.split('?')[0].replace(BQ, '')} -> {r.status_code}: {_message(r)}")
            return r.json()
        raise GcpError(f"{method} {url.split('?')[0].replace(BQ, '')} failed after {self.max_retries} attempts")

    # ---- metadata & permissions ------------------------------------------------------------------
    def table_info(self, table: str) -> dict:
        t = parse_table(table)
        body = self._request("GET", f"{BQ}/projects/{t['project']}/datasets/{t['dataset']}/tables/{t['table']}")
        fields = {f["name"] for f in body.get("schema", {}).get("fields", [])}
        missing = {"billing_account_id", "service", "sku", "usage_start_time", "project", "cost", "currency"} - fields
        if missing:
            raise GcpError(f"{table} is not a Cloud Billing export table (missing {sorted(missing)})")
        part = body.get("timePartitioning") or {}
        return {**t, "location": body.get("location"), "resource_level": "resource" in fields,
                "ingestion_partitioned": bool(part) and not part.get("field"), "rows": int(body.get("numRows", 0))}

    def check(self, table: str, job_project: str | None = None) -> dict:
        checks: dict[str, str] = {}
        info = None
        try:
            info = self.table_info(table)
            checks["BigQuery Data Viewer"] = "ok"
        except GcpError as exc:
            checks["BigQuery Data Viewer"] = str(exc)
        if info:
            try:
                today = date.today()
                sql, params = export_query(table, info, today.replace(day=1), today)
                self.dry_run(sql, params, info, job_project)
                checks["BigQuery Job User"] = "ok"
            except GcpError as exc:
                checks["BigQuery Job User"] = str(exc)
        missing = [k for k, v in checks.items() if v != "ok"]
        return {"status": "ok" if not missing else "missing_roles", "checks": checks, "missing": missing,
                "required": REQUIRED_ROLES, "table": info}

    # ---- queries --------------------------------------------------------------------------------
    def dry_run(self, sql: str, params: list, info: dict, job_project: str | None = None) -> int:
        body = {"configuration": {"dryRun": True, "query": {"query": sql, "useLegacySql": False, "parameterMode": "NAMED",
                                                             "queryParameters": params}},
                "jobReference": {"location": info["location"]} if info.get("location") else {}}
        r = self._request("POST", f"{BQ}/projects/{job_project or info['project']}/jobs", json=body)
        return int((r.get("statistics") or {}).get("totalBytesProcessed", 0))

    def query(self, sql: str, params: list, info: dict, job_project: str | None = None, page_rows: int = 20000) -> Iterator[dict]:
        """Run a query and yield rows as dicts; verifies the number of rows read equals the total."""
        project = job_project or info["project"]
        body = {"query": sql, "useLegacySql": False, "parameterMode": "NAMED", "queryParameters": params,
                "maxResults": page_rows, "timeoutMs": 60000}
        if info.get("location"):
            body["location"] = info["location"]
        r = self._request("POST", f"{BQ}/projects/{project}/queries", json=body)
        job = r.get("jobReference") or {}
        loc = {"location": job.get("location") or info.get("location")} if (job.get("location") or info.get("location")) else {}
        deadline = time.time() + 1800
        while not r.get("jobComplete"):
            if time.time() > deadline:
                raise GcpError("BigQuery query did not complete in 30 minutes")
            r = self._request("GET", f"{BQ}/projects/{project}/queries/{job['jobId']}",
                              params={"timeoutMs": 60000, "maxResults": page_rows, **loc})
        fields = [f["name"] for f in r.get("schema", {}).get("fields", [])]
        total, seen = int(r.get("totalRows", 0)), 0
        while True:
            for row in r.get("rows", []):
                seen += 1
                yield {name: cell.get("v") for name, cell in zip(fields, row["f"], strict=True)}
            token = r.get("pageToken")
            if not token:
                break
            r = self._request("GET", f"{BQ}/projects/{project}/queries/{job['jobId']}",
                              params={"pageToken": token, "maxResults": page_rows, **loc})
        if seen != total:
            raise GcpError(f"BigQuery returned {seen} of {total} rows")


def export_query(table: str, info: dict, start: date, end: date) -> tuple[str, list]:
    """Flatten the export into the layout the GCP adapter reads (same shape as deploy/gcp/billing-export-for-cloudarc.sql).

    Charges with no project (support, some taxes) are kept under the billing account id so totals match
    the Cloud Billing console. The table name is validated by ``parse_table`` before it is interpolated.
    """
    t = parse_table(table)
    resource = "resource.global_name" if info.get("resource_level") else "CAST(NULL AS STRING)"
    partition = " AND _PARTITIONTIME >= TIMESTAMP(@d0)" if info.get("ingestion_partitioned") else ""
    sql = f"""
SELECT billing_account_id,
  service.description AS service_description,
  sku.description AS sku_description,
  FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', usage_start_time) AS usage_start_time,
  COALESCE(project.id, LOWER(billing_account_id)) AS project_id,
  project.name AS project_name,  -- NULL for project-less charges, so the connection keeps its name
  TO_JSON_STRING(labels) AS labels,
  location.region AS location_region,
  cost, currency,
  usage.amount_in_pricing_units AS usage_amount_in_pricing_units,
  usage.pricing_unit AS usage_pricing_unit,
  TO_JSON_STRING(credits) AS credits,
  cost_type,
  {resource} AS resource_global_name
FROM `{t['project']}.{t['dataset']}.{t['table']}`
WHERE DATE(usage_start_time) BETWEEN @d0 AND @d1{partition}"""
    params = [{"name": "d0", "parameterType": {"type": "DATE"}, "parameterValue": {"value": start.isoformat()}},
              {"name": "d1", "parameterType": {"type": "DATE"}, "parameterValue": {"value": end.isoformat()}}]
    return sql, params


def _message(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error")
        msg = err.get("message") if isinstance(err, dict) else body.get("error_description") or err
        return str(msg or r.text)[:300]
    except ValueError:
        return r.text[:300]
