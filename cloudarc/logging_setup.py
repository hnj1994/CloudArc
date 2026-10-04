"""Structured JSON logging with secret redaction (NFR-08, FR-103)."""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from .security.secrets import RedactingFilter


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "tenant_id", "path", "status", "duration_ms"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(getattr(h, "_cloudarc", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    handler._cloudarc = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
