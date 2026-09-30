"""AWS Cost and Usage Report — legacy CUR (``lineItem/…``) and CUR 2.0 / Data Exports (``line_item_…``).

Costs are loaded on an *amortized* basis so that Reserved Instance and Savings
Plan usage shows its effective cost on the consuming resource instead of the
upfront/recurring fee lines, which is what cost allocation needs.
"""
from __future__ import annotations

from .common import BillingAdapter, Columns, normalize_location, sql_str


class AwsCurAdapter(BillingAdapter):
    provider = "aws"
    label = "AWS Cost and Usage Report (CUR / CUR 2.0)"

    def detect(self, cols: Columns) -> bool:
        return cols.has("lineItem/UnblendedCost", "line_item_unblended_cost")

    def _cost(self, cols: Columns) -> str:
        unblended = cols.num("lineItem/UnblendedCost", "line_item_unblended_cost", default="0")
        lt = cols.text("lineItem/LineItemType", "line_item_line_item_type", default="'Usage'")
        ri_eff = cols.num("reservation/EffectiveCost", "reservation_effective_cost")
        sp_eff = cols.num("savingsPlan/SavingsPlanEffectiveCost", "savings_plan_savings_plan_effective_cost")
        ri_unused = (
            f"({cols.num('reservation/UnusedAmortizedUpfrontFeeForBillingPeriod', 'reservation_unused_amortized_upfront_fee_for_billing_period', default='0')}"
            f" + {cols.num('reservation/UnusedRecurringFee', 'reservation_unused_recurring_fee', default='0')})"
        )
        sp_unused = (
            f"({cols.num('savingsPlan/TotalCommitmentToDate', 'savings_plan_total_commitment_to_date', default='NULL')}"
            f" - {cols.num('savingsPlan/UsedCommitment', 'savings_plan_used_commitment', default='NULL')})"
        )
        return (
            f"COALESCE(CASE {lt} "
            f"WHEN 'DiscountedUsage' THEN COALESCE({ri_eff}, {unblended}) "
            f"WHEN 'SavingsPlanCoveredUsage' THEN COALESCE({sp_eff}, {unblended}) "
            f"WHEN 'SavingsPlanNegation' THEN 0 "
            f"WHEN 'SavingsPlanUpfrontFee' THEN 0 "
            f"WHEN 'SavingsPlanRecurringFee' THEN COALESCE({sp_unused}, {unblended}) "
            f"WHEN 'RIFee' THEN {ri_unused} "
            f"ELSE {unblended} END, 0)"
        )

    def source_cost_expr(self, cols: Columns) -> str:
        return self._cost(cols)

    def _tags(self, cols: Columns) -> str:
        legacy = cols.with_prefix("resourceTags/")
        flat = [c for c in cols.with_prefix("resource_tags_") if c.lower() != "resource_tags"]
        if cols.has("resource_tags"):
            name = cols.find("resource_tags")
            c = cols.col("resource_tags")
            if "MAP" in cols.type_of(name) or "STRUCT" in cols.type_of(name):
                return f"CAST(to_json({c}) AS JSON)"
            return f"CAST(CASE WHEN json_valid({c}) THEN {c} ELSE '{{}}' END AS JSON)"
        pairs = []
        for col in legacy + flat:
            if col.startswith("resourceTags/"):
                key = col.split("/", 1)[1]
                key = key.split(":", 1)[1] if key.startswith("user:") else key
            else:
                key = col[len("resource_tags_"):]
                key = key[len("user_"):] if key.startswith("user_") else key
            pairs.append(f"{sql_str(key)}, NULLIF(CAST({cols.col(col)} AS VARCHAR), '')")
        if not pairs:
            return "CAST('{}' AS JSON)"
        # json_object keeps NULL values; strip them so tag coverage is accurate.
        return f"CAST(json_merge_patch('{{}}', json_object({', '.join(pairs)})) AS JSON)"

    def select_list(self, cols: Columns) -> str:
        rid = cols.text("lineItem/ResourceId", "line_item_resource_id")
        product = cols.text("lineItem/ProductCode", "line_item_product_code")
        lt = cols.text("lineItem/LineItemType", "line_item_line_item_type", default="'Usage'")
        usage_type = cols.text("lineItem/UsageType", "line_item_usage_type")
        term = cols.text("pricing/term", "pricing_term")
        rtype = (
            f"CASE WHEN {rid} LIKE 'arn:%' THEN split_part({rid}, ':', 3) || '/' || "
            f"split_part(split_part({rid}, ':', 6), '/', 1) "
            f"WHEN {rid} LIKE 'i-%' THEN 'ec2/instance' WHEN {rid} LIKE 'vol-%' THEN 'ec2/volume' "
            f"WHEN {rid} LIKE 'snap-%' THEN 'ec2/snapshot' WHEN {rid} LIKE 'eipalloc-%' THEN 'ec2/elastic-ip' "
            f"ELSE lower({product}) END"
        )
        pricing_model = (
            f"CASE WHEN {lt} = 'DiscountedUsage' OR {lt} = 'RIFee' THEN 'Reservation' "
            f"WHEN {lt} LIKE 'SavingsPlan%' THEN 'SavingsPlan' "
            f"WHEN {usage_type} LIKE '%SpotUsage%' THEN 'Spot' ELSE 'OnDemand' END"
        )
        return ",\n".join(
            [
                f"{cols.text('lineItem/UsageAccountId', 'line_item_usage_account_id')} AS external_account_id",
                f"{cols.text('lineItem/UsageAccountName', 'line_item_usage_account_name', 'bill/PayerAccountName')} AS account_name",
                f"{cols.date('lineItem/UsageStartDate', 'line_item_usage_start_date')} AS charge_date",
                f"{rid} AS resource_id",
                f"regexp_extract({rid}, '[^/:]+$') AS resource_name",
                "NULL AS resource_group",
                f"{rtype} AS resource_type",
                f"COALESCE({cols.text('product/ProductName', 'product_product_name', 'product_servicecode')}, {product}) AS service_name",
                f"{cols.text('product/productFamily', 'product_product_family')} AS meter_category",
                f"{cols.text('lineItem/Operation', 'line_item_operation')} AS meter_subcategory",
                f"{usage_type} AS meter_name",
                f"{normalize_location(cols.text('product/regionCode', 'product_region_code', 'product/region', 'product_region'))} AS location",
                f"{cols.num('lineItem/UsageAmount', 'line_item_usage_amount')} AS quantity",
                f"{cols.text('pricing/unit', 'pricing_unit')} AS unit",
                f"{cols.num('lineItem/UnblendedRate', 'line_item_unblended_rate')} AS unit_price",
                f"{self._cost(cols)} AS cost",
                f"upper(COALESCE({cols.text('lineItem/CurrencyCode', 'line_item_currency_code')}, 'USD')) AS currency",
                f"{pricing_model} AS pricing_model",
                f"{lt} AS charge_type",
                f"{self._tags(cols)} AS tags",
            ]
        )
