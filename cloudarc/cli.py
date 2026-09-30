"""Command-line entry point: ``cloudarc <command>``."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


def _db():
    from .db import get_db

    return get_db()


def cmd_serve(a):
    import uvicorn

    uvicorn.run("cloudarc.api.app:app_factory", factory=True, host=a.host, port=a.port, proxy_headers=True)


def cmd_init(a):
    from .security import auth

    db = _db()
    uid = auth.create_user(db, a.admin_email, "Platform Admin", is_platform_admin=True)
    token = auth.issue_token(db, uid, "bootstrap")
    print(json.dumps({"admin_user_id": uid, "api_token": token, "note": "token shown once"}, indent=2))


def cmd_seed_demo(a):
    from . import demo
    from .config import get_settings

    as_of = date.fromisoformat(a.as_of) if a.as_of else None
    res = demo.seed(_db(), get_settings().data_dir / "demo", as_of)
    print(json.dumps(res, indent=2))


def cmd_tenant(a):
    from .tenants import create_tenant

    print(create_tenant(_db(), a.name, a.currency, a.fiscal_year_start))


def cmd_user(a):
    from .security import auth

    db = _db()
    uid = auth.create_user(db, a.email, a.name, a.platform_admin)
    for spec in a.role or []:
        tenant_id, _, role = spec.partition(":")
        auth.assign_role(db, uid, tenant_id, role or "viewer")
    out = {"user_id": uid}
    if a.token:
        out["api_token"] = auth.issue_token(db, uid, "cli")
    print(json.dumps(out, indent=2))


def cmd_ingest(a):
    from .ingest.loader import ingest_files
    from .sync import post_sync

    db = _db()
    res = ingest_files(db, a.tenant, a.files, a.provider, source="cli")
    post_sync(db, a.tenant)
    print(json.dumps({**res.__dict__, "reconciled": res.reconciled}, indent=2, default=str))


def cmd_sync(a):
    from .sync import enqueue, enqueue_daily, run_due_jobs

    db = _db()
    if a.account:
        enqueue(db, a.tenant, a.account, "cli")
    else:
        enqueue_daily(db)
    print(f"processed {run_due_jobs(db)} job(s)")


def cmd_report(a):
    from .reports import builder, render
    from .timeutil import parse_month

    rep = builder.build(_db(), a.tenant, parse_month(a.month) if a.month else None)
    out = Path(a.out or f"Cost-Governance-Report-{rep.month}.{a.format}")
    (render.to_docx if a.format == "docx" else render.to_pdf)(rep, out)
    print(out)


def cmd_backup(a):
    """Consistent snapshot as Parquet (NFR-09). Restore with `cloudarc restore <dir>` into an empty database."""
    db = _db()
    target = Path(a.dir) / date.today().isoformat()
    target.parent.mkdir(parents=True, exist_ok=True)
    db.execute("CHECKPOINT")
    db.execute(f"EXPORT DATABASE '{target}' (FORMAT PARQUET)")
    print(target)


def cmd_restore(a):
    db = _db()
    if db.scalar("SELECT count(*) FROM tenants"):
        sys.exit("refusing to restore into a non-empty database; point CLOUDARC_DB_PATH at a new file")
    from .db import SCHEMA

    for stmt in [s for s in SCHEMA.split(";") if s.strip().upper().startswith("CREATE TABLE")]:
        name = stmt.split("EXISTS", 1)[1].split("(", 1)[0].strip()
        db.execute(f"DROP TABLE IF EXISTS {name}")
    db.execute(f"IMPORT DATABASE '{a.dir}'")
    print("restored")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="cloudarc", description="CloudArc cost management platform")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the API + web console (and the scheduler)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("init", help="create the first platform admin and print an API token")
    s.add_argument("--admin-email", required=True)
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("seed-demo", help="load synthetic demo tenants, users and tokens")
    s.add_argument("--as-of", help="last day of demo data (YYYY-MM-DD); default yesterday")
    s.set_defaults(fn=cmd_seed_demo)

    s = sub.add_parser("create-tenant")
    s.add_argument("name")
    s.add_argument("--currency", default="INR")
    s.add_argument("--fiscal-year-start", type=int, default=4)
    s.set_defaults(fn=cmd_tenant)

    s = sub.add_parser("create-user")
    s.add_argument("email")
    s.add_argument("--name")
    s.add_argument("--platform-admin", action="store_true")
    s.add_argument("--role", action="append", help="TENANT_ID:role (viewer|analyst|tenant_admin); repeatable")
    s.add_argument("--token", action="store_true", help="also issue an API token")
    s.set_defaults(fn=cmd_user)

    s = sub.add_parser("ingest", help="load billing export files (Azure / AWS CUR / GCP)")
    s.add_argument("--tenant", required=True)
    s.add_argument("--provider", choices=["azure", "aws", "gcp"])
    s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("sync", help="run API syncs now (all enabled accounts, or one)")
    s.add_argument("--tenant")
    s.add_argument("--account")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("report", help="generate the monthly Cost & Governance report")
    s.add_argument("--tenant", required=True)
    s.add_argument("--month", help="YYYY-MM; default last closed month")
    s.add_argument("--format", choices=["docx", "pdf"], default="docx")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("backup")
    s.add_argument("--dir", default="backups")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore")
    s.add_argument("dir")
    s.set_defaults(fn=cmd_restore)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
