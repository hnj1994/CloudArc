import base64
import csv
import os
import tempfile

# Settings are cached on first use, so configure the environment before importing cloudarc.
_TMP = tempfile.mkdtemp(prefix="cloudarc-test-")
os.environ.setdefault("CLOUDARC_DATA_DIR", _TMP)
os.environ.setdefault("CLOUDARC_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("CLOUDARC_SCHEDULER_ENABLED", "false")

from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from cloudarc import demo  # noqa: E402
from cloudarc.db import Database, set_db  # noqa: E402

AS_OF = date(2026, 9, 29)


@pytest.fixture
def db():
    d = Database()
    set_db(d)
    yield d
    set_db(None)
    d.close()


@pytest.fixture(scope="session")
def seed_template(tmp_path_factory):
    """Seed the demo estate once; each test gets its own copy of the database file."""
    base = tmp_path_factory.mktemp("seed")
    path = base / "seed.duckdb"
    d = Database(str(path))
    set_db(d)
    info = demo.seed(d, base / "demo", AS_OF)
    d.execute("CHECKPOINT")
    d.close()
    set_db(None)
    return path, info


@pytest.fixture
def seeded(seed_template, tmp_path: Path):
    import shutil

    path, info = seed_template
    copy = tmp_path / "db.duckdb"
    shutil.copy(path, copy)
    d = Database(str(copy))
    set_db(d)
    yield d, info
    set_db(None)
    d.close()


@pytest.fixture
def client(seeded):
    from fastapi.testclient import TestClient

    from cloudarc.api.app import create_app

    db, info = seeded
    tokens = list(info["tokens"].values())
    c = TestClient(create_app(db, start_scheduler=False))
    c.tokens = {"admin": tokens[0], "analyst": tokens[1], "viewer": tokens[2]}
    c.hdr = lambda who: {"Authorization": f"Bearer {c.tokens[who]}"}
    return c


def write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path
