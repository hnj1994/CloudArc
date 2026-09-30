"""Synthetic demo estate.

Two Azure tenants modelled on the shapes seen during the CloudSpend evaluation
(a small single-VM + SQL production subscription, and a two-region
production/DR estate with a pricing-calculator BOQ), plus a small AWS tenant.
All names, IDs and figures are synthetic. The data goes through the real
ingestion pipeline as provider-format CSV exports.
"""
from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from . import budgets, tenants
from .analytics import allocation
from .db import Database
from .ingest.loader import ingest_files
from .inventory import upsert_metrics, upsert_resources
from .recommendations import engine
from .reports.boq import import_boq_rows
from .security import auth

AZ_HEADER = ["SubscriptionId", "SubscriptionName", "Date", "ResourceGroup", "ResourceId", "MeterCategory", "MeterSubCategory",
             "MeterName", "ResourceLocation", "Quantity", "UnitOfMeasure", "EffectivePrice", "CostInBillingCurrency",
             "BillingCurrency", "PricingModel", "ChargeType", "Tags"]


def _rid(sub: str, rg: str, provider: str, *parts: str) -> str:
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/" + "/".join(parts)


def _days(d0: date, d1: date):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)


def _tags(t: dict | None) -> str:
    return ",".join(f'"{k}": "{v}"' for k, v in (t or {}).items())  # Azure export style (no braces)


