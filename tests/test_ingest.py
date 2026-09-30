from datetime import date

import pytest

from cloudarc.ingest.loader import IngestError, ingest_files, set_fx_rate
from tests.conftest import write_csv

EA = ["SubscriptionId", "SubscriptionName", "Date", "ResourceGroup", "ResourceId", "MeterCategory", "MeterSubCategory",
      "MeterName", "ResourceLocation", "Quantity", "UnitOfMeasure", "EffectivePrice", "CostInBillingCurrency",
      "BillingCurrency", "PricingModel", "ChargeType", "Tags"]
VM = "/subscriptions/sub-1/resourceGroups/RG-App/providers/Microsoft.Compute/virtualMachines/vm1"
DB = "/subscriptions/sub-1/resourceGroups/rg-app/providers/Microsoft.Sql/servers/s1/databases/db1"


def ea_rows(day: str, vm_cost: float = 100.0):
    return [
        ["sub-1", "Prod", day, "RG-App", VM, "Virtual Machines", "BS Series", "B4als v2", "Central India", 24, "1 Hour", 4, vm_cost,
         "INR", "OnDemand", "Usage", '"Environment": "Production","Owner": "ops"'],
        ["sub-1", "Prod", day, "rg-app", DB, "SQL Database", "Standard - S0", "S0 DTUs", "Central India", 1, "1/Day", 50, 50.0,
         "INR", "OnDemand", "Usage", ""],
    ]


def test_azure_ea_normalization(db, tmp_path):
    f = write_csv(tmp_path / "a.csv", EA, ea_rows("06/01/2026"))
    res = ingest_files(db, "t1", [f])
    assert res.provider == "azure" and res.rows_loaded == 2 and res.reconciled
    rows = {r["resource_name"]: r for r in db.query("SELECT *, CAST(tags AS VARCHAR) AS t FROM cost_records")}
    vm = rows["vm1"]
    assert vm["resource_group"] == "rg-app"  # Azure RG casing varies across rows; normalized
    assert vm["resource_type"] == "microsoft.compute/virtualmachines"
    assert vm["location"] == "centralindia"
    assert vm["charge_date"] == date(2026, 6, 1)
    assert '"Environment"' in vm["t"] and '"Production"' in vm["t"]  # brace-less Azure tags parsed
    assert rows["db1"]["resource_type"] == "microsoft.sql/servers/databases"
    assert db.scalar("SELECT count(*) FROM cloud_accounts WHERE tenant_id = 't1'") == 1  # auto-discovered


def test_azure_mca_camelcase_columns_and_fx(db, tmp_path):
    header = ["subscriptionId", "subscriptionName", "date", "resourceGroupName", "ResourceId", "meterCategory",
              "meterSubCategory", "meterName", "resourceLocation", "quantity", "unitOfMeasure", "effectivePrice",
              "costInBillingCurrency", "billingCurrency", "pricingModel", "chargeType", "tags"]
    f = write_csv(tmp_path / "m.csv", header, [["SUB-2", "Dev", "2026-06-03", "rg", VM, "Virtual Machines", "", "D2s v5",
                                                "centralindia", 24, "1 Hour", 1, 12.5, "USD", "OnDemand", "Usage", '{"a": "b"}']])
    set_fx_rate(db, "USD", "2026-01-01", 85.0)
    set_fx_rate(db, "USD", "2026-06-02", 86.0)
    ingest_files(db, "t1", [f])
    assert db.one("SELECT external_id FROM cloud_accounts")["external_id"] == "sub-2"
    assert db.scalar("SELECT cost_base FROM cost_records") == pytest.approx(12.5 * 86.0)  # as-of FX rate


def test_reingest_is_idempotent_and_restatements_replace(db, tmp_path):
    f1 = write_csv(tmp_path / "1.csv", EA, ea_rows("06/01/2026") + ea_rows("06/02/2026"))
    ingest_files(db, "t1", [f1])
    ingest_files(db, "t1", [f1])
    assert db.scalar("SELECT count(*) FROM cost_records") == 4
    # Azure restates 2 June: only that day is replaced; 1 June is untouched.
    f2 = write_csv(tmp_path / "2.csv", EA, ea_rows("06/02/2026", vm_cost=130.0))
    ingest_files(db, "t1", [f2])
    by_day = {r["d"]: r["c"] for r in db.query("SELECT charge_date AS d, SUM(cost) AS c FROM cost_records GROUP BY 1")}
    assert by_day[date(2026, 6, 1)] == pytest.approx(150.0)
    assert by_day[date(2026, 6, 2)] == pytest.approx(180.0)
    assert db.scalar("SELECT count(*) FROM cost_records") == 4


