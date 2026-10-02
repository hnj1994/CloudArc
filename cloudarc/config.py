"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"CLOUDARC_{name}", default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: str
    base_currency: str
    master_key: str | None
    auth_mode: str  # "dev" (API tokens only) or "entra" (Entra ID JWT + API tokens)
    entra_tenant_id: str | None
    entra_client_id: str | None
    sync_hour_utc: int
    sync_lookback_days: int
    initial_backfill_months: int
    retention_months: int
    scheduler_enabled: bool
    fx_defaults: dict[str, float] = field(default_factory=dict)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    org_name: str = "CloudArc"  # shown as "Prepared by" and in report footers
    google_site_verification: str | None = None  # Search Console HTML-tag token, served as a meta tag on "/"
    llm_provider: str | None = None  # None | "anthropic"
    llm_model: str | None = None
    llm_data_policy_approved: bool = False
    ri_discount_1y: float = 0.35
    ri_discount_3y: float = 0.55

    @property
    def reports_dir(self) -> Path:
        p = self.data_dir / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache
def get_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        db_path=_env("DB_PATH", str(data_dir / "cloudarc.duckdb")),
        base_currency=_env("BASE_CURRENCY", "INR"),
        master_key=_env("MASTER_KEY"),
        auth_mode=_env("AUTH_MODE", "dev"),
        entra_tenant_id=_env("ENTRA_TENANT_ID"),
        entra_client_id=_env("ENTRA_CLIENT_ID"),
        sync_hour_utc=int(_env("SYNC_HOUR_UTC", "2")),
        sync_lookback_days=int(_env("SYNC_LOOKBACK_DAYS", "5")),
        initial_backfill_months=int(_env("INITIAL_BACKFILL_MONTHS", "3")),
        retention_months=int(_env("RETENTION_MONTHS", "24")),
        scheduler_enabled=_bool(_env("SCHEDULER_ENABLED"), True),
        fx_defaults=json.loads(_env("FX_DEFAULTS", '{"USD": 88.0, "EUR": 103.0, "GBP": 118.0}')),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=int(_env("SMTP_PORT", "587")),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_from=_env("SMTP_FROM"),
        org_name=_env("ORG_NAME", "CloudArc"),
        google_site_verification=_env("GOOGLE_SITE_VERIFICATION"),
        llm_provider=_env("LLM_PROVIDER"),
        llm_model=_env("LLM_MODEL"),
        llm_data_policy_approved=_bool(_env("LLM_DATA_POLICY_APPROVED")),
        ri_discount_1y=float(_env("RI_DISCOUNT_1Y", "0.35")),
        ri_discount_3y=float(_env("RI_DISCOUNT_3Y", "0.55")),
    )