def _write_azure(path: Path, rows: list[list]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(AZ_HEADER)
        w.writerows(rows)


def _monthly_to_daily(monthly: float, d: date, rng: random.Random, jitter: float) -> float:
    import calendar

    base = monthly / calendar.monthrange(d.year, d.month)[1]
    return round(base * (1 + rng.uniform(-jitter, jitter)), 4)


# ---------------------------------------------------------------------------------------------
# Tenant A: single production subscription (VM + SQL), stable daily spend
# ---------------------------------------------------------------------------------------------

def _tenant_a(db: Database, out: Path, as_of: date, rng: random.Random) -> str:
    tid = tenants.create_tenant(db, "Demo Client A – Analytics App", tenant_id="demo-a")
    sub, rg, loc = "0f6d2a1c-1111-4c2e-9a51-3b1d7e000001", "app-prod-rg", "Central India"
    tags = {"Environment": "Production", "Department": "IT", "Application": "Analytics-App", "Owner": "Cloud Operations"}
    vm = _rid(sub, rg, "Microsoft.Compute", "virtualMachines", "app-prod-vm01")
    disk = _rid(sub, rg, "Microsoft.Compute", "disks", "app-prod-vm01_OsDisk_1")
    old_disk = _rid(sub, rg, "Microsoft.Compute", "disks", "app-prod-vm01_datadisk_old")
    sqlsrv = _rid(sub, rg, "Microsoft.Sql", "servers", "app-prod-sqlsrv01")
    sqldb = sqlsrv + "/databases/app-prod-db001"
    pe = _rid(sub, rg, "Microsoft.Network", "privateEndpoints", "app-prod-endpoint01")
    pip = _rid(sub, rg, "Microsoft.Network", "publicIPAddresses", "app-prod-vm01-ip")
    dns = _rid(sub, rg, "Microsoft.Network", "privateDnsZones", "privatelink.database.windows.net")
    # (resource, category, subcategory, meter, unit, qty/day, monthly cost, tags)
    lines = [
        (vm, "Virtual Machines", "BS Series", "B4als v2", "1 Hour", 24, 7303.08, tags),
        (sqldb, "SQL Database", "Standard - S0", "S0 DTUs", "1/Day", 1, 1589.26, tags),
        (disk, "Storage", "Standard HDD Managed Disks", "S15 LRS Disk", "1/Month", 1 / 30, 1192.50, tags),
        (pe, "Virtual Network", "Private Link", "Standard Private Endpoint", "1 Hour", 24, 688.99, tags),
        (pip, "Virtual Network", "IP Addresses", "Standard IPv4 Static Public IP", "1 Hour", 24, 344.49, tags),
        (disk, "Storage", "Standard HDD Managed Disks", "S4 LRS Disk Operations", "10K", 3.1, 76.01, tags),
        (dns, "Azure DNS", "Private", "Private Zone", "1/Month", 1 / 30, 46.30, None),
        (vm, "Bandwidth", "Rtn Preference: MGN", "Standard Data Transfer Out", "1 GB", 1.1, 23.32, tags),
        (vm, "Bandwidth", "Inter-Region", "Inter-Continent Data Transfer Out (ASIA to Any)", "1 GB", 0.05, 0.48, tags),
    ]
    rows = []
    start = date(as_of.year, 6, 1)
    for d in _days(start, as_of):
        for rid, cat, sub_cat, meter, unit, qty, monthly, t in lines:
            cost = _monthly_to_daily(monthly, d, rng, 0.004)
            if meter == "Standard Data Transfer Out" and d == date(as_of.year, 9, 18):
                cost = 412.75  # egress spike -> anomaly demo
            rows.append([sub, "Production", d.strftime("%m/%d/%Y"), rg.upper() if rng.random() < 0.3 else rg, rid, cat, sub_cat,
                         meter, loc, round(qty, 4), unit, round(cost / qty, 6) if qty else 0, cost, "INR", "OnDemand", "Usage", _tags(t)])
        if d >= date(as_of.year, 8, 1):
            rows.append([sub, "Production", d.strftime("%m/%d/%Y"), rg, old_disk, "Storage", "Standard HDD Managed Disks",
                         "S10 LRS Disk", loc, round(1 / 30, 4), "1/Month", 0, 5.02, "INR", "OnDemand", "Usage", _tags(tags)])
    path = out / "demo_a_azure_costdetails.csv"
    _write_azure(path, rows)
    ingest_files(db, tid, [path], source="demo")

    account = db.scalar("SELECT id FROM cloud_accounts WHERE tenant_id = ?", [tid])
    db.execute("UPDATE cloud_accounts SET permission_status = 'ok', last_sync_status = 'succeeded', last_sync_at = now() WHERE id = ?", [account])
    rgid = f"/subscriptions/{sub}/resourceGroups/{rg}"

    def res(rid, rtype, sku=None, props=None, location="centralindia", t=tags, managed_by=None):
        return {"id": rid, "name": rid.rsplit("/", 1)[-1], "type": rtype, "resourceGroup": rg, "location": location,
                "sku": {"name": sku} if sku else None, "tags": t, "properties": props or {}, "managedBy": managed_by}

    inv = [
        res(vm, "Microsoft.Compute/virtualMachines", props={"hardwareProfile": {"vmSize": "Standard_B4als_v2"},
            "extended": {"instanceView": {"powerState": {"code": "PowerState/running"}}}}),
        res(disk, "Microsoft.Compute/disks", "Standard_LRS", {"diskState": "Attached", "diskSizeGB": 256, "tier": "S15"}, managed_by=vm),
        res(old_disk, "Microsoft.Compute/disks", "Standard_LRS", {"diskState": "Unattached", "diskSizeGB": 128, "tier": "S10",
            "timeCreated": "2026-03-02T10:00:00Z"}),
        res(sqlsrv, "Microsoft.Sql/servers"),
        res(sqldb, "Microsoft.Sql/servers/databases", "S0"),
        res(pe, "Microsoft.Network/privateEndpoints"),
        res(pe.replace("privateEndpoints/app-prod-endpoint01", "networkInterfaces/app-prod-endpoint01.nic"), "Microsoft.Network/networkInterfaces"),
        res(pip, "Microsoft.Network/publicIPAddresses", "Standard", {"ipAddress": "20.0.0.10", "publicIPAllocationMethod": "Static",
            "ipConfiguration": {"id": "nic-ipconfig"}}),
        res(_rid(sub, rg, "Microsoft.Network", "networkSecurityGroups", "app-prod-vm01-nsg"), "Microsoft.Network/networkSecurityGroups"),
        res(_rid(sub, rg, "Microsoft.Network", "virtualNetworks", "app-prod-vm01-vnet"), "Microsoft.Network/virtualNetworks"),
        res(_rid(sub, rg, "Microsoft.Network", "networkInterfaces", "app-prod-vm01197"), "Microsoft.Network/networkInterfaces"),
        res(_rid(sub, rg, "Microsoft.Network", "networkWatchers", "NetworkWatcher_centralindia"), "Microsoft.Network/networkWatchers"),
        res(dns, "Microsoft.Network/privateDnsZones", location="global", t={}),
        {"id": rgid, "name": rg, "type": "microsoft.resources/subscriptions/resourcegroups", "resourceGroup": rg,
         "location": "centralindia", "tags": tags, "properties": {}},
    ]
    upsert_resources(db, tid, account, inv)

    metrics = []
    for d in _days(as_of - timedelta(days=29), as_of):
        metrics += [
            {"resource_id": vm, "metric": "Percentage CPU", "day": d, "avg": rng.uniform(8, 13), "max": rng.uniform(22, 34), "min": 2},
            {"resource_id": vm, "metric": "Available Memory Bytes", "day": d, "avg": 4.6 * 1024 ** 3, "max": 5.5 * 1024 ** 3, "min": 3.9 * 1024 ** 3},
            {"resource_id": vm, "metric": "CPU Credits Remaining", "day": d, "avg": 520, "max": 576, "min": 410},
            {"resource_id": sqldb, "metric": "dtu_consumption_percent", "day": d, "avg": rng.uniform(5, 9), "max": rng.uniform(16, 24), "min": 0},
        ]
    upsert_metrics(db, tid, metrics)
    budgets.create_budget(db, tid, name="Production", scope_type="tenant", scope_value=None, period="monthly",
                          amount=7117.89, thresholds=[80, 100], created_by="demo")
    return tid


# ---------------------------------------------------------------------------------------------
# Tenant B: two-region production + DR estate with a BOQ
# ---------------------------------------------------------------------------------------------

B_COMPONENTS = [
    # (resource name, provider/type, category, subcategory, meter, region, unit, qty/day, monthly, pricing)
    ("hp-appgw01", "Microsoft.Network/applicationGateways", "Application Gateway", "WAF v2", "Standard Capacity Units", "Central India", "1/Hour", 192, 44646.29, "OnDemand"),
    ("hp-appgw01-pip", "Microsoft.Network/publicIPAddresses", "Azure DDOS Protection", "DDoS IP Protection", "Protected IP", "Central India", "1/Hour", 24, 18807.84, "OnDemand"),
    ("hp-app-vm01", "Microsoft.Compute/virtualMachines", "Azure Monitor", "Alerts", "Metric Monitored", "Central India", "1/Month", 1 / 30, 138.20, "OnDemand"),
    ("hp-rsv-dr", "Microsoft.RecoveryServices/vaults", "Azure Site Recovery", "", "VM Replicated to Azure", "South India", "1/Month", 2 / 30, 4630.28, "OnDemand"),
    ("hp-rsv-backup", "Microsoft.RecoveryServices/vaults", "Backup", "", "Azure VM Protected Instances", "Central India", "1/Month", 2 / 30, 15162.41, "OnDemand"),
    ("hp-db-vm01", "Microsoft.Compute/virtualMachines", "Bandwidth", "Inter-Region", "Inter-Region Data Transfer Out", "Central India", "1 GB", 180, 10545.83, "OnDemand"),
    ("hparchivestore", "Microsoft.Storage/storageAccounts", "Storage", "Tiered Block Blob", "Hot LRS Data Stored", "Central India", "1 GB/Month", 3000 / 30, 2353.74, "OnDemand"),
    ("hp-db-vm01-pip", "Microsoft.Network/publicIPAddresses", "Virtual Network", "IP Addresses", "Standard IPv4 Static Public IP", "Central India", "1 Hour", 96, 1377.97, "OnDemand"),
    ("hp-app-vm01_OsDisk", "Microsoft.Compute/disks", "Storage", "Premium SSD Managed Disks", "P15 LRS Disk", "Central India", "1/Month", 1 / 30, 3457.39, "OnDemand"),
    ("hp-app-vm01_data01", "Microsoft.Compute/disks", "Storage", "Premium SSD Managed Disks", "P20 LRS Disk", "Central India", "1/Month", 1 / 30, 6659.73, "OnDemand"),
    ("hp-db-vm01_OsDisk", "Microsoft.Compute/disks", "Storage", "Premium SSD Managed Disks", "P15 LRS Disk", "Central India", "1/Month", 1 / 30, 3457.39, "OnDemand"),
    ("hp-db-vm01_data01", "Microsoft.Compute/disks", "Storage", "Premium SSD Managed Disks", "P20 LRS Disk", "Central India", "1/Month", 1 / 30, 6659.73, "OnDemand"),
    ("hp-db-vm01_data02", "Microsoft.Compute/disks", "Storage", "Premium SSD Managed Disks", "P15 LRS Disk", "Central India", "1/Month", 1 / 30, 3457.39, "OnDemand"),
    ("hp-snap-weekly", "Microsoft.Compute/snapshots", "Storage", "Premium SSD Managed Disks", "LRS Snapshots", "Central India", "1 GB/Month", 900 / 30, 6462.58, "OnDemand"),
    ("hp-asr-replica-disks", "Microsoft.Compute/disks", "Storage", "Premium SSD Managed Disks", "P20 LRS Disk", "South India", "1/Month", 4 / 30, 27332.66, "OnDemand"),
    ("hp-appgw01", "Microsoft.Network/applicationGateways", "Virtual Network", "Rtn Preference: MGN", "Data Transfer Out", "Central India", "1 GB", 900, 5298.80, "OnDemand"),
    ("hpasrcache", "Microsoft.Storage/storageAccounts", "Storage", "Standard Page Blob v2", "LRS Data Stored", "Central India", "1 GB/Month", 2500 / 30, 5423.70, "OnDemand"),
    ("hp-db-vm01", "Microsoft.Compute/virtualMachines", "Virtual Machines", "Easv5 Series", "E16as v5", "Central India", "1 Hour", 24, 39405.44, "OnDemand"),
    ("hp-app-vm01", "Microsoft.Compute/virtualMachines", "Virtual Machines", "Esv5 Series", "E8s v5", "Central India", "1 Hour", 24, 21222.89, "Reservation"),
    ("hp-vpngw01", "Microsoft.Network/virtualNetworkGateways", "VPN Gateway", "VpnGw1AZ", "VpnGw1AZ", "Central India", "1 Hour", 24, 14468.71, "OnDemand"),
]

B_BOQ = [
    ("Compute", "Virtual Machines", "App VM", "Central India", "1 E8s v5 (8 vCPUs, 64 GB RAM) (1 year reserved)", 20373.92),
    ("Storage", "Managed Disks", "OS disk - 256 GB", "Central India", "Premium SSD, LRS, P15", 3457.39),
    ("Storage", "Managed Disks", "Data disk - 512 GB", "Central India", "Premium SSD, LRS, P20", 6659.73),
    ("Storage", "Managed Disks", "Data disk - 256 GB", "Central India", "Premium SSD, LRS, P15", 3457.39),
    ("Compute", "Virtual Machines", "DB VM", "Central India", "1 E16as v5 (16 vCPUs, 128 GB RAM) x 730 Hours (Pay as you go)", 37979.17),
    ("Storage", "Managed Disks", "OS disk - 256 GB", "Central India", "Premium SSD, LRS, P15", 3457.39),
    ("Storage", "Managed Disks", "Data disk - 512 GB", "Central India", "Premium SSD, LRS, P20", 6659.73),
    ("Storage", "Managed Disks", "Data disk - 256 GB", "Central India", "Premium SSD, LRS, P15", 3457.39),
    ("Networking", "VPN Gateway", "", "Central India", "VpnGw1AZ tier, 730 gateway hours", 13943.40),
    ("Networking", "IP Addresses", "4 IPs", "Central India", "Standard (ARM), 4 Static IP Addresses", 1327.94),
    ("Management and Governance", "Azure Backup", "App VM backup", "Central India", "Enhanced policy, 50 GB", 909.43),
    ("Management and Governance", "Azure Backup", "DB VM backup", "Central India", "Enhanced policy, 200 GB", 2728.18),
    ("Networking", "Azure DDoS Protection", "DDoS for App VM", "Central India", "IP Protection, 1 resource", 18099.86),
    ("Networking", "Application Gateway", "WAF", "Central India", "WAF V2, 1 compute unit", 34420.28),
    ("Management and Governance", "Azure Site Recovery", "ASR", "South India", "2 Azure instances", 4547.75),
    ("Storage", "Managed Disks", "ASR replica disks", "South India", "Premium SSD, LRS, P15/P20 x 2", 24434.12),
    ("Networking", "Bandwidth", "", "Central India", "Internet egress, 101 GB", 10.91),
]


def _tenant_b(db: Database, out: Path, as_of: date, rng: random.Random) -> str:
    tid = tenants.create_tenant(db, "Demo Client B – Healthcare Portal", tenant_id="demo-b")
    sub, rg_prod, rg_dr = "0f6d2a1c-2222-4c2e-9a51-3b1d7e000002", "hp-prod-rg", "hp-dr-rg"
    tags = {"Environment": "Production", "Department": "Clinical-IT", "Application": "HealthPortal", "Owner": "Cloud Operations"}
    rows, ids = [], {}
    for name, rtype, *_ in B_COMPONENTS:
        prov, typ = rtype.split("/", 1)
        region_rg = rg_dr if name.startswith("hp-asr") or name == "hp-rsv-dr" else rg_prod
        ids[name] = (_rid(sub, region_rg, prov, typ, name), region_rg)
    for d in _days(date(as_of.year, 5, 1), as_of):
        for name, rtype, cat, sub_cat, meter, region, unit, qty, monthly, pricing in B_COMPONENTS:
            rid, rg = ids[name]
            cost = _monthly_to_daily(monthly, d, rng, 0.03)
            t = tags if name != "hparchivestore" else {"Environment": "Production"}
            rows.append([sub, "HP-Production", d.strftime("%m/%d/%Y"), rg, rid, cat, sub_cat, meter, region, round(qty, 4), unit,
                         round(cost / qty, 6), cost, "INR", pricing, "Usage", _tags(t)])
    path = out / "demo_b_azure_costdetails.csv"
    _write_azure(path, rows)
    ingest_files(db, tid, [path], source="demo")
    account = db.scalar("SELECT id FROM cloud_accounts WHERE tenant_id = ?", [tid])
    db.execute("UPDATE cloud_accounts SET permission_status = 'ok', last_sync_status = 'succeeded', last_sync_at = now() WHERE id = ?", [account])

    inv = []
    for name, rtype, cat, sub_cat, meter, region, *_ in B_COMPONENTS:
        rid, rg = ids[name]
        if any(i["id"] == rid for i in inv):
            continue
        props: dict = {}
        sku = None
        if rtype.endswith("virtualMachines"):
            sku = "Standard_E16as_v5" if "db" in name else "Standard_E8s_v5"
            props = {"hardwareProfile": {"vmSize": sku}, "extended": {"instanceView": {"powerState": {"code": "PowerState/running"}}}}
        elif rtype.endswith("disks"):
            tier = meter.split()[0] if meter.startswith("P") else "P20"
            props = {"diskState": "Attached", "tier": tier, "diskSizeGB": 512 if tier == "P20" else 256}
        elif rtype.endswith("snapshots"):
            props = {"timeCreated": "2026-04-20T02:00:00Z", "diskSizeGB": 512}
        elif rtype.endswith("publicIPAddresses"):
            props = {"ipConfiguration": {"id": "cfg"}, "publicIPAllocationMethod": "Static"}
        inv.append({"id": rid, "name": name, "type": rtype, "resourceGroup": rg, "location": region.lower().replace(" ", ""),
                    "sku": {"name": sku} if sku else None, "tags": tags, "properties": props})
    spare_ip = _rid(sub, rg_prod, "Microsoft.Network", "publicIPAddresses", "hp-migration-temp-ip")
    inv.append({"id": spare_ip, "name": "hp-migration-temp-ip", "type": "Microsoft.Network/publicIPAddresses", "resourceGroup": rg_prod,
                "location": "centralindia", "tags": {}, "properties": {"publicIPAllocationMethod": "Static"}})
    upsert_resources(db, tid, account, inv)

    metrics = []
    app_vm, db_vm = ids["hp-app-vm01"][0], ids["hp-db-vm01"][0]
    for d in _days(as_of - timedelta(days=29), as_of):
        metrics += [
            {"resource_id": app_vm, "metric": "Percentage CPU", "day": d, "avg": rng.uniform(6, 11), "max": rng.uniform(20, 31), "min": 1},
            {"resource_id": app_vm, "metric": "Available Memory Bytes", "day": d, "avg": 40 * 1024 ** 3, "max": 44 * 1024 ** 3, "min": 36 * 1024 ** 3},
            {"resource_id": db_vm, "metric": "Percentage CPU", "day": d, "avg": rng.uniform(32, 45), "max": rng.uniform(70, 88), "min": 12},
        ]
        for disk in ("hp-app-vm01_data01", "hp-db-vm01_data02", "hp-app-vm01_OsDisk"):
            metrics += [
                {"resource_id": ids[disk][0], "metric": "Composite Disk Read Operations/sec", "day": d, "avg": rng.uniform(15, 40), "max": rng.uniform(90, 160)},
                {"resource_id": ids[disk][0], "metric": "Composite Disk Write Operations/sec", "day": d, "avg": rng.uniform(10, 30), "max": rng.uniform(60, 120)},
            ]
        metrics += [
            {"resource_id": ids["hp-db-vm01_data01"][0], "metric": "Composite Disk Read Operations/sec", "day": d, "avg": 900, "max": 1800},
            {"resource_id": ids["hp-db-vm01_data01"][0], "metric": "Composite Disk Write Operations/sec", "day": d, "avg": 300, "max": 600},
        ]
    upsert_metrics(db, tid, metrics)

    import_boq_rows(db, tid, "Approved BOQ (March 2026)", [
        {"service_category": c, "service_type": t, "custom_name": n, "region": r, "description": desc, "monthly_cost": m}
        for c, t, n, r, desc, m in B_BOQ
    ])
    budgets.create_budget(db, tid, name="Monthly BOQ", scope_type="tenant", scope_value=None, period="monthly",
                          amount=round(sum(b[-1] for b in B_BOQ), 2), thresholds=[50, 80, 100], created_by="demo")
    budgets.create_budget(db, tid, name="DR region", scope_type="resource_group", scope_value=rg_dr, period="monthly",
                          amount=35000, thresholds=[80, 100], created_by="demo")
    allocation.create_center(db, tid, "Production workload", [{"resource_group_pattern": rg_prod}])
    allocation.create_center(db, tid, "Disaster recovery", [{"resource_group_pattern": rg_dr},
                                                            {"services": ["Azure Site Recovery"]}])
    return tid


# ---------------------------------------------------------------------------------------------
# Tenant C: AWS CUR (USD) — shows the multi-cloud model and FX conversion
# ---------------------------------------------------------------------------------------------

def _tenant_c(db: Database, out: Path, as_of: date, rng: random.Random) -> str:
    tid = tenants.create_tenant(db, "Demo Client C – Retail (AWS)", tenant_id="demo-c")
    header = ["identity/LineItemId", "bill/PayerAccountId", "lineItem/UsageAccountId", "lineItem/LineItemType", "lineItem/UsageStartDate",
              "lineItem/ProductCode", "product/ProductName", "product/productFamily", "product/regionCode", "lineItem/UsageType",
              "lineItem/Operation", "lineItem/ResourceId", "lineItem/UsageAmount", "pricing/unit", "lineItem/UnblendedRate",
              "lineItem/UnblendedCost", "lineItem/CurrencyCode", "reservation/EffectiveCost", "resourceTags/user:Environment",
              "resourceTags/user:Team"]
    items = [
        ("AmazonEC2", "Amazon Elastic Compute Cloud", "Compute Instance", "APS3-BoxUsage:m6i.xlarge", "RunInstances", "i-0a1b2c3d4e5f00001", 24, "Hrs", 0.202, "Usage", "prod", "web"),
        ("AmazonEC2", "Amazon Elastic Compute Cloud", "Compute Instance", "APS3-BoxUsage:r6i.2xlarge", "RunInstances", "i-0a1b2c3d4e5f00002", 24, "Hrs", 0.0, "DiscountedUsage", "prod", "data"),
        ("AmazonEC2", "Amazon Elastic Compute Cloud", "Storage", "APS3-EBS:VolumeUsage.gp3", "CreateVolume-Gp3", "vol-0a1b2c3d4e5f00003", 16.4, "GB-Mo", 0.0912, "Usage", "prod", "web"),
        ("AmazonRDS", "Amazon Relational Database Service", "Database Instance", "APS3-InstanceUsage:db.m6g.large", "CreateDBInstance", "arn:aws:rds:ap-south-1:210987654321:db:retail-db", 24, "Hrs", 0.198, "Usage", "prod", "data"),
        ("AmazonS3", "Amazon Simple Storage Service", "Storage", "APS3-TimedStorage-ByteHrs", "PutObject", "retail-assets-bucket", 40.0, "GB-Mo", 0.025, "Usage", "", ""),
    ]
    rows, n = [], 0
    for d in _days(date(as_of.year, 6, 1), as_of):
        for code, pname, fam, ut, op, rid, qty, unit, rate, lt, env, team in items:
            n += 1
            cost = round(qty * rate * (1 + rng.uniform(-0.02, 0.02)), 6)
            eff = round(24 * 0.33, 6) if lt == "DiscountedUsage" else ""
            rows.append([f"li{n}", "210987654321", "210987654321", lt, f"{d.isoformat()}T00:00:00Z", code, pname, fam, "ap-south-1",
                         ut, op, rid, qty, unit, rate, cost, "USD", eff, env, team])
    path = out / "demo_c_aws_cur.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    ingest_files(db, tid, [path], source="demo")
    budgets.create_budget(db, tid, name="AWS monthly", scope_type="tenant", scope_value=None, period="monthly",
                          amount=45000, thresholds=[80, 100], created_by="demo")
    return tid


def seed(db: Database, out_dir: Path, as_of: date | None = None) -> dict:
    """Create demo tenants, users and tokens. Returns the plaintext tokens (shown once)."""
    as_of = as_of or (date.today() - timedelta(days=1))
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    a = _tenant_a(db, out_dir, as_of, rng)
    b = _tenant_b(db, out_dir, as_of, rng)
    c = _tenant_c(db, out_dir, as_of, rng)
    for tid in (a, b, c):
        engine.run(db, tid, as_of)
        budgets.evaluate(db, tid, as_of, notify=False)

    admin = auth.create_user(db, "admin@cloudarc.example", "Platform Admin", is_platform_admin=True)
    analyst = auth.create_user(db, "analyst@cloudarc.example", "Cloud Engineer")
    viewer = auth.create_user(db, "delivery@cloudarc.example", "Delivery Manager")
    auth.assign_role(db, analyst, a, "analyst")
    auth.assign_role(db, analyst, c, "analyst")
    auth.assign_role(db, viewer, b, "viewer")
    tokens = {
        "admin@cloudarc.example (platform admin)": auth.issue_token(db, admin, "demo"),
        "analyst@cloudarc.example (analyst: A, C)": auth.issue_token(db, analyst, "demo"),
        "delivery@cloudarc.example (viewer: B)": auth.issue_token(db, viewer, "demo"),
    }
    (out_dir / "demo_tokens.json").write_text(json.dumps(tokens, indent=2))
    return {"tenants": [a, b, c], "as_of": as_of.isoformat(), "tokens": tokens, "generated_at": datetime.now().isoformat()}