def test_tenants_do_not_overwrite_each_other(db, tmp_path):
    f = write_csv(tmp_path / "1.csv", EA, ea_rows("06/01/2026"))
    ingest_files(db, "t1", [f])
    ingest_files(db, "t2", [f])
    assert db.scalar("SELECT count(*) FROM cost_records WHERE tenant_id = 't1'") == 2
    assert db.scalar("SELECT count(*) FROM cost_records WHERE tenant_id = 't2'") == 2


def test_aws_cur_amortized(db, tmp_path):
    header = ["lineItem/UsageAccountId", "lineItem/UsageStartDate", "lineItem/ProductCode", "product/ProductName",
              "lineItem/ResourceId", "lineItem/LineItemType", "lineItem/UsageType", "lineItem/UsageAmount", "pricing/unit",
              "lineItem/UnblendedCost", "lineItem/CurrencyCode", "product/regionCode", "reservation/EffectiveCost",
              "savingsPlan/SavingsPlanEffectiveCost", "resourceTags/user:Team"]
    rows = [
        ["111", "2026-06-01T00:00:00Z", "AmazonEC2", "Amazon EC2", "i-1", "Usage", "BoxUsage:m5.large", 24, "Hrs", 2.4, "USD", "ap-south-1", "", "", "web"],
        ["111", "2026-06-01T00:00:00Z", "AmazonEC2", "Amazon EC2", "i-2", "DiscountedUsage", "BoxUsage:m5.large", 24, "Hrs", 0, "USD", "ap-south-1", 1.5, "", ""],
        ["111", "2026-06-01T00:00:00Z", "AmazonEC2", "Amazon EC2", "i-3", "SavingsPlanCoveredUsage", "BoxUsage", 24, "Hrs", 2.0, "USD", "ap-south-1", "", 1.2, ""],
        ["111", "2026-06-01T00:00:00Z", "AmazonEC2", "Amazon EC2", "i-3", "SavingsPlanNegation", "BoxUsage", 0, "Hrs", -2.0, "USD", "ap-south-1", "", "", ""],
        ["111", "2026-06-01T00:00:00Z", "AmazonRDS", "Amazon RDS", "arn:aws:rds:ap-south-1:111:db:x", "Usage", "InstanceUsage", 24, "Hrs", 4.0, "USD", "ap-south-1", "", "", ""],
    ]
    res = ingest_files(db, "t1", [write_csv(tmp_path / "cur.csv", header, rows)])
    assert res.provider == "aws" and res.reconciled
    assert res.loaded_total == pytest.approx(2.4 + 1.5 + 1.2 + 0 + 4.0)
    pm = {r["resource_id"]: r for r in db.query("SELECT resource_id, pricing_model, resource_type, CAST(tags AS VARCHAR) AS t FROM cost_records")}
    assert pm["i-2"]["pricing_model"] == "Reservation"
    assert pm["arn:aws:rds:ap-south-1:111:db:x"]["resource_type"] == "rds/db"
    assert pm["i-1"]["t"] == '{"Team":"web"}'


def test_gcp_export_with_credits(db, tmp_path):
    header = ["billing_account_id", "service.description", "sku.description", "usage_start_time", "project.id", "project.name",
              "labels", "location.region", "cost", "currency", "usage.amount", "usage.unit", "credits"]
    rows = [["B-1", "Compute Engine", "N2 Core", "2026-06-01 00:00:00 UTC", "proj", "Project", '[{"key":"env","value":"prod"}]',
             "asia-south1", 10.0, "INR", 24, "hour", '[{"name":"SUD","amount":-2.5}]']]
    res = ingest_files(db, "t1", [write_csv(tmp_path / "g.csv", header, rows)])
    assert res.provider == "gcp" and res.loaded_total == pytest.approx(7.5)
    assert db.scalar("SELECT json_extract_string(tags, '$.env') FROM cost_records") == "prod"


def test_unrecognized_file_is_rejected(db, tmp_path):
    with pytest.raises(IngestError):
        ingest_files(db, "t1", [write_csv(tmp_path / "x.csv", ["a", "b"], [[1, 2]])])
    assert db.scalar("SELECT status FROM ingestion_runs") == "failed"
