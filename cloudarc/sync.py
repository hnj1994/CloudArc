"""Sync orchestration, job queue with retry/back-off, and the in-process scheduler (FR-203, FR-204, FR-1101).

Each account sync pulls cost details for a look-back window (default 5 days,
or an initial backfill), replaces those days idempotently, refreshes the
inventory, utilization metrics, Advisor items and retail prices, then
re-evaluates recommendations, budgets and anomalies for the tenant.
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from . import alerts, audit, budgets
from .analytics.costs import anomalies_by
from .analytics.filters import Scope
from .config import get_settings
from .connectors.azure import DISK_METRICS, SQL_METRICS, VM_METRICS, AzureClient, AzureError
from .db import Database, new_id
from .ingest.loader import ingest_files
from .inventory import upsert_metrics, upsert_resources
from .recommendations import engine
from .tenants import get_account, load_credential
from .timeutil import add_months, fmt_inr, month_end, month_start

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
MAX_METRIC_RESOURCES = 200

ClientFactory = Callable[[dict], AzureClient]


def _default_factory(cred: dict) -> AzureClient:
    return AzureClient(cred["directory_id"], cred["client_id"], cred["secret"])


def _month_chunks(start: date, end: date):
    d = start
    while d <= end:
        chunk_end = min(month_end(d), end)
        yield d, chunk_end
        d = chunk_end + timedelta(days=1)


def sync_account(db: Database, tenant_id: str, account_id: str, factory: ClientFactory = _default_factory,
                 today: date | None = None) -> dict:
    s = get_settings()
    t0 = time.time()
    acct = get_account(db, tenant_id, account_id)
    if acct["provider"] != "azure":
        raise ValueError("API sync is implemented for Azure; load AWS/GCP exports via file ingestion")
    if not acct["credential_id"]:
        raise ValueError("account has no credential; upload billing files or attach a credential")
    client = factory(load_credential(db, tenant_id, acct["credential_id"]))
    sub = acct["external_id"]
    today = today or date.today()
    has_data = db.scalar("SELECT count(*) FROM cost_records WHERE tenant_id = ? AND account_id = ?", [tenant_id, account_id])
    start = today - timedelta(days=s.sync_lookback_days) if has_data else add_months(month_start(today), -s.initial_backfill_months)
    result: dict = {"window": [start.isoformat(), today.isoformat()], "cost_rows": 0}

    perm = client.check_permissions(sub)
    db.execute("UPDATE cloud_accounts SET permission_status = ?, permission_detail = ? WHERE id = ?",
               [perm["status"], json.dumps(perm), account_id])
    result["permissions"] = perm["status"]

    with tempfile.TemporaryDirectory(prefix="cloudarc-sync-") as tmp:
        for d0, d1 in _month_chunks(start, today):
            blobs = client.cost_details(sub, d0, d1)
            paths = []
            for i, blob in enumerate(blobs):
                p = Path(tmp) / f"{sub}-{d0}-{i}.csv"
                p.write_bytes(blob)
                paths.append(p)
            if paths:
                res = ingest_files(db, tenant_id, paths, provider="azure", source="azure-api")
                result["cost_rows"] += res.rows_loaded
                if not res.reconciled:
                    raise RuntimeError(f"reconciliation mismatch: source {res.source_total} vs loaded {res.loaded_total}")

    inv = client.inventory(sub)
    result["inventory"] = upsert_resources(db, tenant_id, account_id, inv)

    # Metrics and Advisor enrich recommendations but are not required for cost data: a subscription without
    # the Microsoft.Insights / Microsoft.Advisor providers registered still syncs, with a warning.
    warnings: list[str] = []
    metrics, n = [], 0
    for r in inv:
        rtype = (r.get("type") or "").lower()
        wanted = (VM_METRICS if rtype == "microsoft.compute/virtualmachines" else SQL_METRICS
                  if rtype == "microsoft.sql/servers/databases" else DISK_METRICS if rtype == "microsoft.compute/disks" else None)
        if wanted and n < MAX_METRIC_RESOURCES:
            n += 1
            try:
                metrics += client.daily_metrics(r["id"], wanted)
            except AzureError as exc:
                warnings.append(f"utilization metrics unavailable: {_short(exc)}")
                break  # same cause for every resource (e.g. Microsoft.Insights not registered)
    result["metric_points"] = upsert_metrics(db, tenant_id, metrics)

    for r in inv:
        if (r.get("type") or "").lower() == "microsoft.compute/virtualmachines":
            size = ((r.get("properties") or {}).get("hardwareProfile") or {}).get("vmSize")
            region = (r.get("location") or "").lower()
            if size and region:
                try:
                    for pricing, price in client.retail_prices(size, region, s.base_currency).items():
                        db.execute(
                            "INSERT INTO price_catalog (provider, sku, region, pricing, currency, hourly_price) VALUES ('azure', ?, ?, ?, ?, ?) "
                            "ON CONFLICT (provider, sku, region, pricing, currency) DO UPDATE SET hourly_price = excluded.hourly_price, fetched_at = now()",
                            [size, region, pricing, s.base_currency, price],
                        )
                except Exception as exc:  # noqa: BLE001 - prices are an enhancement, not a sync failure
                    log.warning("retail price lookup failed for %s: %s", size, type(exc).__name__)

    try:
        result["advisor"] = engine.merge_advisor(db, tenant_id, account_id, client.advisor_cost(sub))
    except AzureError as exc:
        warnings.append(f"Azure Advisor unavailable: {_short(exc)}")
    duration = time.time() - t0
    db.execute(
        "UPDATE cloud_accounts SET last_sync_at = now(), last_sync_status = ?, last_sync_duration_s = ?, last_error = ? WHERE id = ?",
        ["partial" if warnings else "succeeded", duration, "; ".join(warnings) or None, account_id],
    )
    result["duration_s"] = round(duration, 1)
    result["warnings"] = warnings
    return result


def _short(exc: Exception) -> str:
    text = str(exc)
    for code in ("MissingSubscriptionRegistration", "AuthorizationFailed", "SubscriptionNotRegistered"):
        if code in text:
            return f"{code} (register the resource provider or grant Reader, then re-sync)"
    return text[:200]


def post_sync(db: Database, tenant_id: str, as_of: date | None = None, notify: bool = True) -> dict:
    """Recommendations, budget evaluation and anomaly alerts after new data lands (BR-03: within 24 h)."""
    as_of = as_of or db.scalar("SELECT max(charge_date) FROM cost_records WHERE tenant_id = ?", [tenant_id]) or date.today()
    recs = engine.run(db, tenant_id, as_of)
    raised = budgets.evaluate(db, tenant_id, as_of, notify=False)
    tenant = db.one("SELECT name FROM tenants WHERE id = ?", [tenant_id]) or {"name": tenant_id}
    for dim in ("account", "service"):
        for a in anomalies_by(db, Scope(tenant_id), dim, as_of):
            if date.fromisoformat(a["date"]) < as_of - timedelta(days=3) or a["direction"] != "spike":
                continue
            item = alerts.raise_alert(
                db, tenant_id, kind="anomaly", severity="warning",
                message=f"{tenant['name']} · cost anomaly in {dim} '{a['value']}' on {a['date']}: {fmt_inr(a['cost'])} "
                        f"vs baseline {fmt_inr(a['baseline'])} (+{a['deviation_pct']:.0f}%)",
                details=a, dedupe_key=f"anomaly:{dim}:{a['value']}:{a['date']}",
            )
            if item:
                raised.append(item)
    if notify and raised:
        alerts.dispatch(db, tenant_id, raised)
    return {"recommendations": recs, "alerts_raised": len(raised)}


# ---- job queue ------------------------------------------------------------------------------------

def enqueue(db: Database, tenant_id: str, account_id: str, requested_by: str | None = None) -> str:
    pending = db.scalar(
        "SELECT id FROM sync_jobs WHERE tenant_id = ? AND account_id = ? AND status IN ('queued', 'running')", [tenant_id, account_id]
    )
    if pending:
        return pending
    jid = new_id()
    db.execute("INSERT INTO sync_jobs (id, tenant_id, account_id, status, requested_by) VALUES (?, ?, ?, 'queued', ?)",
               [jid, tenant_id, account_id, requested_by])
    return jid


def run_due_jobs(db: Database, factory: ClientFactory = _default_factory, now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    jobs = db.query("SELECT id, tenant_id, account_id, attempts FROM sync_jobs WHERE status = 'queued' AND next_run_at <= ? "
                    "ORDER BY next_run_at", [now])
    tenants_done = set()
    for j in jobs:
        db.execute("UPDATE sync_jobs SET status = 'running', attempts = attempts + 1 WHERE id = ?", [j["id"]])
        try:
            res = sync_account(db, j["tenant_id"], j["account_id"], factory)
            db.execute("UPDATE sync_jobs SET status = 'succeeded', finished_at = now(), last_error = NULL WHERE id = ?", [j["id"]])
            audit.record(db, "sync.succeeded", tenant_id=j["tenant_id"], target=j["account_id"], detail=res)
            tenants_done.add(j["tenant_id"])
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {str(exc)[:500]}"
            attempts = j["attempts"] + 1
            db.execute("UPDATE cloud_accounts SET last_sync_status = 'failed', last_error = ? WHERE id = ?", [err, j["account_id"]])
            if attempts < MAX_ATTEMPTS:
                backoff = timedelta(minutes=5 * 2 ** (attempts - 1))
                db.execute("UPDATE sync_jobs SET status = 'queued', next_run_at = ?, last_error = ? WHERE id = ?",
                           [now + backoff, err, j["id"]])
                log.warning("sync %s failed (attempt %s); retrying in %s", j["account_id"], attempts, backoff)
            else:
                db.execute("UPDATE sync_jobs SET status = 'failed', finished_at = now(), last_error = ? WHERE id = ?", [err, j["id"]])
                item = alerts.raise_alert(db, j["tenant_id"], kind="sync_failure", severity="critical",
                                          message=f"Sync failed after {attempts} attempts: {err}", details={"account_id": j["account_id"]},
                                          dedupe_key=f"sync:{j['id']}")
                if item:
                    alerts.dispatch(db, j["tenant_id"], [item])
    for tid in tenants_done:
        try:
            post_sync(db, tid)
        except Exception:  # noqa: BLE001
            log.exception("post-sync processing failed for tenant %s", tid)
    return len(jobs)


def enqueue_daily(db: Database) -> int:
    rows = db.query("SELECT tenant_id, id FROM cloud_accounts WHERE enabled AND credential_id IS NOT NULL")
    for r in rows:
        enqueue(db, r["tenant_id"], r["id"], requested_by="scheduler")
    return len(rows)


def apply_retention(db: Database, months: int | None = None) -> None:
    cutoff = add_months(month_start(date.today()), -(months or get_settings().retention_months))
    db.execute("DELETE FROM cost_records WHERE charge_date < ?", [cutoff])
    db.execute("DELETE FROM resource_metrics WHERE day < ?", [cutoff])


class Scheduler(threading.Thread):
    """Daily sync at CLOUDARC_SYNC_HOUR_UTC plus on-demand jobs, polled every ``interval`` seconds."""

    def __init__(self, db: Database, interval: float = 30.0):
        super().__init__(daemon=True, name="cloudarc-scheduler")
        self.db, self.interval = db, interval
        self._stop = threading.Event()
        self._last_daily: date | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        hour = get_settings().sync_hour_utc
        while not self._stop.is_set():
            try:
                now = datetime.utcnow()
                if now.hour == hour and self._last_daily != now.date():
                    self._last_daily = now.date()
                    enqueue_daily(self.db)
                    apply_retention(self.db)
                run_due_jobs(self.db)
            except Exception:  # noqa: BLE001
                log.exception("scheduler iteration failed")
            self._stop.wait(self.interval)
