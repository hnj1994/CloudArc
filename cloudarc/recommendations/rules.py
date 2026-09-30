"""Native optimization rules (FR-801 … FR-805).

Every recommendation carries: the resource, the evidence (metrics and/or cost
data), the proposed action, an estimated monthly saving, and confidence,
effort and risk ratings. Rules that depend on utilization emit nothing when
metrics are missing: no metrics, no recommendation.
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..config import get_settings
from ..db import Database

MONTH_DAYS = 30.4
WINDOW_DAYS = 30
MIN_METRIC_DAYS = 14


@dataclass
class Rec:
    category: str
    horizon: str  # immediate | short_term | ongoing
    resource_id: str | None
    resource_name: str | None
    account_id: str | None
    title: str
    action: str
    evidence: dict
    est_monthly_saving: float
    confidence: str
    effort: str
    risk: str
    variant: str = ""
    source: str = "native"

    @property
    def dedupe_key(self) -> str:
        return f"{self.category}:{self.resource_id or '-'}:{self.variant}"


@dataclass
class Context:
    db: Database
    tenant_id: str
    as_of: date
    window_start: date = field(init=False)

    def __post_init__(self) -> None:
        self.window_start = self.as_of - timedelta(days=WINDOW_DAYS - 1)

    def resource_costs(self, where: str = "TRUE", params: list | None = None) -> dict[str, dict]:
        rows = self.db.query(
            f"""
            SELECT c.resource_id, any_value(c.resource_name) AS name, any_value(c.account_id) AS account_id,
                   any_value(c.location) AS location, any_value(c.meter_name) AS meter_name,
                   any_value(c.meter_subcategory) AS meter_subcategory,
                   SUM(c.cost_base) AS cost, count(DISTINCT c.charge_date) AS days,
                   SUM(CASE WHEN lower(c.unit) LIKE '%hour%' THEN c.quantity ELSE 0 END) AS hours,
                   bool_or(c.pricing_model <> 'OnDemand') AS has_commitment
            FROM cost_records c
            WHERE c.tenant_id = ? AND c.charge_date BETWEEN ? AND ? AND c.resource_id IS NOT NULL AND {where}
            GROUP BY 1
            """,
            [self.tenant_id, self.window_start, self.as_of] + (params or []),
        )
        for r in rows:
            r["monthly_cost"] = r["cost"] * MONTH_DAYS / WINDOW_DAYS
        return {r["resource_id"]: r for r in rows}

    def daily_costs(self, resource_id: str) -> list[float]:
        rows = self.db.query(
            "SELECT charge_date, SUM(cost_base) AS v FROM cost_records WHERE tenant_id = ? AND resource_id = ? "
            "AND charge_date BETWEEN ? AND ? GROUP BY 1 ORDER BY 1",
            [self.tenant_id, resource_id, self.window_start, self.as_of],
        )
        return [float(r["v"]) for r in rows]

    def metrics(self, resource_id: str, metric: str) -> list[dict]:
        return self.db.query(
            "SELECT day, avg, max, min FROM resource_metrics WHERE tenant_id = ? AND resource_id = ? AND metric = ? "
            "AND day BETWEEN ? AND ? ORDER BY day",
            [self.tenant_id, resource_id, metric, self.window_start, self.as_of],
        )

    def inventory(self, rtype: str) -> list[dict]:
        rows = self.db.query(
            "SELECT resource_id, account_id, name, type, resource_group, location, sku, CAST(properties AS VARCHAR) AS properties, "
            "created_time FROM resources WHERE tenant_id = ? AND type = ? AND removed_at IS NULL",
            [self.tenant_id, rtype],
        )
        for r in rows:
            r["properties"] = json.loads(r["properties"] or "{}")
        return rows

    def price(self, sku: str, region: str | None, pricing: str) -> float | None:
        if not region:
            return None
        return self.db.scalar(
            "SELECT hourly_price FROM price_catalog WHERE provider = 'azure' AND lower(sku) = lower(?) AND region = ? "
            "AND pricing = ? AND currency = ?",
            [sku, region, pricing, get_settings().base_currency],
        )


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def _r(x: float) -> float:
    return round(float(x), 2)


# ---- VM sizing -------------------------------------------------------------------------------

_VM = re.compile(r"^(?:standard_)?([a-z]+?)(\d+)(-\d+)?([a-z]*)(?:[_ ](v\d+))?$", re.IGNORECASE)
_VALID_VCPU = [1, 2, 4, 8, 16, 20, 32, 48, 64, 96]


def parse_vm_size(name: str | None) -> dict | None:
    if not name:
        return None
    m = _VM.match(name.strip().replace(" ", "_"))
    if not m:
        return None
    family, n, constrained, feats, ver = m.groups()
    return {"family": family.upper(), "vcpus": int(n), "features": feats.lower(), "version": (ver or "").lower()}


def vm_size_name(family: str, vcpus: int, features: str, version: str) -> str:
    return f"Standard_{family}{vcpus}{features}" + (f"_{version}" if version else "")


def smaller_size(size: dict) -> dict | None:
    target = max((v for v in _VALID_VCPU if v <= size["vcpus"] / 2), default=None)
    if not target or target >= size["vcpus"]:
        return None
    return {**size, "vcpus": target}


def burstable_to_d_series(size: dict) -> str:
    """B-series has no fixed performance; the D-series v5 of the same vCPU count does."""
    feats = size["features"]
    amd = "a" in feats
    return vm_size_name("D", size["vcpus"], ("as" if amd else "s"), "v5")


def vm_rightsizing(ctx: Context) -> list[Rec]:
    recs: list[Rec] = []
    costs = ctx.resource_costs("c.service_name = 'Virtual Machines' AND c.resource_type = 'microsoft.compute/virtualmachines'")
    inv = {r["resource_id"]: r for r in ctx.inventory("microsoft.compute/virtualmachines")}
    for rid, cost in costs.items():
        cpu = ctx.metrics(rid, "Percentage CPU")
        if len(cpu) < MIN_METRIC_DAYS:
            continue  # no metric evidence -> no recommendation
        sku = (inv.get(rid) or {}).get("sku") or cost["meter_name"]
        size = parse_vm_size(sku)
        if not size:
            continue
        avg_cpu = statistics.fmean(m["avg"] for m in cpu if m["avg"] is not None)
        p95_max = _p95([m["max"] for m in cpu if m["max"] is not None])
        mem = ctx.metrics(rid, "Available Memory Bytes")
        credits = ctx.metrics(rid, "CPU Credits Remaining")
        evidence = {
            "window": f"{ctx.window_start.isoformat()} to {ctx.as_of.isoformat()}",
            "metric_days": len(cpu),
            "cpu_avg_pct": _r(avg_cpu),
            "cpu_p95_daily_max_pct": _r(p95_max),
            "current_size": sku,
            "monthly_compute_cost": _r(cost["monthly_cost"]),
        }
        if mem:
            evidence["available_memory_min_gb"] = _r(min(m["min"] or m["avg"] for m in mem) / 1024 ** 3)
        region = cost["location"]
        name = cost["name"]

        if size["family"] == "B" and credits:
            exhausted_days = sum(1 for m in credits if (m["min"] if m["min"] is not None else m["avg"]) < 1)
            evidence["cpu_credit_exhausted_days"] = exhausted_days
            if exhausted_days >= 3:
                target = burstable_to_d_series(size)
                cur_p, tgt_p = ctx.price(sku, region, "payg"), ctx.price(target, region, "payg")
                delta = (cur_p - tgt_p) * 730 if cur_p and tgt_p else 0.0
                recs.append(Rec(
                    "vm_series_change", "immediate", rid, name, cost["account_id"],
                    f"Move burstable VM {name} to fixed-performance {target}",
                    f"CPU credits were exhausted on {exhausted_days} of {len(credits)} days, so the VM is being throttled "
                    f"to its baseline. Move {sku} to {target} (same vCPU count) for consistent performance.",
                    evidence | {"target_size": target}, _r(delta), "high", "medium", "medium", variant=target,
                ))
                continue

        if avg_cpu < 15 and p95_max < 40:
            smaller = smaller_size(size)
            if smaller:
                target = vm_size_name(smaller["family"], smaller["vcpus"], smaller["features"], smaller["version"])
                cur_p, tgt_p = ctx.price(sku, region, "payg"), ctx.price(target, region, "payg")
                if cur_p and tgt_p:
                    saving = (cur_p - tgt_p) * 730
                    evidence["pricing_basis"] = "Azure retail prices"
                else:
                    saving = cost["monthly_cost"] * (1 - smaller["vcpus"] / size["vcpus"])
                    evidence["pricing_basis"] = "pro-rata by vCPU (VM prices are linear within a series)"
                confidence = "high" if mem and len(cpu) >= 28 else "medium"
                if not mem:
                    evidence["note"] = "memory utilization not collected; validate memory headroom before resizing"
                if cost["has_commitment"]:
                    evidence["reservation"] = ("VM is covered by a reservation: the saving is realized by exchanging it or "
                                               "re-scoping it to the smaller size (instance size flexibility)")
                recs.append(Rec(
                    "vm_rightsizing", "immediate", rid, name, cost["account_id"],
                    f"Right-size {name} from {sku} to {target}",
                    f"Average CPU {avg_cpu:.1f}% and 95th-percentile daily peak {p95_max:.1f}% over {len(cpu)} days. "
                    f"Resize {sku} → {target} ({size['vcpus']} → {smaller['vcpus']} vCPU) in a maintenance window.",
                    evidence | {"target_size": target}, _r(saving), confidence, "low", "low", variant=target,
                ))
        elif avg_cpu > 80 or p95_max > 95:
            recs.append(Rec(
                "vm_upsize", "immediate", rid, name, cost["account_id"],
                f"{name} is CPU-constrained",
                f"Average CPU {avg_cpu:.1f}% / peak {p95_max:.1f}%. Consider the next size up or scaling out.",
                evidence, 0.0, "medium", "medium", "medium",
            ))
    return recs


def reserved_instances(ctx: Context) -> list[Rec]:
    s = get_settings()
    recs = []
    costs = ctx.resource_costs(
        "c.service_name = 'Virtual Machines' AND c.resource_type = 'microsoft.compute/virtualmachines' AND c.pricing_model = 'OnDemand'"
    )
    inv = {r["resource_id"]: r for r in ctx.inventory("microsoft.compute/virtualmachines")}
    for rid, cost in costs.items():
        if cost["has_commitment"] or cost["days"] < 28:
            continue
        coverage = cost["hours"] / (24 * WINDOW_DAYS)
        daily = ctx.daily_costs(rid)
        cv = statistics.pstdev(daily) / statistics.fmean(daily) if len(daily) > 1 and statistics.fmean(daily) else 1
        if coverage < 0.95 or cv > 0.1:
            continue
        sku = (inv.get(rid) or {}).get("sku") or cost["meter_name"]
        region = cost["location"]
        payg, ri1, ri3 = (ctx.price(sku, region, p) for p in ("payg", "reservation_1y", "reservation_3y"))
        if payg and ri1 and ri3:
            s1, s3 = (payg - ri1) * 730, (payg - ri3) * 730
            basis = "Azure retail prices (reservation vs pay-as-you-go)"
        else:
            s1, s3 = cost["monthly_cost"] * s.ri_discount_1y, cost["monthly_cost"] * s.ri_discount_3y
            basis = f"assumed discounts {s.ri_discount_1y:.0%} (1-yr) / {s.ri_discount_3y:.0%} (3-yr); refresh price catalog for exact rates"
        windows = "windows" in (cost["meter_subcategory"] or "").lower()
        evidence = {
            "window": f"{ctx.window_start.isoformat()} to {ctx.as_of.isoformat()}",
            "running_hours": _r(cost["hours"]),
            "hours_coverage_pct": _r(100 * coverage),
            "daily_cost_cv": round(cv, 4),
            "monthly_on_demand_cost": _r(cost["monthly_cost"]),
            "size": sku,
            "saving_1y_monthly": _r(s1),
            "saving_3y_monthly": _r(s3),
            "pricing_basis": basis,
        }
        action = (f"{cost['name']} ran {100 * coverage:.0f}% of hours at steady cost. Purchase a 1-year reservation "
                  f"(or 3-year for ~{_r(s3)}/month) for {sku} in {region}. Apply any right-sizing first.")
        if windows:
            action += " Reservations cover compute only; also confirm Azure Hybrid Benefit for the Windows licence."
        recs.append(Rec(
            "reserved_instance", "ongoing", rid, cost["name"], cost["account_id"],
            f"Reserve steady 24×7 VM {cost['name']}", action, evidence, _r(s1),
            "high" if payg else "medium", "low", "low", variant=sku or "",
        ))
    return recs


# ---- idle / orphaned ---------------------------------------------------------------------------

def idle_resources(ctx: Context) -> list[Rec]:
    recs = []
    costs = ctx.resource_costs()

    for d in ctx.inventory("microsoft.compute/disks"):
        props = d["properties"]
        state = props.get("diskState")
        if state == "Unattached" or (state is None and not props.get("managedBy")):
            c = costs.get(d["resource_id"], {})
            recs.append(Rec(
                "orphaned_disk", "immediate", d["resource_id"], d["name"], d["account_id"],
                f"Unattached managed disk {d['name']}",
                "Disk is not attached to any VM. Snapshot it if the data is needed, then delete the disk.",
                {"disk_state": state or "no managedBy", "sku": d["sku"], "size_gb": props.get("diskSizeGB"),
                 "monthly_cost": _r(c.get("monthly_cost", 0))},
                _r(c.get("monthly_cost", 0)), "high", "low", "low",
            ))

    for ip in ctx.inventory("microsoft.network/publicipaddresses"):
        props = ip["properties"]
        if not props.get("ipConfiguration") and not props.get("natGateway"):
            c = costs.get(ip["resource_id"], {})
            recs.append(Rec(
                "unused_public_ip", "immediate", ip["resource_id"], ip["name"], ip["account_id"],
                f"Unassociated public IP {ip['name']}",
                "Public IP is not associated with any NIC, load balancer or NAT gateway. Release it if not reserved for a planned use.",
                {"ip_address": props.get("ipAddress"), "allocation": props.get("publicIPAllocationMethod"),
                 "monthly_cost": _r(c.get("monthly_cost", 0))},
                _r(c.get("monthly_cost", 0)), "high", "low", "low",
            ))

    for vm in ctx.inventory("microsoft.compute/virtualmachines"):
        power = (((vm["properties"].get("extended") or {}).get("instanceView") or {}).get("powerState") or {}).get("code")
        if power == "PowerState/stopped":
            c = costs.get(vm["resource_id"], {})
            recs.append(Rec(
                "stopped_vm_allocated", "immediate", vm["resource_id"], vm["name"], vm["account_id"],
                f"VM {vm['name']} is stopped but still allocated",
                "Stopped from inside the OS, so compute is still billed. Stop (deallocate) it from the portal/CLI.",
                {"power_state": power, "monthly_compute_cost": _r(c.get("monthly_cost", 0))},
                _r(c.get("monthly_cost", 0)), "high", "low", "low",
            ))

    cutoff = datetime.combine(ctx.as_of - timedelta(days=90), datetime.min.time())
    for snap in ctx.inventory("microsoft.compute/snapshots"):
        created = snap["created_time"]
        if created and created < cutoff:
            c = costs.get(snap["resource_id"], {})
            recs.append(Rec(
                "old_snapshot", "short_term", snap["resource_id"], snap["name"], snap["account_id"],
                f"Snapshot {snap['name']} is older than 90 days",
                "Confirm retention requirements; delete if superseded by backup-vault recovery points.",
                {"created": created.isoformat(), "monthly_cost": _r(c.get("monthly_cost", 0))},
                _r(c.get("monthly_cost", 0)), "medium", "low", "low",
            ))

    groups = ctx.db.query(
        "SELECT g.resource_id, g.name, g.account_id FROM resources g WHERE g.tenant_id = ? AND g.removed_at IS NULL "
        "AND g.type = 'microsoft.resources/subscriptions/resourcegroups' AND NOT EXISTS ("
        "SELECT 1 FROM resources r WHERE r.tenant_id = g.tenant_id AND r.removed_at IS NULL AND r.account_id = g.account_id "
        "AND r.resource_group = lower(g.name) AND r.type <> g.type)",
        [ctx.tenant_id],
    )
    for g in groups:
        recs.append(Rec(
            "empty_resource_group", "ongoing", g["resource_id"], g["name"], g["account_id"],
            f"Empty resource group {g['name']}",
            "Resource group contains no resources. Delete it to keep the subscription tidy (no direct saving).",
            {"resources": 0}, 0.0, "high", "low", "low",
        ))
    return recs


# ---- SQL and disk tiers -------------------------------------------------------------------------

# Relative monthly list prices (USD) — only ratios are used, so the currency does not matter.
SQL_DTU_TIERS = [  # (tier, dtu, relative price)
    ("Basic", 5, 4.99), ("S0", 10, 14.72), ("S1", 20, 29.43), ("S2", 50, 73.58), ("S3", 100, 147.17),
    ("S4", 200, 294.33), ("S6", 400, 588.67), ("S7", 800, 1177.33), ("S9", 1600, 2354.67), ("S12", 3000, 4415.0),
]
PREMIUM_DTU_TIERS = [("P1", 125, 456.25), ("P2", 250, 912.5), ("P4", 500, 1825.0), ("P6", 1000, 3650.0),
                     ("P11", 1750, 6868.0), ("P15", 4000, 15698.0)]


def _sql_tier(sku: str | None, meter: str | None) -> str | None:
    for s in (sku, meter):
        if not s:
            continue
        m = re.search(r"\b(Basic|S\d+|P\d+)\b", s, re.IGNORECASE)
        if m:
            t = m.group(1)
            return "Basic" if t.lower() == "basic" else t.upper()
    return None


def sql_tier_review(ctx: Context) -> list[Rec]:
    recs = []
    costs = ctx.resource_costs("c.resource_type = 'microsoft.sql/servers/databases'")
    inv = {r["resource_id"]: r for r in ctx.inventory("microsoft.sql/servers/databases")}
    for rid, cost in costs.items():
        dtu = ctx.metrics(rid, "dtu_consumption_percent")
        if len(dtu) < MIN_METRIC_DAYS:
            continue
        tier = _sql_tier((inv.get(rid) or {}).get("sku"), cost["meter_name"])
        ladder = SQL_DTU_TIERS if tier and not tier.startswith("P") else PREMIUM_DTU_TIERS
        current = next((t for t in ladder if t[0] == tier), None)
        if not current:
            continue
        avg = statistics.fmean(m["avg"] for m in dtu if m["avg"] is not None)
        p95 = _p95([m["max"] for m in dtu if m["max"] is not None])
        needed = current[1] * p95 / 100 / 0.7  # keep 30% headroom over the observed peak
        target = next((t for t in ladder if t[1] >= needed), current)
        evidence = {"window": f"{ctx.window_start.isoformat()} to {ctx.as_of.isoformat()}", "metric_days": len(dtu),
                    "tier": tier, "dtu_avg_pct": _r(avg), "dtu_p95_daily_max_pct": _r(p95),
                    "monthly_cost": _r(cost["monthly_cost"])}
        if target[0] == current[0]:
            continue
        saving = cost["monthly_cost"] * (1 - target[2] / current[2])
        action = (f"DTU use averages {avg:.1f}% with a 95th-percentile peak of {p95:.1f}% of {tier} ({current[1]} DTU). "
                  f"Move to {target[0]} ({target[1]} DTU).")
        if target[0] == "Basic":
            action += " Basic caps database size at 2 GB — confirm size first; otherwise consider vCore serverless."
        elif avg < 5:
            action += " Usage is sporadic: vCore serverless with auto-pause may be cheaper still."
        recs.append(Rec("sql_tier", "short_term", rid, cost["name"], cost["account_id"],
                        f"Lower SQL Database tier for {cost['name']} ({tier} → {target[0]})", action,
                        evidence | {"target_tier": target[0]}, _r(saving), "medium", "low", "medium", variant=target[0]))
    return recs


PREMIUM_DISK = {"P4": (120, 5.28), "P6": (240, 10.21), "P10": (500, 19.71), "P15": (1100, 38.02), "P20": (2300, 73.22),
                "P30": (5000, 135.17), "P40": (7500, 259.05), "P50": (7500, 495.57)}
STANDARD_SSD_FOR = {"P4": ("E4", 2.4), "P6": ("E6", 4.8), "P10": ("E10", 9.6), "P15": ("E15", 19.2), "P20": ("E20", 38.4),
                    "P30": ("E30", 76.8), "P40": ("E40", 153.6), "P50": ("E50", 307.2)}


def disk_tier_review(ctx: Context) -> list[Rec]:
    recs = []
    costs = ctx.resource_costs()
    for d in ctx.inventory("microsoft.compute/disks"):
        tier = (d["properties"].get("tier") or "").upper()
        if tier not in PREMIUM_DISK:
            continue
        reads = {m["day"]: m for m in ctx.metrics(d["resource_id"], "Composite Disk Read Operations/sec")}
        writes = {m["day"]: m for m in ctx.metrics(d["resource_id"], "Composite Disk Write Operations/sec")}
        days = sorted(set(reads) & set(writes))
        if len(days) < MIN_METRIC_DAYS:
            continue
        peak = [(reads[x]["max"] or 0) + (writes[x]["max"] or 0) for x in days]
        avg = statistics.fmean((reads[x]["avg"] or 0) + (writes[x]["avg"] or 0) for x in days)
        p95 = _p95(peak)
        provisioned, rel_price = PREMIUM_DISK[tier]
        std_tier, std_price = STANDARD_SSD_FOR[tier]
        if p95 >= 400:  # Standard SSD sustains 500 IOPS on these sizes; keep 20% headroom
            continue
        monthly = costs.get(d["resource_id"], {}).get("monthly_cost", 0)
        saving = monthly * (1 - std_price / rel_price)
        recs.append(Rec(
            "disk_tier", "short_term", d["resource_id"], d["name"], d["account_id"],
            f"Move disk {d['name']} from Premium {tier} to Standard SSD {std_tier}",
            f"IOPS 95th-percentile peak {p95:.0f} vs {provisioned} provisioned ({100 * p95 / provisioned:.0f}%). "
            f"Standard SSD {std_tier} (up to 500 IOPS) covers this; expect somewhat higher latency. Requires the VM to be deallocated.",
            {"window": f"{ctx.window_start.isoformat()} to {ctx.as_of.isoformat()}", "metric_days": len(days), "tier": tier,
             "provisioned_iops": provisioned, "iops_avg": _r(avg), "iops_p95_daily_max": _r(p95), "monthly_cost": _r(monthly)},
            _r(saving), "medium", "low", "medium", variant=std_tier,
        ))
    return recs


RULES = [vm_rightsizing, reserved_instances, idle_resources, sql_tier_review, disk_tier_review]
