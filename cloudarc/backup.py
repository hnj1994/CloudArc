"""Backups to Azure Blob Storage and staged restores.

A backup is a consistent ``EXPORT DATABASE`` (Parquet, portable across DuckDB versions) packed as
``cloudarc-<UTC timestamp>.tar.gz`` with a manifest of row counts, uploaded with the web app's managed
identity (no storage keys). Retention is a lifecycle rule on the container (deploy/azure/setup-backups.sh).

Restore never touches the open database: ``stage_restore`` imports the archive into
``<db>.restore`` and checks the row counts against the manifest; the next start swaps it in
(``apply_pending_restore``) and keeps the previous file as ``<db>.pre-restore-<timestamp>``.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx

from . import __version__
from .db import Database

log = logging.getLogger(__name__)
STORAGE_API = "2023-11-03"
PREFIX = "cloudarc-"


class BackupError(RuntimeError):
    pass


def managed_identity_token(resource: str = "https://storage.azure.com/", http: httpx.Client | None = None) -> str:
    """Token for the App Service managed identity (IDENTITY_ENDPOINT / IDENTITY_HEADER)."""
    endpoint, secret = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
    if not endpoint or not secret:
        raise BackupError("no managed identity in this environment (enable it on the web app)")
    r = (http or httpx).get(endpoint, params={"resource": resource, "api-version": "2019-08-01"},
                            headers={"X-IDENTITY-HEADER": secret}, timeout=30)
    if r.status_code != 200:
        raise BackupError(f"managed identity token request failed ({r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def _tables(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY 1"
    ).fetchall()]


def snapshot(db: Database, workdir: Path) -> Path:
    """Consistent export of the live database (one transaction) packed as tar.gz with a manifest."""
    export = workdir / "export"
    cur = db.cursor()
    try:
        cur.execute(f"EXPORT DATABASE '{export}' (FORMAT PARQUET)")
        counts = {t: cur.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in _tables(cur)}
    finally:
        cur.close()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (export / "manifest.json").write_text(json.dumps(
        {"created_at": stamp, "app_version": __version__, "duckdb": duckdb.__version__, "row_counts": counts}, indent=1))
    archive = workdir / f"{PREFIX}{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(export, arcname="export")
    return archive


class BlobContainer:
    """Minimal Blob REST client for one container, authenticated with a bearer token."""

    def __init__(self, url: str, token_fn=managed_identity_token, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.token_fn = token_fn
        self.http = http or httpx.Client(timeout=600)

    def _headers(self, **extra) -> dict:
        return {"Authorization": f"Bearer {self.token_fn()}", "x-ms-version": STORAGE_API, **extra}

    def upload(self, path: Path, metadata: dict[str, str]) -> None:
        headers = self._headers(**{"x-ms-blob-type": "BlockBlob", "Content-Type": "application/gzip",
                                   "Content-Length": str(path.stat().st_size)},
                                **{f"x-ms-meta-{k}": v for k, v in metadata.items()})
        with path.open("rb") as fh:
            r = self.http.put(f"{self.url}/{path.name}", content=fh, headers=headers)
        if r.status_code != 201:
            raise BackupError(f"upload failed ({r.status_code}): {r.text[:300]}")

    def list(self) -> list[dict]:
        out, marker = [], ""
        while True:
            params = {"restype": "container", "comp": "list", "prefix": PREFIX, **({"marker": marker} if marker else {})}
            r = self.http.get(self.url, params=params, headers=self._headers())
            if r.status_code != 200:
                raise BackupError(f"list failed ({r.status_code}): {r.text[:300]}")
            root = ET.fromstring(r.content)
            for b in root.iter("Blob"):
                props = b.find("Properties")
                out.append({"name": b.findtext("Name"), "size": int(props.findtext("Content-Length") or 0),
                            "last_modified": props.findtext("Last-Modified")})
            marker = root.findtext("NextMarker") or ""
            if not marker:
                return sorted(out, key=lambda b: b["name"])

    def download(self, name: str, dest: Path) -> None:
        with self.http.stream("GET", f"{self.url}/{name}", headers=self._headers()) as r:
            if r.status_code != 200:
                raise BackupError(f"download of {name} failed ({r.status_code})")
            with dest.open("wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)


def run_backup(db: Database, container: BlobContainer) -> dict:
    with tempfile.TemporaryDirectory(prefix="cloudarc-backup-") as tmp:
        archive = snapshot(db, Path(tmp))
        size = archive.stat().st_size
        container.upload(archive, {"appversion": __version__})
    result = {"name": archive.name, "bytes": size, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    log.info("backup uploaded: %s (%s bytes)", archive.name, size)
    return result


def record(db: Database, key: str, value: dict) -> None:
    db.execute("INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, now()) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()", [key, json.dumps(value)])


def state(db: Database, key: str) -> dict | None:
    v = db.scalar("SELECT CAST(value AS VARCHAR) FROM system_state WHERE key = ?", [key])
    return json.loads(v) if v else None


def stage_restore(archive: Path, db_path: str) -> dict:
    """Import ``archive`` into ``<db_path>.restore`` and verify it against the archive's manifest."""
    staged = Path(f"{db_path}.restore")
    tmp_db = Path(f"{db_path}.restore.tmp")
    for p in (staged, tmp_db, Path(f"{tmp_db}.wal")):
        p.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="cloudarc-restore-") as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        export = Path(tmp) / "export"
        manifest = json.loads((export / "manifest.json").read_text())
        con = duckdb.connect(str(tmp_db))
        try:
            con.execute(f"IMPORT DATABASE '{export}'")
            got = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in _tables(con)}
            con.execute("CHECKPOINT")
        finally:
            con.close()
    expected = manifest["row_counts"]
    diff = {t: (expected.get(t), got.get(t)) for t in set(expected) | set(got) if expected.get(t) != got.get(t)}
    if diff:
        tmp_db.unlink(missing_ok=True)
        raise BackupError(f"restored row counts differ from the backup manifest: {diff}")
    tmp_db.rename(staged)
    return {"staged": str(staged), "backup_created_at": manifest["created_at"], "tables": len(got), "rows": sum(got.values())}


def apply_pending_restore(db_path: str) -> bool:
    """Swap in a staged restore before the database is opened. Keeps the old file alongside."""
    staged = Path(f"{db_path}.restore")
    if db_path == ":memory:" or not staged.exists():
        return False
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for suffix in ("", ".wal"):
        cur = Path(db_path + suffix)
        if cur.exists():
            shutil.move(str(cur), f"{db_path}.pre-restore-{stamp}{suffix}")
    shutil.move(str(staged), db_path)
    log.warning("applied staged restore; previous database kept as %s.pre-restore-%s", db_path, stamp)
    return True
