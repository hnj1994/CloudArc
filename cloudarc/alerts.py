"""Alert store, acknowledgement workflow and notification channels (FR-602, FR-606).

Channels: e-mail (SMTP), Microsoft Teams incoming webhook, and a generic JSON
webhook (Slack-compatible ``text`` field) for ITSM or chat integrations.
Channel targets (webhook URLs embed credentials) are encrypted at rest.
"""
from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import get_settings
from .db import Database, new_id
from .security.secrets import SecretBox, hint

log = logging.getLogger(__name__)
CHANNEL_KINDS = {"email", "teams", "webhook"}


def raise_alert(db: Database, tenant_id: str, *, kind: str, severity: str, message: str, details: dict | None,
                dedupe_key: str, budget_id: str | None = None) -> dict | None:
    """Insert an alert unless one with the same dedupe key exists. Returns the new alert or None."""
    exists = db.scalar("SELECT count(*) FROM alerts WHERE tenant_id = ? AND dedupe_key = ?", [tenant_id, dedupe_key])
    if exists:
        return None
    aid = new_id()
    db.execute(
        "INSERT INTO alerts (id, tenant_id, kind, severity, message, details, budget_id, dedupe_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, dedupe_key) DO NOTHING",
        [aid, tenant_id, kind, severity, message, json.dumps(details or {}, default=str), budget_id, dedupe_key],
    )
    return {"id": aid, "kind": kind, "severity": severity, "message": message}


def list_alerts(db: Database, tenant_id: str, include_acknowledged: bool = True, limit: int = 200) -> list[dict]:
    cond = "" if include_acknowledged else " AND acknowledged_at IS NULL"
    rows = db.query(
        f"SELECT id, kind, severity, message, CAST(details AS VARCHAR) AS details, budget_id, created_at, notified_at, "
        f"acknowledged_by, acknowledged_at FROM alerts WHERE tenant_id = ?{cond} ORDER BY created_at DESC LIMIT {int(limit)}",
        [tenant_id],
    )
    for r in rows:
        r["details"] = json.loads(r["details"] or "{}")
    return rows


def acknowledge(db: Database, tenant_id: str, alert_id: str, user_id: str) -> bool:
    if not db.scalar("SELECT count(*) FROM alerts WHERE tenant_id = ? AND id = ?", [tenant_id, alert_id]):
        return False
    db.execute(
        "UPDATE alerts SET acknowledged_by = ?, acknowledged_at = now() WHERE tenant_id = ? AND id = ?",
        [user_id, tenant_id, alert_id],
    )
    return True


# ---- channels -------------------------------------------------------------------------------

def add_channel(db: Database, tenant_id: str, kind: str, target: str) -> str:
    if kind not in CHANNEL_KINDS:
        raise ValueError(f"kind must be one of {sorted(CHANNEL_KINDS)}")
    if kind in {"teams", "webhook"} and not target.startswith("https://"):
        raise ValueError("webhook targets must be https URLs")
    cid = new_id()
    sealed = SecretBox.from_settings().seal(target, aad=f"channel:{tenant_id}")
    shown = target if kind == "email" else hint(target)
    db.execute(
        "INSERT INTO alert_channels (id, tenant_id, kind, target_ciphertext, target_hint) VALUES (?, ?, ?, ?, ?)",
        [cid, tenant_id, kind, sealed, shown],
    )
    return cid


def list_channels(db: Database, tenant_id: str) -> list[dict]:
    return db.query(
        "SELECT id, kind, target_hint, created_at FROM alert_channels WHERE tenant_id = ? ORDER BY created_at", [tenant_id]
    )


def delete_channel(db: Database, tenant_id: str, channel_id: str) -> None:
    db.execute("DELETE FROM alert_channels WHERE tenant_id = ? AND id = ?", [tenant_id, channel_id])


def _teams_payload(tenant_name: str, items: list[dict]) -> dict:
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [{"type": "TextBlock", "weight": "Bolder", "size": "Medium", "text": f"CloudArc alerts · {tenant_name}"}]
                + [{"type": "TextBlock", "wrap": True, "text": f"**{a['severity'].upper()}** — {a['message']}"} for a in items],
            },
        }],
    }


def _send_email(to: str, subject: str, body: str) -> None:
    s = get_settings()
    if not s.smtp_host:
        raise RuntimeError("SMTP is not configured (CLOUDARC_SMTP_HOST)")
    msg = EmailMessage()
    msg["From"] = s.smtp_from or s.smtp_user or "cloudarc@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password or "")
        smtp.send_message(msg)


def dispatch(db: Database, tenant_id: str, items: list[dict], client: httpx.Client | None = None) -> dict:
    """Send alerts to every channel of the tenant. Failures are logged, never raised to the sync."""
    channels = db.query("SELECT id, kind, target_ciphertext FROM alert_channels WHERE tenant_id = ?", [tenant_id])
    tenant = db.one("SELECT name FROM tenants WHERE id = ?", [tenant_id]) or {"name": tenant_id}
    box = SecretBox.from_settings()
    sent, failed = 0, 0
    http = client or httpx.Client(timeout=20)
    try:
        for ch in channels:
            target = box.open(ch["target_ciphertext"], aad=f"channel:{tenant_id}")
            try:
                if ch["kind"] == "teams":
                    http.post(target, json=_teams_payload(tenant["name"], items)).raise_for_status()
                elif ch["kind"] == "webhook":
                    text = "\n".join(f"[{a['severity']}] {a['message']}" for a in items)
                    http.post(target, json={"text": text, "tenant": tenant["name"], "alerts": items}).raise_for_status()
                else:
                    _send_email(target, f"CloudArc: {len(items)} alert(s) for {tenant['name']}",
                                "\n\n".join(f"[{a['severity'].upper()}] {a['message']}" for a in items))
                sent += 1
            except Exception as exc:  # noqa: BLE001 - a broken channel must not break the sync
                failed += 1
                log.warning("alert channel %s (%s) failed: %s", ch["id"], ch["kind"], type(exc).__name__)
    finally:
        if client is None:
            http.close()
    if sent:
        ids = [a["id"] for a in items]
        db.execute(
            f"UPDATE alerts SET notified_at = now() WHERE tenant_id = ? AND id IN ({', '.join('?' for _ in ids)})",
            [tenant_id, *ids],
        )
    return {"sent": sent, "failed": failed}
