"""Approved estimate (BOQ) vs actual variance.

Imports an Azure Pricing Calculator export (the "Microsoft Azure Estimate"
workbook: Service category / Service type / Custom name / Region /
Description / Estimated monthly cost) and compares it with a month of actual
cost, by component and region, including actual spend that has no BOQ line.
This automates the "Azure Cost Review — BOQ vs first full month" workbook.
"""
from __future__ import annotations

import fnmatch
from datetime import date
from pathlib import Path

from ..db import Database, new_id
from ..timeutil import fmt_month, month_end, month_start, region_display

# Calculator "Service type" -> list of (meter category, meter sub-category glob)
SERVICE_MAP: dict[str, list[tuple[str, str]]] = {
    "Virtual Machines": [("Virtual Machines", "*")],
    "Managed Disks": [("Storage", "*Managed Disks")],
    "IP Addresses": [("Virtual Network", "IP Addresses")],
    "Azure Backup": [("Backup", "*")],
    "Azure DDoS Protection": [("Azure DDOS Protection", "*"), ("Azure DDoS Protection", "*")],
    "Application Gateway": [("Application Gateway", "*")],
    "Azure Site Recovery": [("Azure Site Recovery", "*")],
    "VPN Gateway": [("VPN Gateway", "*")],
    "Bandwidth": [("Bandwidth", "*")],
    "Storage Accounts": [("Storage", "*Blob*"), ("Storage", "Files*"), ("Storage", "Tables"), ("Storage", "Queues")],
    "Azure SQL Database": [("SQL Database", "*")],
    "Log Analytics": [("Log Analytics", "*")],
    "Azure Monitor": [("Azure Monitor", "*")],
    "Azure Firewall": [("Azure Firewall", "*")],
    "Load Balancer": [("Load Balancer", "*")],
    "Azure DNS": [("Azure DNS", "*")],
    "Private Link": [("Virtual Network", "Private Link")],
}

HEADERS = ["service category", "service type", "custom name", "region", "description", "estimated monthly cost"]


def import_boq_rows(db: Database, tenant_id: str, boq_name: str, rows: list[dict]) -> int:
    db.execute("DELETE FROM boq_items WHERE tenant_id = ? AND boq_name = ?", [tenant_id, boq_name])
    n = 0
    for r in rows:
        if r.get("monthly_cost") in (None, ""):
            continue
        db.execute(
            "INSERT INTO boq_items (id, tenant_id, boq_name, service_category, service_type, custom_name, region, description, "
            "monthly_cost, upfront_cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [new_id(), tenant_id, boq_name, r.get("service_category"), r.get("service_type"), r.get("custom_name"),
             r.get("region"), r.get("description"), float(r["monthly_cost"]), float(r.get("upfront_cost") or 0)],
        )
        n += 1
    return n


