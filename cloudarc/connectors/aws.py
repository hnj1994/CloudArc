"""AWS read-only connector.

Two sources, both read-only:

* **Cost Explorer API** — daily amortized cost by linked account and service. Needs nothing but IAM
  permissions, so a new connection backfills ~12 months immediately. No resource-level detail.
* **CUR 2.0 (Data Exports) files in S3** — line items with resource IDs and tags. When configured,
  a billing period with CUR files replaces the Cost Explorer summary for that period.

Authenticates with an access key of a dedicated read-only IAM user, optionally assuming a role
(with an external ID) in the payer account. Every call is a read; Cost Explorer requests are billed
by AWS at $0.01 each, so the connector keeps them to a handful per sync.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import PurePosixPath

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)

CE_REGION = "us-east-1"  # Cost Explorer has a single endpoint
REQUIRED_ACTIONS = {
    "Cost Explorer": "ce:GetCostAndUsage, ce:GetDimensionValues",
    "CUR files": "s3:ListBucket, s3:GetObject on the export bucket (only if a CUR bucket is set)",
}
CUR_DATA_SUFFIXES = (".parquet", ".csv.gz", ".csv")
ROLE_ARN = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+")

# CUR 2.0 column names the AWS adapter reads; Cost Explorer rows are written in this shape.
CE_COLUMNS = ["line_item_usage_account_id", "line_item_usage_account_name", "line_item_usage_start_date",
              "line_item_product_code", "product_product_name", "line_item_line_item_type",
              "line_item_unblended_cost", "line_item_currency_code"]


class AwsError(RuntimeError):
    pass


def _err(exc: Exception) -> AwsError:
    if isinstance(exc, ClientError):
        e = exc.response.get("Error", {})
        return AwsError(f"{e.get('Code', 'Error')}: {e.get('Message', '')[:240]}")
    return AwsError(f"{type(exc).__name__}: {str(exc)[:240]}")


class AwsClient:
    def __init__(self, access_key_id: str, secret_access_key: str, role_arn: str | None = None,
                 external_id: str | None = None, session_factory=boto3.session.Session):
        if role_arn and not ROLE_ARN.fullmatch(role_arn):
            raise AwsError("role ARN must look like arn:aws:iam::123456789012:role/name")
        self.access_key_id, self._secret = access_key_id, secret_access_key
        self.role_arn, self.external_id = role_arn or None, external_id or None
        self._session_factory = session_factory
        self._session = None
        self._config = Config(retries={"mode": "adaptive", "max_attempts": 8}, read_timeout=120, connect_timeout=20)

    def __repr__(self) -> str:  # never leak the secret through reprs / logs
        return f"AwsClient(access_key_id={self.access_key_id!r}, role_arn={self.role_arn!r})"

    # ---- plumbing -----------------------------------------------------------------------------
    def session(self):
        if self._session is None:
            base = self._session_factory(aws_access_key_id=self.access_key_id, aws_secret_access_key=self._secret)
            if not self.role_arn:
                self._session = base
            else:
                kw = {"RoleArn": self.role_arn, "RoleSessionName": "cloudarc-sync", "DurationSeconds": 3600}
                if self.external_id:
                    kw["ExternalId"] = self.external_id
                try:
                    c = base.client("sts", config=self._config).assume_role(**kw)["Credentials"]
                except (ClientError, BotoCoreError) as exc:
                    raise _err(exc) from exc
                self._session = self._session_factory(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
                                                      aws_session_token=c["SessionToken"])
        return self._session

    def _client(self, service: str, region: str | None = None):
        return self.session().client(service, region_name=region or CE_REGION, config=self._config)

    def _call(self, service: str, op: str, region: str | None = None, **kw):
        try:
            return getattr(self._client(service, region), op)(**kw)
        except (ClientError, BotoCoreError) as exc:
            raise _err(exc) from exc

    # ---- identity & permissions ------------------------------------------------------------------
    def identity(self) -> dict:
        r = self._call("sts", "get_caller_identity")
        return {"account_id": r["Account"], "arn": r["Arn"]}

    def check(self, cur_bucket: str | None = None, cur_prefix: str | None = None) -> dict:
        """Probe each required permission with the cheapest read (one Cost Explorer request, $0.01)."""
        checks: dict[str, str] = {}
        y = date.today() - timedelta(days=1)
        try:
            self._call("ce", "get_cost_and_usage", TimePeriod={"Start": (y - timedelta(days=1)).isoformat(), "End": y.isoformat()},
                       Granularity="DAILY", Metrics=["AmortizedCost"])
            checks["Cost Explorer"] = "ok"
        except AwsError as exc:
            checks["Cost Explorer"] = str(exc)
        if cur_bucket:
            try:
                self._call("s3", "list_objects_v2", Bucket=cur_bucket, Prefix=_dir(cur_prefix), MaxKeys=1)
                checks["CUR files"] = "ok"
            except AwsError as exc:
                checks["CUR files"] = str(exc)
        missing = [k for k, v in checks.items() if v != "ok"]
        return {"status": "ok" if not missing else "missing_roles", "checks": checks, "missing": missing,
                "required": {k: REQUIRED_ACTIONS[k] for k in checks}}

    # ---- Cost Explorer ----------------------------------------------------------------------------
    def _paged_ce(self, op: str, key: str, **kw):
        token = None
        while True:
            r = self._call("ce", op, **kw, **({"NextPageToken": token} if token else {}))
            yield from r.get(key, [])
            token = r.get("NextPageToken")
            if not token:
                return

    def account_names(self, start: date, end: date) -> dict[str, str]:
        out = {}
        for v in self._paged_ce("get_dimension_values", "DimensionValues", Dimension="LINKED_ACCOUNT",
                                TimePeriod={"Start": start.isoformat(), "End": (end + timedelta(days=1)).isoformat()}):
            out[v["Value"]] = (v.get("Attributes") or {}).get("description") or v["Value"]
        return out

    def daily_cost(self, start: date, end: date) -> list[dict]:
        """Amortized cost per day x linked account x service, ``end`` inclusive. Zero rows are dropped."""
        names = self.account_names(start, end)
        rows = []
        for day in self._paged_ce("get_cost_and_usage", "ResultsByTime",
                                  TimePeriod={"Start": start.isoformat(), "End": (end + timedelta(days=1)).isoformat()},
                                  Granularity="DAILY", Metrics=["AmortizedCost"],
                                  GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}, {"Type": "DIMENSION", "Key": "SERVICE"}]):
            for g in day.get("Groups", []):
                account, service = g["Keys"]
                m = g["Metrics"]["AmortizedCost"]
                amount = float(m["Amount"])
                if amount == 0:
                    continue
                rows.append({
                    "line_item_usage_account_id": account, "line_item_usage_account_name": names.get(account, account),
                    "line_item_usage_start_date": day["TimePeriod"]["Start"], "line_item_product_code": service,
                    "product_product_name": service, "line_item_line_item_type": "Usage",
                    "line_item_unblended_cost": repr(amount), "line_item_currency_code": m.get("Unit") or "USD",
                })
        return rows

    def total_cost(self, start: date, end: date) -> float:
        """Ungrouped amortized total for the same window: an independent check on the grouped pages."""
        total = 0.0
        for period in self._paged_ce("get_cost_and_usage", "ResultsByTime",
                                     TimePeriod={"Start": start.isoformat(), "End": (end + timedelta(days=1)).isoformat()},
                                     Granularity="MONTHLY", Metrics=["AmortizedCost"]):
            total += float(period["Total"]["AmortizedCost"]["Amount"])
        return total

    # ---- CUR 2.0 files ----------------------------------------------------------------------------
    def cur_files(self, bucket: str, prefix: str | None, period: str) -> list[str]:
        """Data files for one billing period (``YYYY-MM``) of a CUR 2.0 export.

        Files live under ``…/data/BILLING_PERIOD=YYYY-MM/``. If the export keeps several deliveries
        (sub-folders per delivery), only the most recently written folder is used, so a period is
        never loaded twice.
        """
        marker = f"BILLING_PERIOD={period}/"
        folders: dict[str, list[tuple[str, object]]] = {}
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": _dir(prefix)}
            if token:
                kw["ContinuationToken"] = token
            r = self._call("s3", "list_objects_v2", **kw)
            for o in r.get("Contents", []):
                key = o["Key"]
                if marker in key and "/data/" in f"/{key}" and key.lower().endswith(CUR_DATA_SUFFIXES):
                    folders.setdefault(str(PurePosixPath(key).parent), []).append((key, o["LastModified"]))
            token = r.get("NextContinuationToken")
            if not r.get("IsTruncated") or not token:
                break
        if not folders:
            return []
        latest = max(folders.values(), key=lambda files: max(m for _, m in files))
        return sorted(k for k, _ in latest)

    def download(self, bucket: str, key: str, dest: str) -> None:
        try:
            self._client("s3").download_file(bucket, key, dest)
        except (ClientError, BotoCoreError) as exc:
            raise _err(exc) from exc


def _dir(prefix: str | None) -> str:
    p = (prefix or "").strip().strip("/")
    return f"{p}/" if p else ""
