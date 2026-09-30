"""Idempotent loading of billing exports into ``cost_records``.

Providers restate recent days (Azure revises cost for up to ~72 h, AWS
finalizes CUR at month close). Each load therefore *replaces* every
(account, day) window present in the incoming data inside one transaction:
re-syncing the same period any number of times never duplicates cost
(FR-204, NFR-05), and a restated day overwrites the stale one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..config import get_settings
from ..db import Database, new_id
from .aws import AwsCurAdapter
from .azure import AzureAdapter
from .common import NORMALIZED_COLUMNS, BillingAdapter, Columns, sql_str
from .gcp import GcpBillingAdapter

log = logging.getLogger(__name__)

ADAPTERS: dict[str, BillingAdapter] = {
    a.provider: a for a in (AzureAdapter(), AwsCurAdapter(), GcpBillingAdapter())
}


class IngestError(ValueError):
    pass


@dataclass
class IngestResult:
    ingestion_id: str
    provider: str
    rows_loaded: int
    source_total: float
    loaded_total: float
    date_from: str | None
    date_to: str | None
    accounts: list[str]

    @property
    def reconciled(self) -> bool:
        return abs(self.source_total - self.loaded_total) <= max(0.005 * abs(self.source_total), 0.01)


def _reader(paths: Sequence[str]) -> str:
    listed = "[" + ", ".join(sql_str(str(p)) for p in paths) + "]"
    if all(str(p).lower().endswith(".parquet") for p in paths):
        return f"read_parquet({listed}, union_by_name=true)"
    return f"read_csv({listed}, header=true, all_varchar=true, union_by_name=true, sample_size=-1)"


def detect_provider(cols: Columns) -> str:
    for provider, adapter in ADAPTERS.items():
        if adapter.detect(cols):
            return provider
    raise IngestError("unrecognized billing file layout (expected Azure cost details, AWS CUR or GCP export)")


def ingest_files(
    db: Database,
    tenant_id: str,
    paths: Sequence[str | Path],
    provider: str | None = None,
    source: str = "upload",
) -> IngestResult:
    paths = [str(p) for p in paths]
    for p in paths:
        if not Path(p).exists():
            raise IngestError(f"file not found: {p}")
    base = get_settings().base_currency
    run_id = new_id()
    db.execute(
        "INSERT INTO ingestion_runs (id, tenant_id, provider, source, status) VALUES (?, ?, ?, ?, 'running')",
        [run_id, tenant_id, provider or "auto", source],
    )
    try:
        with db.transaction() as cur:
            cur.execute(f"CREATE OR REPLACE TEMP VIEW raw AS SELECT * FROM {_reader(paths)}")
            cols = Columns({r[0]: r[1] for r in cur.execute("DESCRIBE raw").fetchall()})
            provider = provider or detect_provider(cols)
            adapter = ADAPTERS[provider]
            where = adapter.row_filter(cols)

            cur.execute(
                f"CREATE OR REPLACE TEMP TABLE staged AS SELECT {adapter.select_list(cols)} FROM raw WHERE {where}"
            )
            source_total = cur.execute(
                f"SELECT COALESCE(SUM({adapter.source_cost_expr(cols)}), 0) FROM raw WHERE {where}"
            ).fetchone()[0]
            bad = cur.execute(
                "SELECT count(*) FROM staged WHERE charge_date IS NULL OR external_account_id IS NULL"
            ).fetchone()[0]
            if bad:
                raise IngestError(f"{bad} rows have no parsable date or account id")

            # Auto-register accounts seen in the data (FR-104 for file-based onboarding).
            for ext_id, name in cur.execute(
                "SELECT external_account_id, max(account_name) FROM staged GROUP BY 1"
            ).fetchall():
                cur.execute(
                    "INSERT INTO cloud_accounts (id, tenant_id, provider, external_id, name) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (tenant_id, provider, external_id) DO UPDATE SET name = COALESCE(excluded.name, name)",
                    [new_id(), tenant_id, provider, ext_id, name],
                )

            cur.execute(
                "CREATE OR REPLACE TEMP TABLE windows AS "
                "SELECT a.id AS account_id, s.external_account_id, min(s.charge_date) AS d0, max(s.charge_date) AS d1 "
                "FROM staged s JOIN cloud_accounts a ON a.tenant_id = ? AND a.provider = ? "
                "AND a.external_id = s.external_account_id GROUP BY 1, 2",
                [tenant_id, provider],
            )
            cur.execute(
                "DELETE FROM cost_records c USING windows w WHERE c.tenant_id = ? AND c.account_id = w.account_id "
                "AND c.charge_date BETWEEN w.d0 AND w.d1",
                [tenant_id],
            )

            defaults = get_settings().fx_defaults
            fx_default_rows = ", ".join(
                f"({sql_str(k.upper())}, {float(v)})" for k, v in defaults.items()
            ) or "(NULL, NULL)"
            select_cols = ", ".join(f"s.{c}" for c in NORMALIZED_COLUMNS if c not in {"external_account_id", "account_name"})
            cur.execute(
                f"""
                INSERT INTO cost_records (tenant_id, account_id, provider, {', '.join(c for c in NORMALIZED_COLUMNS if c not in {'external_account_id', 'account_name'})}, cost_base, ingestion_id)
                WITH d(currency, rate) AS (VALUES {fx_default_rows})
                SELECT ?, w.account_id, ?, {select_cols},
                       s.cost * CASE WHEN s.currency = ? THEN 1 ELSE COALESCE(f.rate_to_base, d.rate, 1) END,
                       ?
                FROM staged s
                JOIN windows w ON w.external_account_id = s.external_account_id
                ASOF LEFT JOIN fx_rates f ON f.currency = s.currency AND s.charge_date >= f.rate_date
                LEFT JOIN d ON d.currency = s.currency
                """,
                [tenant_id, provider, base, run_id],
            )
            rows, loaded_total, d0, d1 = cur.execute(
                "SELECT count(*), COALESCE(SUM(cost), 0), min(charge_date), max(charge_date) "
                "FROM cost_records WHERE tenant_id = ? AND ingestion_id = ?",
                [tenant_id, run_id],
            ).fetchone()
            accounts = [r[0] for r in cur.execute("SELECT account_id FROM windows").fetchall()]
            cur.execute("DROP TABLE staged")
            cur.execute("DROP TABLE windows")
            cur.execute("DROP VIEW raw")
    except Exception as exc:
        db.execute(
            "UPDATE ingestion_runs SET status = 'failed', error = ?, finished_at = now() WHERE id = ?",
            [str(exc)[:2000], run_id],
        )
        if isinstance(exc, IngestError):
            raise
        raise IngestError(str(exc)) from exc

    result = IngestResult(
        ingestion_id=run_id,
        provider=provider,
        rows_loaded=int(rows),
        source_total=float(source_total),
        loaded_total=float(loaded_total),
        date_from=str(d0) if d0 else None,
        date_to=str(d1) if d1 else None,
        accounts=accounts,
    )
    db.execute(
        "UPDATE ingestion_runs SET status = ?, provider = ?, rows_loaded = ?, source_total = ?, loaded_total = ?, "
        "date_from = ?, date_to = ?, finished_at = now() WHERE id = ?",
        [
            "succeeded" if result.reconciled else "reconciliation_mismatch",
            provider,
            result.rows_loaded,
            result.source_total,
            result.loaded_total,
            d0,
            d1,
            run_id,
        ],
    )
    log.info(
        "ingested %s rows (%s) for tenant %s: source=%.2f loaded=%.2f",
        result.rows_loaded, provider, tenant_id, result.source_total, result.loaded_total,
    )
    return result


def set_fx_rate(db: Database, currency: str, rate_date: str, rate_to_base: float) -> None:
    db.execute(
        "INSERT INTO fx_rates VALUES (?, ?, ?) ON CONFLICT (currency, rate_date) DO UPDATE SET rate_to_base = excluded.rate_to_base",
        [currency.upper(), rate_date, rate_to_base],
    )
