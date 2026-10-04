"""Azure read-only connector.

Uses an Entra ID app registration (client credentials) holding Reader, Cost
Management Reader and Billing Reader. Every call is a GET, or a POST to a
read-only query endpoint (Cost Details report generation, Resource Graph);
the connector never modifies a client environment.

APIs: Cost Management Cost Details, Resource Graph, Azure Monitor metrics,
Advisor, Authorization permissions, and the public Retail Prices API.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta

import httpx

log = logging.getLogger(__name__)
ARM = "https://management.azure.com"
LOGIN = "https://login.microsoftonline.com"
RETAIL = "https://prices.azure.com/api/retail/prices"

REQUIRED_READ = {
    "Reader": "*/read",
    "Cost Management Reader": "Microsoft.CostManagement/*/read",
    "Billing Reader": "Microsoft.Billing/*/read",
}

VM_METRICS = ["Percentage CPU", "Available Memory Bytes", "CPU Credits Remaining"]
SQL_METRICS = ["dtu_consumption_percent", "cpu_percent"]
DISK_METRICS = ["Composite Disk Read Operations/sec", "Composite Disk Write Operations/sec"]


class AzureError(RuntimeError):
    pass


def _matches(pattern: str, action: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(action.lower(), pattern.lower())


def evaluate_permissions(permissions: list[dict]) -> dict:
    """Effective permissions (from Microsoft.Authorization/permissions) -> required-role coverage and write detection."""
    actions = [a for p in permissions for a in p.get("actions", [])]
    not_actions = [a for p in permissions for a in p.get("notActions", [])]

    def allowed(action: str) -> bool:
        return any(_matches(a, action) for a in actions) and not any(_matches(n, action) for n in not_actions)

    probes = {
        "Reader": "Microsoft.Compute/virtualMachines/read",
        "Cost Management Reader": "Microsoft.CostManagement/query/read",
        "Billing Reader": "Microsoft.Billing/billingPeriods/read",
    }
    coverage = {role: allowed(action) for role, action in probes.items()}
    write_probes = ["Microsoft.Compute/virtualMachines/write", "Microsoft.Compute/virtualMachines/delete",
                    "Microsoft.Resources/subscriptions/resourceGroups/write", "Microsoft.Authorization/roleAssignments/write"]
    writes = [w for w in write_probes if allowed(w)]
    missing = [r for r, ok in coverage.items() if not ok]
    status = "ok" if not missing and not writes else ("over_privileged" if writes else "missing_roles")
    return {"status": status, "roles": coverage, "missing": missing, "write_actions": writes}


class AzureClient:
    def __init__(self, directory_id: str, client_id: str, secret: str, http: httpx.Client | None = None,
                 poll_interval: float = 5.0, max_retries: int = 5, sleep=time.sleep):
        self.directory_id, self.client_id, self._secret = directory_id, client_id, secret
        self.http = http or httpx.Client(timeout=120)
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.max_retries = max_retries
        self._token: str | None = None
        self._token_exp = 0.0

    def __repr__(self) -> str:  # never leak the secret through reprs / logs
        return f"AzureClient(directory_id={self.directory_id!r}, client_id={self.client_id!r})"

    # ---- plumbing -----------------------------------------------------------------------------
    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        r = self.http.post(
            f"{LOGIN}/{self.directory_id}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self._secret,
                  "scope": f"{ARM}/.default"},
        )
        if r.status_code != 200:
            raise AzureError(f"token request failed ({r.status_code}): {r.json().get('error_description', '')[:200]}")
        body = r.json()
        self._token, self._token_exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(self.max_retries):
            r = self.http.request(method, url if url.startswith("http") else ARM + url,
                                  headers={"Authorization": f"Bearer {self.token()}"}, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = float(r.headers.get("Retry-After") or r.headers.get("x-ms-ratelimit-microsoft.costmanagement-entity-retry-after") or 2 ** attempt)
                log.info("azure throttled/unavailable (%s); retrying in %.0fs", r.status_code, wait)
                self.sleep(min(wait, 60))
                continue
            return r
        raise AzureError(f"{method} {url.split('?')[0]} failed after {self.max_retries} attempts")

    def _get_json(self, url: str, **kw) -> dict:
        r = self._request("GET", url, **kw)
        if r.status_code >= 400:
            raise AzureError(f"GET {url.split('?')[0]} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _paged(self, url: str) -> Iterator[dict]:
        while url:
            body = self._get_json(url)
            yield from body.get("value", [])
            url = body.get("nextLink")

    # ---- discovery & permissions ----------------------------------------------------------------
    def list_subscriptions(self) -> list[dict]:
        return [{"subscription_id": s["subscriptionId"], "name": s.get("displayName"), "state": s.get("state")}
                for s in self._paged("/subscriptions?api-version=2022-12-01")]

    def check_permissions(self, subscription_id: str) -> dict:
        perms = list(self._paged(f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01"))
        return evaluate_permissions(perms)

    # ---- cost details ---------------------------------------------------------------------------
    def cost_details(self, subscription_id: str, start: date, end: date, metric: str = "ActualCost") -> list[bytes]:
        """Generate a Cost Details report (≤ 1 month per request) and download its CSV blob(s)."""
        url = f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/generateCostDetailsReport?api-version=2023-11-01"
        r = self._request("POST", url, json={"metric": metric, "timePeriod": {"start": start.isoformat(), "end": end.isoformat()}})
        if r.status_code == 204:
            return []
        if r.status_code not in (200, 202):
            raise AzureError(f"cost details request failed ({r.status_code}): {r.text[:300]}")
        def manifest(b: dict) -> dict | None:
            return b.get("manifest") or (b.get("properties") or {}).get("manifest")

        def status(b: dict) -> str:
            return b.get("status") or (b.get("properties") or {}).get("status") or ""

        body = r.json() if r.status_code == 200 and r.content else None
        location = r.headers.get("Location") or r.headers.get("location")
        deadline = time.time() + 1800
        while body is None or (not manifest(body) and status(body) not in ("Completed", "Failed")):
            if not location or time.time() > deadline:
                raise AzureError("cost details report did not complete")
            self.sleep(float(r.headers.get("Retry-After", self.poll_interval)))
            r = self._request("GET", location)
            if r.status_code == 202:
                continue
            if r.status_code != 200:
                raise AzureError(f"cost details poll failed ({r.status_code})")
            body = r.json()
        if status(body) == "Failed":
            raise AzureError(f"cost details report failed: {body.get('error')}")
        blobs = (manifest(body) or {}).get("blobs", [])
        out = []
        for b in blobs:
            resp = self.http.get(b["blobLink"])  # SAS URL, no bearer token
            resp.raise_for_status()
            out.append(resp.content)
        return out

    # ---- inventory ------------------------------------------------------------------------------
    def resource_graph(self, subscription_id: str, query: str) -> list[dict]:
        rows, skip = [], None
        while True:
            body = {"subscriptions": [subscription_id], "query": query, "options": {"$top": 1000, "resultFormat": "objectArray"}}
            if skip:
                body["options"]["$skipToken"] = skip
            r = self._request("POST", "/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01", json=body)
            if r.status_code != 200:
                raise AzureError(f"resource graph failed ({r.status_code}): {r.text[:300]}")
            data = r.json()
            rows.extend(data.get("data", []))
            skip = data.get("$skipToken")
            if not skip:
                return rows

    def inventory(self, subscription_id: str) -> list[dict]:
        resources = self.resource_graph(
            subscription_id,
            "Resources | project id, name, type, resourceGroup, location, sku, tags, properties, kind, managedBy",
        )
        groups = self.resource_graph(
            subscription_id,
            "ResourceContainers | where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
            "| project id, name, type, resourceGroup=name, location, tags, properties",
        )
        return resources + groups

    # ---- metrics ----------------------------------------------------------------------------
    def daily_metrics(self, resource_id: str, metrics: list[str], days: int = 30) -> list[dict]:
        end = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        params = {
            "api-version": "2023-10-01", "metricnames": ",".join(metrics), "interval": "P1D",
            "aggregation": "Average,Maximum,Minimum", "timespan": f"{start.isoformat()}Z/{end.isoformat()}Z",
        }
        r = self._request("GET", f"{resource_id}/providers/Microsoft.Insights/metrics", params=params)
        if r.status_code == 400:  # metric not supported on this SKU (e.g. credits on non-B VMs)
            return []
        if r.status_code >= 400:
            raise AzureError(f"metrics failed ({r.status_code}) for {resource_id}: {r.text[:300]}")
        out = []
        for m in r.json().get("value", []):
            name = m["name"]["value"]
            for ts in m.get("timeseries", []):
                for pt in ts.get("data", []):
                    if pt.get("average") is None and pt.get("maximum") is None:
                        continue
                    out.append({"resource_id": resource_id, "metric": name, "day": pt["timeStamp"][:10],
                                "avg": pt.get("average"), "max": pt.get("maximum"), "min": pt.get("minimum")})
        return out

    # ---- advisor & prices -------------------------------------------------------------------
    def advisor_cost(self, subscription_id: str) -> list[dict]:
        return list(self._paged(
            f"/subscriptions/{subscription_id}/providers/Microsoft.Advisor/recommendations?api-version=2023-01-01"
            "&$filter=Category eq 'Cost'"
        ))

    def retail_prices(self, arm_sku: str, region: str, currency: str = "INR") -> dict[str, float]:
        """Hourly pay-as-you-go and 1y/3y reservation prices (public API, no auth)."""
        flt = (f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' and armSkuName eq '{arm_sku}' "
               "and priceType ne 'DevTestConsumption'")
        url, prices = f"{RETAIL}?currencyCode='{currency}'&$filter={flt}", {}
        while url:
            r = self.http.get(url)
            r.raise_for_status()
            body = r.json()
            for it in body.get("Items", []):
                name = it.get("meterName", "")
                if "Low Priority" in name or "Spot" in name or "Windows" in it.get("productName", ""):
                    continue
                if it["type"] == "Consumption":
                    prices["payg"] = it["retailPrice"]
                elif it["type"] == "Reservation":
                    hours = 8760 if it.get("reservationTerm") == "1 Year" else 26280
                    prices["reservation_1y" if hours == 8760 else "reservation_3y"] = it["retailPrice"] / hours
            url = body.get("NextPageLink")
        return prices
