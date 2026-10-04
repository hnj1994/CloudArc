"""Backups to Blob storage (faked REST surface) and staged restores."""
import dataclasses
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from cloudarc import backup, sync
from cloudarc.config import get_settings
from cloudarc.db import Database, get_db

URL = "https://acct.blob.core.windows.net/backups"


class FakeBlob:
    def __init__(self, fail_uploads=False):
        self.blobs: dict[str, bytes] = {}
        self.fail = fail_uploads

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok" and request.headers["x-ms-version"]
        name = request.url.path.split("/backups", 1)[1].lstrip("/")
        if request.method == "PUT":
            if self.fail:
                return httpx.Response(403, text="AuthorizationPermissionMismatch")
            assert request.headers["x-ms-blob-type"] == "BlockBlob"
            self.blobs[name] = request.read()
            return httpx.Response(201)
        if request.method == "GET" and not name:
            items = "".join(f"<Blob><Name>{n}</Name><Properties><Content-Length>{len(b)}</Content-Length>"
                            f"<Last-Modified>now</Last-Modified></Properties></Blob>" for n, b in sorted(self.blobs.items()))
            return httpx.Response(200, content=f"<EnumerationResults><Blobs>{items}</Blobs><NextMarker/></EnumerationResults>")
        if request.method == "GET":
            return httpx.Response(200, content=self.blobs[name])
        return httpx.Response(405)


def container(fake: FakeBlob) -> backup.BlobContainer:
    return backup.BlobContainer(URL, token_fn=lambda: "tok", http=httpx.Client(transport=httpx.MockTransport(fake.handler)))


def counts(db: Database) -> dict:
    tables = [r["table_name"] for r in db.query("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
                                                 "AND table_type = 'BASE TABLE'")]
    return {t: db.scalar(f'SELECT count(*) FROM "{t}"') for t in tables}


def test_backup_upload_list_restore_round_trip(seeded, tmp_path):
    db, _ = seeded
    fake = FakeBlob()
    res = sync.nightly_backup(db, container(fake))
    assert res["name"].startswith("cloudarc-") and res["name"] in fake.blobs
    assert backup.state(db, "backup")["status"] == "succeeded"
    assert [b["name"] for b in container(fake).list()] == [res["name"]]

    archive = tmp_path / res["name"]
    container(fake).download(res["name"], archive)
    target = str(tmp_path / "restored.duckdb")
    Database(target).close()  # an existing (different) database that the restore replaces
    staged = backup.stage_restore(archive, target)
    assert staged["rows"] > 1000 and Path(target + ".restore").exists()

    restored = Database(target)  # swap happens on open
    try:
        src, got = counts(db), counts(restored)
        after = {"system_state", "audit_log"}  # written after the snapshot (backup outcome)
        assert {t: n for t, n in got.items() if t not in after} == {t: n for t, n in src.items() if t not in after}
        assert got["audit_log"] == src["audit_log"] - 1
    finally:
        restored.close()
    assert list(tmp_path.glob("restored.duckdb.pre-restore-*")) and not Path(target + ".restore").exists()


def test_restore_refuses_an_archive_that_does_not_match_its_manifest(seeded, tmp_path):
    db, _ = seeded
    archive = backup.snapshot(db, tmp_path)
    work = tmp_path / "x"
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    manifest = work / "export" / "manifest.json"
    m = json.loads(manifest.read_text())
    m["row_counts"]["cost_records"] += 1
    manifest.write_text(json.dumps(m))
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        tar.add(work / "export", arcname="export")
    target = str(tmp_path / "t.duckdb")
    with pytest.raises(backup.BackupError, match="cost_records"):
        backup.stage_restore(bad, target)
    assert not Path(target + ".restore").exists()


def test_failed_backup_is_recorded_and_shown_by_health(client, monkeypatch):
    from cloudarc.api import admin

    db = get_db()
    sync.nightly_backup(db, container(FakeBlob()))
    res = sync.nightly_backup(db, container(FakeBlob(fail_uploads=True)))
    assert "AuthorizationPermissionMismatch" in res["error"]
    monkeypatch.setattr(admin, "get_settings", lambda: dataclasses.replace(get_settings(), backup_url=URL))
    h = client.get("/api/health").json()["backup"]
    assert h["status"] == "failed" and h["last_success"]  # the last good backup is still reported


def test_managed_identity_token_requires_the_platform_endpoint(monkeypatch):
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    with pytest.raises(backup.BackupError, match="managed identity"):
        backup.managed_identity_token()
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://msi.local/token")
    monkeypatch.setenv("IDENTITY_HEADER", "secret-header")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-IDENTITY-HEADER"] == "secret-header" and request.url.params["resource"] == "https://storage.azure.com/"
        return httpx.Response(200, json={"access_token": "tok"})

    assert backup.managed_identity_token(http=httpx.Client(transport=httpx.MockTransport(handler))) == "tok"
