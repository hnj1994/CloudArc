"""First-start admin bootstrap for hosts without an interactive shell (e.g. Azure App Service).

When CLOUDARC_BOOTSTRAP_ADMIN_EMAIL is set and the database has no users, a
platform admin is created and its API token is written to
``<data dir>/bootstrap-admin-token.txt`` (mode 0600) — never to the logs.
Read it once from the persistent storage (Kudu / SSH), then delete it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ..db import Database
from . import auth

log = logging.getLogger(__name__)


def bootstrap_admin(db: Database, email: str | None, data_dir: Path) -> Path | None:
    if not email or db.scalar("SELECT count(*) FROM users"):
        return None
    uid = auth.create_user(db, email, "Platform Admin", is_platform_admin=True)
    token = auth.issue_token(db, uid, "bootstrap")
    path = data_dir / "bootstrap-admin-token.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{email}\n{token}\n")
    log.warning("bootstrap admin %s created; API token written to %s (read once, then delete)", email, path)
    return path
