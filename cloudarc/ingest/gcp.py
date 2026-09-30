"""Google Cloud billing export (BigQuery detailed/standard export, flattened to CSV or Parquet).

Net cost = cost + sum(credits.amount), matching the "cost after credits" figure
shown in the Cloud Billing console.
"""
from __future__ import annotations

from .common import BillingAdapter, Columns, normalize_location


class GcpBillingAdapter(BillingAdapter):
    provider = "gcp"
    label = "GCP Cloud Billing export"

    def detect(self, cols: Columns) -> bool:
        return cols.has("billing_account_id") and cols.has("service.description", "service_description")

    def _credits(self, cols: Columns) -> str:
        c = cols.col("credits")
        if not c:
            return "0"
        return (
            f"COALESCE(list_sum(list_transform("
            f"from_json(CASE WHEN json_valid(CAST({c} AS VARCHAR)) THEN CAST({c} AS VARCHAR) ELSE '[]' END, "
            f"'[{{\"amount\":\"DOUBLE\"}}]'), x -> x.amount)), 0)"
        )

    def _cost(self, cols: Columns) -> str:
        return f"(COALESCE({cols.num('cost')}, 0) + {self._credits(cols)})"

    def source_cost_expr(self, cols: Columns) -> str:
        return self._cost(cols)

    def _tags(self, cols: Columns) -> str:
        c = cols.col("labels")
        if not c:
            return "CAST('{}' AS JSON)"
        v = f"CAST({c} AS VARCHAR)"
        return (
            f"CAST(COALESCE(to_json(map_from_entries(list_transform("
            f"from_json(CASE WHEN json_valid({v}) THEN {v} ELSE '[]' END, "
            f"'[{{\"key\":\"VARCHAR\",\"value\":\"VARCHAR\"}}]'), x -> {{'k': x.key, 'v': x.value}}))), '{{}}') AS JSON)"
        )

    def select_list(self, cols: Columns) -> str:
        rid = cols.text("resource.global_name", "resource_global_name", "resource.name", "resource_name")
        return ",\n".join(
            [
                f"{cols.text('project.id', 'project_id')} AS external_account_id",
                f"{cols.text('project.name', 'project_name')} AS account_name",
                f"{cols.date('usage_start_time', 'usage_date')} AS charge_date",
                f"{rid} AS resource_id",
                f"regexp_extract({rid}, '[^/]+$') AS resource_name",
                "NULL AS resource_group",
                f"lower({cols.text('service.description', 'service_description')}) AS resource_type",
                f"{cols.text('service.description', 'service_description')} AS service_name",
                f"{cols.text('service.description', 'service_description')} AS meter_category",
                f"{cols.text('cost_type')} AS meter_subcategory",
                f"{cols.text('sku.description', 'sku_description')} AS meter_name",
                f"{normalize_location(cols.text('location.region', 'location_region', 'location.location', 'location_location'))} AS location",
                f"{cols.num('usage.amount_in_pricing_units', 'usage_amount_in_pricing_units', 'usage.amount', 'usage_amount')} AS quantity",
                f"{cols.text('usage.pricing_unit', 'usage_pricing_unit', 'usage.unit', 'usage_unit')} AS unit",
                "NULL AS unit_price",
                f"{self._cost(cols)} AS cost",
                f"upper(COALESCE({cols.text('currency')}, 'USD')) AS currency",
                "'OnDemand' AS pricing_model",
                f"COALESCE({cols.text('cost_type')}, 'regular') AS charge_type",
                f"{self._tags(cols)} AS tags",
            ]
        )
