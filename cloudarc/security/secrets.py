"""Encryption of stored credentials and alert-channel targets (FR-103, NFR-01).

Secrets are sealed with AES-256-GCM under a master key supplied through
``CLOUDARC_MASTER_KEY`` (base64, 32 bytes). Plaintext is never persisted,
returned by the API, or written to logs; only a short masked hint is kept so
engineers can tell credentials apart.
"""
from __future__ import annotations

import base64
import logging
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import get_settings

log = logging.getLogger(__name__)
_VERSION = "v1"


class SecretBox:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("master key must be 32 bytes (AES-256)")
        self._aead = AESGCM(key)

    @classmethod
    def from_settings(cls) -> SecretBox:
        raw = get_settings().master_key
        if not raw:
            key_file = get_settings().data_dir / "master.key"
            if not key_file.exists():
                log.warning("CLOUDARC_MASTER_KEY not set; generating %s (dev only)", key_file)
                key_file.write_bytes(base64.b64encode(os.urandom(32)))
                key_file.chmod(0o600)
            raw = key_file.read_text().strip()
        return cls(base64.b64decode(raw))

    def seal(self, plaintext: str, aad: str = "") -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode(), aad.encode())
        return f"{_VERSION}:{base64.b64encode(nonce + ct).decode()}"

    def open(self, token: str, aad: str = "") -> str:
        version, _, body = token.partition(":")
        if version != _VERSION:
            raise ValueError("unknown secret format")
        blob = base64.b64decode(body)
        return self._aead.decrypt(blob[:12], blob[12:], aad.encode()).decode()


def hint(secret: str) -> str:
    """Masked hint safe to show: never more than the last 4 characters."""
    return "••••" + secret[-4:] if len(secret) >= 12 else "••••"


_REDACT = re.compile(
    r'("?(?:client_secret|secret|password|api[_-]?key|token|authorization)"?\s*[:=]\s*)("[^"]*"|[^\s,&}]+)',
    re.IGNORECASE,
)


class RedactingFilter(logging.Filter):
    """Belt-and-braces: masks secret-looking values in any log line."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = _REDACT.sub(r"\1[REDACTED]", msg)
        if redacted != msg:
            record.msg, record.args = redacted, None
        return True
