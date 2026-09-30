"""Azure Cost Management cost-details / exports (EA, MCA and legacy usage-details layouts)."""
from __future__ import annotations

from .common import BillingAdapter, Columns, normalize_location

COST_COLS = ("CostInBillingCurrency", "costInBillingCurrency", "PreTaxCost", "Cost", "BilledCost")
DATE_COLS = ("Date", "date", "UsageDateTime", "UsageDate", "ChargePeriodStart")
ACCOUNT_COLS = ("SubscriptionId", "subscriptionId", "SubscriptionGuid", "subscriptionGuid", "SubAccountId")


def resource_type_expr(rid: str) -> str:
    """microsoft.sql/servers/databases from .../providers/Microsoft.Sql/servers/x/databases/y."""
    pat = "providers/([^/]+)/([^/]+)/[^/]+(?:/([^/]+)/[^/]+)?"
    return (
        f"CASE WHEN regexp_matches({rid}, '{pat}') THEN "
        f"regexp_extract({rid}, '{pat}', 1) || '/' || regexp_extract({rid}, '{pat}', 2) || "
        f"CASE WHEN regexp_extract({rid}, '{pat}', 3) <> '' THEN '/' || regexp_extract({rid}, '{pat}', 3) ELSE '' END "
        f"END"
    )


def azure_tags_expr(raw: str) -> str:
    """Azure exports write tags either as JSON or as '"k": "v","k2": "v2"' without braces."""
    return (
        f"CAST(CASE WHEN {raw} IS NULL THEN '{{}}' "
        f"WHEN json_valid({raw}) AND left(trim({raw}), 1) = '{{' THEN {raw} "
        f"WHEN json_valid('{{' || {raw} || '}}') THEN '{{' || {raw} || '}}' "
        f"ELSE '{{}}' END AS JSON)"
    )


class AzureAdapter(BillingAdapter):
    provider = "azure"
    label = "Azure cost details / export"

    def detect(self, cols: Columns) -> bool:
        return cols.has(*ACCOUNT_COLS) and cols.has("MeterCategory", "meterCategory", "ConsumedService")

    def source_cost_expr(self, cols: Columns) -> str:
        return f"COALESCE({cols.num(*COST_COLS)}, 0)"

    def select_list(self, cols: Columns) -> str:
        rid = f"lower({cols.text('ResourceId', 'resourceId', 'InstanceId', 'instanceId', 'ResourceID')})"
        tags = azure_tags_expr(cols.text("Tags", "tags"))
        return ",\n".join(
            [
                f"lower({cols.text(*ACCOUNT_COLS)}) AS external_account_id",
                f"{cols.text('SubscriptionName', 'subscriptionName', 'SubAccountName')} AS account_name",
                f"{cols.date(*DATE_COLS)} AS charge_date",
                f"{rid} AS resource_id",
                f"regexp_extract({rid}, '[^/]+$') AS resource_name",
                f"lower({cols.text('ResourceGroup', 'resourceGroupName', 'ResourceGroupName', 'resourceGroup')}) AS resource_group",
                f"{resource_type_expr(rid)} AS resource_type",
                f"{cols.text('MeterCategory', 'meterCategory', 'ServiceName', 'ConsumedService')} AS service_name",
                f"{cols.text('MeterCategory', 'meterCategory', 'ServiceName')} AS meter_category",
                f"{cols.text('MeterSubCategory', 'meterSubCategory', 'MeterSubcategory', 'meterSubcategory')} AS meter_subcategory",
                f"{cols.text('MeterName', 'meterName')} AS meter_name",
                f"{normalize_location(cols.text('ResourceLocation', 'resourceLocation', 'Location', 'location', 'MeterRegion'))} AS location",
                f"{cols.num('Quantity', 'quantity', 'UsageQuantity', 'ConsumedQuantity')} AS quantity",
                f"{cols.text('UnitOfMeasure', 'unitOfMeasure', 'ConsumedUnit')} AS unit",
                f"{cols.num('EffectivePrice', 'effectivePrice', 'UnitPrice', 'unitPrice', 'ResourceRate')} AS unit_price",
                f"COALESCE({cols.num(*COST_COLS)}, 0) AS cost",
                f"upper(COALESCE({cols.text('BillingCurrency', 'billingCurrency', 'BillingCurrencyCode', 'Currency', 'currency')}, 'USD')) AS currency",
                f"COALESCE({cols.text('PricingModel', 'pricingModel')}, 'OnDemand') AS pricing_model",
                f"COALESCE({cols.text('ChargeType', 'chargeType')}, 'Usage') AS charge_type",
                f"{tags} AS tags",
            ]
        )
