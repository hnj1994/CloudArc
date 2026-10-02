"""DuckDB storage: schema and connection management.

DuckDB is an embedded columnar engine: it scans hundreds of millions of billing
rows on a single node and reads CSV / Parquet exports natively, which is why the
ingestion pipeline normalizes billing files in SQL rather than row by row.

Every tenant-owned table carries ``tenant_id`` and every query in the
application filters on it (NFR-02). Only one process may open the database file
for writing, so the scheduler runs inside the API process.
"""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import duckdb

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    currency VARCHAR NOT NULL DEFAULT 'INR',
    fiscal_year_start INTEGER NOT NULL DEFAULT 4,
    required_tags VARCHAR NOT NULL DEFAULT '[]',
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS credentials (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    directory_id VARCHAR,
    client_id VARCHAR,
    secret_ciphertext VARCHAR NOT NULL,
    secret_hint VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    rotated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cloud_accounts (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    external_id VARCHAR NOT NULL,
    name VARCHAR,
    credential_id VARCHAR,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    permission_status VARCHAR,
    permission_detail VARCHAR,
    last_sync_at TIMESTAMP,
    last_sync_status VARCHAR,
    last_sync_duration_s DOUBLE,
    last_error VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider, external_id)
);

CREATE TABLE IF NOT EXISTS cost_records (
    tenant_id VARCHAR NOT NULL,
    account_id VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    charge_date DATE NOT NULL,
    resource_id VARCHAR,
    resource_name VARCHAR,
    resource_group VARCHAR,
    resource_type VARCHAR,
    service_name VARCHAR,
    meter_category VARCHAR,
    meter_subcategory VARCHAR,
    meter_name VARCHAR,
    location VARCHAR,
    quantity DOUBLE,
    unit VARCHAR,
    unit_price DOUBLE,
    cost DOUBLE NOT NULL,
    currency VARCHAR,
    cost_base DOUBLE NOT NULL,
    pricing_model VARCHAR,
    charge_type VARCHAR,
    tags JSON,
    ingestion_id VARCHAR
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    source VARCHAR,
    status VARCHAR NOT NULL,
    rows_loaded BIGINT,
    source_total DOUBLE,
    loaded_total DOUBLE,
    date_from DATE,
    date_to DATE,
    error VARCHAR,
    started_at TIMESTAMP NOT NULL DEFAULT now(),
    finished_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fx_rates (
    currency VARCHAR NOT NULL,
    rate_date DATE NOT NULL,
    rate_to_base DOUBLE NOT NULL,
    PRIMARY KEY (currency, rate_date)
);

CREATE TABLE IF NOT EXISTS resources (
    tenant_id VARCHAR NOT NULL,
    account_id VARCHAR NOT NULL,
    resource_id VARCHAR NOT NULL,
    name VARCHAR,
    type VARCHAR,
    resource_group VARCHAR,
    location VARCHAR,
    sku VARCHAR,
    tags JSON,
    properties JSON,
    created_time TIMESTAMP,
    first_seen TIMESTAMP NOT NULL DEFAULT now(),
    last_seen TIMESTAMP NOT NULL DEFAULT now(),
    removed_at TIMESTAMP,
    PRIMARY KEY (tenant_id, resource_id)
);

CREATE TABLE IF NOT EXISTS resource_changes (
    tenant_id VARCHAR NOT NULL,
    resource_id VARCHAR NOT NULL,
    change_type VARCHAR NOT NULL,
    detail VARCHAR,
    changed_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resource_metrics (
    tenant_id VARCHAR NOT NULL,
    resource_id VARCHAR NOT NULL,
    metric VARCHAR NOT NULL,
    day DATE NOT NULL,
    avg DOUBLE,
    max DOUBLE,
    min DOUBLE,
    PRIMARY KEY (tenant_id, resource_id, metric, day)
);

CREATE TABLE IF NOT EXISTS price_catalog (
    provider VARCHAR NOT NULL,
    sku VARCHAR NOT NULL,
    region VARCHAR NOT NULL,
    pricing VARCHAR NOT NULL,
    currency VARCHAR NOT NULL,
    hourly_price DOUBLE NOT NULL,
    fetched_at TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, sku, region, pricing, currency)
);

CREATE TABLE IF NOT EXISTS cost_centers (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    rules JSON NOT NULL,
    percent DOUBLE NOT NULL DEFAULT 100,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS budgets (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    scope_type VARCHAR NOT NULL,
    scope_value VARCHAR,
    period VARCHAR NOT NULL,
    amount DOUBLE NOT NULL,
    thresholds JSON NOT NULL,
    forecast_alert BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    details JSON,
    budget_id VARCHAR,
    dedupe_key VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    notified_at TIMESTAMP,
    acknowledged_by VARCHAR,
    acknowledged_at TIMESTAMP,
    UNIQUE (tenant_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS alert_channels (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    target_ciphertext VARCHAR NOT NULL,
    target_hint VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recommendations (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    account_id VARCHAR,
    dedupe_key VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    horizon VARCHAR NOT NULL,
    resource_id VARCHAR,
    resource_name VARCHAR,
    title VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    evidence JSON NOT NULL,
    est_monthly_saving DOUBLE NOT NULL DEFAULT 0,
    confidence VARCHAR NOT NULL,
    effort VARCHAR NOT NULL,
    risk VARCHAR NOT NULL,
    source VARCHAR NOT NULL DEFAULT 'native',
    status VARCHAR NOT NULL DEFAULT 'open',
    status_reason VARCHAR,
    implemented_at TIMESTAMP,
    realized_monthly_saving DOUBLE,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS boq_items (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    boq_name VARCHAR NOT NULL,
    service_category VARCHAR,
    service_type VARCHAR,
    custom_name VARCHAR,
    region VARCHAR,
    description VARCHAR,
    monthly_cost DOUBLE NOT NULL,
    upfront_cost DOUBLE,
    imported_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id VARCHAR PRIMARY KEY,
    email VARCHAR NOT NULL UNIQUE,
    name VARCHAR,
    is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    label VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_tenants (
    user_id VARCHAR NOT NULL,
    tenant_id VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    PRIMARY KEY (user_id, tenant_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id VARCHAR PRIMARY KEY,
    ts TIMESTAMP NOT NULL DEFAULT now(),
    user_id VARCHAR,
    tenant_id VARCHAR,
    action VARCHAR NOT NULL,
    target VARCHAR,
    detail JSON,
    ip VARCHAR
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    account_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_run_at TIMESTAMP NOT NULL DEFAULT now(),
    last_error VARCHAR,
    requested_by VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    finished_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS report_runs (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    month VARCHAR NOT NULL,
    format VARCHAR NOT NULL,
    path VARCHAR NOT NULL,
    created_by VARCHAR,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- Additive migrations for databases created by earlier versions.
-- Non-secret connector settings (AWS CUR bucket/prefix, external ID; GCP export table). Secrets stay in credentials.
ALTER TABLE cloud_accounts ADD COLUMN IF NOT EXISTS config JSON;
"""


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    """Process-wide DuckDB handle.

    Reads use short-lived cursors (thread-safe); writes are serialized with a lock
    so that multi-statement changes (e.g. delete-then-insert of a restated cost
    window) run as one transaction without write-write conflicts.
    """

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._conn = _connect(path)
        self._write_lock = threading.RLock()
        self._conn.execute(SCHEMA)
        if path != ":memory:":
            # Fold schema changes (and everything before them) into the database file now, so a restart
            # never has to replay an ALTER from the write-ahead log (see _connect).
            self._conn.execute("CHECKPOINT")

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._conn.cursor()

    def query(self, sql: str, params: list | tuple | None = None) -> list[dict[str, Any]]:
        cur = self.cursor()
        try:
            rel = cur.execute(sql, params or [])
            cols = [d[0] for d in rel.description] if rel.description else []
            return [dict(zip(cols, row)) for row in rel.fetchall()]
        finally:
            cur.close()

    def one(self, sql: str, params: list | tuple | None = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: list | tuple | None = None) -> Any:
        cur = self.cursor()
        try:
            row = cur.execute(sql, params or []).fetchone()
            return row[0] if row else None
        finally:
            cur.close()

    def execute(self, sql: str, params: list | tuple | None = None) -> None:
        with self._write_lock:
            cur = self.cursor()
            try:
                cur.execute(sql, params or [])
            finally:
                cur.close()

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._write_lock:
            cur = self.cursor()
            cur.execute("BEGIN TRANSACTION")
            try:
                yield cur
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            finally:
                cur.close()

    def checkpoint(self) -> None:
        """Write the WAL into the database file; cheap, and keeps restarts from depending on WAL replay."""
        if self.path != ":memory:":
            with self._write_lock:
                self._conn.execute("CHECKPOINT")

    def close(self) -> None:
        self._conn.close()


def _connect(path: str) -> duckdb.DuckDBPyConnection:
    """Open the database, recovering from a WAL that DuckDB cannot replay when opening the file directly.

    DuckDB 1.5 fails to replay an ``ALTER TABLE … ADD COLUMN`` on a table with ``DEFAULT now()`` columns
    when the file is the main database ("no default database set"). Replaying it while the file is
    attached to an in-memory database works; checkpointing there folds the WAL into the file.
    """
    try:
        return duckdb.connect(path)
    except duckdb.InternalException as exc:
        if path == ":memory:" or "replaying WAL" not in str(exc):
            raise
        log.warning("WAL replay failed on open; replaying via ATTACH and checkpointing: %s", str(exc)[:200])
        recovery = duckdb.connect(":memory:")
        try:
            recovery.execute("ATTACH '" + path.replace("'", "''") + "' AS recovered")
            recovery.execute("CHECKPOINT recovered")
            recovery.execute("DETACH recovered")
        finally:
            recovery.close()
        return duckdb.connect(path)


_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                from .config import get_settings

                _db = Database(get_settings().db_path)
    return _db


def set_db(db: Database | None) -> None:
    """Swap the process database (used by tests and the CLI)."""
    global _db
    _db = db