def parse_calculator_xlsx(path: str | Path) -> list[dict]:
    """Rows of a Pricing Calculator export. Finds the header row on any sheet; stops at 'Total'."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        header_idx: dict[str, int] | None = None
        out: list[dict] = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip().lower() if c is not None else "" for c in row]
            if header_idx is None:
                if all(h in cells for h in HEADERS[:2]) and "estimated monthly cost" in cells:
                    header_idx = {h: cells.index(h) for h in HEADERS if h in cells}
                    up = "estimated upfront cost"
                    if up in cells:
                        header_idx[up] = cells.index(up)
                continue
            first = cells[header_idx["service category"]]
            if first in ("total", "disclaimer") or first.startswith("licensing"):
                break
            cost = row[header_idx["estimated monthly cost"]]
            if not first or not isinstance(cost, (int, float)):
                continue

            def get(h: str):
                i = header_idx.get(h)
                return row[i] if i is not None and i < len(row) else None

            out.append({
                "service_category": get("service category"), "service_type": get("service type"),
                "custom_name": get("custom name"), "region": get("region"), "description": get("description"),
                "monthly_cost": float(cost), "upfront_cost": get("estimated upfront cost"),
            })
        if header_idx is not None:
            return out
    raise ValueError("no Azure Pricing Calculator table found (expected 'Service category', 'Service type', 'Estimated monthly cost')")


def list_boqs(db: Database, tenant_id: str) -> list[dict]:
    return db.query(
        "SELECT boq_name, count(*) AS items, SUM(monthly_cost) AS monthly_total, max(imported_at) AS imported_at "
        "FROM boq_items WHERE tenant_id = ? GROUP BY 1 ORDER BY imported_at DESC",
        [tenant_id],
    )


def _match(service_type: str | None, category: str | None, subcategory: str | None) -> bool:
    for cat, pattern in SERVICE_MAP.get(service_type or "", [(service_type or "", "*")]):
        if (category or "").lower() == cat.lower() and fnmatch.fnmatch((subcategory or "").lower(), pattern.lower()):
            return True
    return False


def variance(db: Database, tenant_id: str, boq_name: str, month: date) -> dict:
    m0, m1 = month_start(month), month_end(month)
    boq = db.query(
        "SELECT service_type, SUM(monthly_cost) AS estimate, string_agg(DISTINCT region, ', ') AS regions, count(*) AS items "
        "FROM boq_items WHERE tenant_id = ? AND boq_name = ? GROUP BY 1 ORDER BY estimate DESC",
        [tenant_id, boq_name],
    )
    if not boq:
        raise ValueError("BOQ not found")
    actual = db.query(
        "SELECT meter_category, COALESCE(meter_subcategory, '') AS meter_subcategory, location, SUM(cost_base) AS cost "
        "FROM cost_records WHERE tenant_id = ? AND charge_date BETWEEN ? AND ? GROUP BY 1, 2, 3",
        [tenant_id, m0, m1],
    )
    as_of = db.scalar("SELECT max(charge_date) FROM cost_records WHERE tenant_id = ?", [tenant_id])
    lines, used = [], set()
    for b in boq:
        by_region: dict[str, float] = {}
        for i, a in enumerate(actual):
            if i not in used and _match(b["service_type"], a["meter_category"], a["meter_subcategory"]):
                used.add(i)
                key = region_display(a["location"])
                by_region[key] = by_region.get(key, 0.0) + a["cost"]
        act = sum(by_region.values())
        lines.append(_line(b["service_type"], b["estimate"], act, by_region, in_boq=True))
    unmatched: dict[str, dict[str, float]] = {}
    for i, a in enumerate(actual):
        if i in used:
            continue
        comp = a["meter_category"] + (f" – {a['meter_subcategory']}" if a["meter_subcategory"] else "")
        reg = unmatched.setdefault(comp, {})
        reg[region_display(a["location"])] = reg.get(region_display(a["location"]), 0.0) + a["cost"]
    for comp, by_region in sorted(unmatched.items(), key=lambda kv: -sum(kv[1].values())):
        lines.append(_line(comp, 0.0, sum(by_region.values()), by_region, in_boq=False))
    est_total = sum(line["estimate"] for line in lines)
    act_total = sum(line["actual"] for line in lines)
    regions = sorted({r for line in lines for r in line["actual_by_region"]})
    return {
        "boq_name": boq_name,
        "month": m0.strftime("%Y-%m"),
        "label": f"{fmt_month(m0)} ({'month-to-date, partial' if as_of is None or as_of < m1 else 'full month, actual'})",
        "is_partial": as_of is None or as_of < m1,
        "regions": regions,
        "lines": lines,
        "estimate_total": round(est_total, 2),
        "actual_total": round(act_total, 2),
        "difference": round(act_total - est_total, 2),
        "difference_pct": round(100 * (act_total - est_total) / est_total, 2) if est_total else None,
    }


def _line(component: str, estimate: float, actual: float, by_region: dict[str, float], in_boq: bool) -> dict:
    diff = actual - estimate
    return {
        "component": component,
        "in_boq": in_boq,
        "estimate": round(estimate, 2),
        "actual": round(actual, 2),
        "actual_by_region": {k: round(v, 2) for k, v in by_region.items()},
        "difference": round(diff, 2),
        "difference_pct": round(100 * diff / estimate, 2) if estimate else None,
        "note": "Not in BOQ" if not in_boq else ("No actual spend this month" if actual == 0 else ""),
    }

