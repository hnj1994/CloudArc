"""Shared helpers for billing-file adapters.

Adapters translate a provider's native billing export into the normalized cost
schema by emitting a SQL SELECT list. The heavy lifting (parsing, casting,
aggregation) runs inside DuckDB, so multi-GB exports stream through without
being materialized as Python objects.
"""
from __future__ import annotations

from dataclasses import dataclass

NORMALIZED_COLUMNS = [
    "external_account_id",
    "account_name",
    "charge_date",
    "resource_id",
    "resource_name",
    "resource_group",
    "resource_type",
    "service_name",
    "meter_category",
    "meter_subcategory",
    "meter_name",
    "location",
    "quantity",
    "unit",
    "unit_price",
    "cost",
    "currency",
    "pricing_model",
    "charge_type",
    "tags",
]


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass
class Columns:
    """Case-insensitive lookup of the columns present in a billing file."""

    types: dict[str, str]  # actual column name -> DuckDB type

    def __post_init__(self) -> None:
        self._lower = {c.lower(): c for c in self.types}

    def find(self, *candidates: str) -> str | None:
        for cand in candidates:
            actual = self._lower.get(cand.lower())
            if actual is not None:
                return actual
        return None

    def has(self, *candidates: str) -> bool:
        return self.find(*candidates) is not None

    def col(self, *candidates: str) -> str | None:
        name = self.find(*candidates)
        return quote_ident(name) if name else None

    def text(self, *candidates: str, default: str = "NULL") -> str:
        c = self.col(*candidates)
        return f"NULLIF(trim(CAST({c} AS VARCHAR)), '')" if c else default

    def num(self, *candidates: str, default: str = "NULL") -> str:
        c = self.col(*candidates)
        return f"TRY_CAST({c} AS DOUBLE)" if c else default

    def date(self, *candidates: str) -> str:
        c = self.col(*candidates)
        if not c:
            raise ValueError(f"billing file has no date column (looked for {candidates})")
        v = f"CAST({c} AS VARCHAR)"
        return (
            f"COALESCE(TRY_CAST(TRY_STRPTIME({v}, '%m/%d/%Y') AS DATE), "
            f"TRY_CAST(left({v}, 10) AS DATE), "
            f"TRY_CAST(TRY_STRPTIME({v}, '%d-%m-%Y') AS DATE))"
        )

    def with_prefix(self, prefix: str) -> list[str]:
        p = prefix.lower()
        return [c for c in self.types if c.lower().startswith(p)]

    def type_of(self, name: str) -> str:
        return self.types[name].upper()


def normalize_location(expr: str) -> str:
    """Azure-style region key: lower case, no spaces ("Central India" -> "centralindia")."""
    return f"NULLIF(lower(replace({expr}, ' ', '')), '')"


class BillingAdapter:
    provider: str = ""
    label: str = ""

    def detect(self, cols: Columns) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def select_list(self, cols: Columns) -> str:  # pragma: no cover - interface
        """SQL select list producing exactly NORMALIZED_COLUMNS from relation ``raw``."""
        raise NotImplementedError

    def source_cost_expr(self, cols: Columns) -> str:  # pragma: no cover - interface
        """Expression summed over ``raw`` to reconcile loaded totals to the source."""
        raise NotImplementedError

    def row_filter(self, cols: Columns) -> str:
        return "TRUE"
