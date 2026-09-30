"""FastAPI application factory."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..analytics.filters import FilterError
from ..budgets import BudgetError
from ..config import get_settings
from ..db import Database, get_db, set_db
from ..ingest.loader import IngestError
from ..logging_setup import configure
from ..recommendations.engine import LifecycleError
from ..tenants import NotFound
from . import admin, routes

log = logging.getLogger("cloudarc.api")
STATIC = Path(__file__).resolve().parent.parent / "web" / "static"


def create_app(db: Database | None = None, start_scheduler: bool | None = None) -> FastAPI:
    configure()
    if db is not None:
        set_db(db)
    run_scheduler = get_settings().scheduler_enabled if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        if run_scheduler:
            from ..sync import Scheduler

            scheduler = Scheduler(get_db())
            scheduler.start()
        yield
        if scheduler:
            scheduler.stop()

    app = FastAPI(title="CloudArc", version=__version__, lifespan=lifespan,
                  description="Multi-tenant cloud cost management & governance API")

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        response = await call_next(request)
        ms = round(1000 * (time.perf_counter() - t0), 1)
        response.headers["X-Request-Id"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            log.info("request", extra={"request_id": rid, "path": request.url.path, "status": response.status_code, "duration_ms": ms})
        return response

    def handler(status: int):
        async def _h(request: Request, exc: Exception):
            return JSONResponse({"detail": str(exc)}, status_code=status)
        return _h

    for exc_type in (FilterError, BudgetError, IngestError, LifecycleError, ValueError):
        app.add_exception_handler(exc_type, handler(400))
    app.add_exception_handler(NotFound, handler(404))

    app.include_router(admin.router)
    app.include_router(routes.router)

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC / "index.html")

    return app


def app_factory() -> FastAPI:  # used by `uvicorn cloudarc.api.app:app_factory --factory`
    return create_app()
