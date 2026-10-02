"""Opening a database whose WAL holds an ALTER that DuckDB cannot replay directly (startup crash on restart)."""
import duckdb
import pytest

from cloudarc.db import Database


def test_unreplayable_wal_is_recovered_on_open(tmp_path):
    path = str(tmp_path / "cloudarc.duckdb")
    con = duckdb.connect(path)
    con.execute("PRAGMA disable_checkpoint_on_shutdown")
    con.execute("CREATE TABLE t (id INTEGER, created_at TIMESTAMP NOT NULL DEFAULT now())")
    con.execute("CHECKPOINT")
    con.execute("INSERT INTO t (id) VALUES (1)")
    con.execute("ALTER TABLE t ADD COLUMN config JSON")  # only in the WAL, as after a migration then a restart
    con.close()

    with pytest.raises(duckdb.InternalException, match="replaying WAL"):
        duckdb.connect(path)
    db = Database(path)
    assert db.scalar("SELECT count(*) FROM t") == 1 and "config" in [r["column_name"] for r in db.query("DESCRIBE t")]
    db.close()
    duckdb.connect(path).close()  # the WAL is folded into the file: a plain open works again


def test_schema_changes_are_checkpointed_on_open(tmp_path):
    path = tmp_path / "cloudarc.duckdb"
    Database(str(path)).close()
    wal = tmp_path / "cloudarc.duckdb.wal"
    assert not wal.exists() or wal.stat().st_size == 0
