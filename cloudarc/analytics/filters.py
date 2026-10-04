"""Tenant-scoped query building shared by every analytics view.

``Scope.where()`` always starts with ``tenant_id = ?``; there is no code path
that queries cost data without it (NFR-02). Dimension names are resolved
against a whitelist, and user-supplied values are bound as parameters.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

_TAG_KEY = re.compile(r"^[^\"\\\x00-\x1f]{1,128}$")

DIMENSIONS: dict[str, str] = {
    "account": "c.account_id",
    "provider": "c.provider",
    "resource_group": "c.resource_group",
    "service": "c.service_name",
    "meter_category": "c.meter_category",
    "meter_subcategory": "c.meter_subcategory",
    "meter": "c.meter_name",
    "location": "c.location",
    "resource": "c.resource_id",
    "resource_type": "c.resource_type",
    "pricing_model": "c.pricing_model",
    "charge_type": "c.charge_type",
    "day": "c.charge_date",
    "month": "strftime(c.charge_date, '%Y-%m')",
}


class FilterError(ValueError):
    pass


def tag_path(key: str) -> str:
    if not _TAG_KEY.match(key):
        raise FilterError(f"invalid tag key: {key!r}")
    return '$."' + key + '"'


def dimension_sql(dim: str, params: list) -> str:
    """SQL expression for a group-by dimension; ``tag:<key>`` groups by tag value."""
    if dim.startswith("tag:"):
        params.append(tag_path(dim[4:]))
        return "COALESCE(NULLIF(json_extract_string(c.tags, ?), ''), '(untagged)')"
    try:
        return DIMENSIONS[dim]
    except KeyError:
        raise FilterError(f"unknown dimension: {dim}") from None


@dataclass
class Scope:
    tenant_id: str
    date_from: date | None = None
    date_to: date | None = None
    accounts: list[str] = field(default_factory=list)
    resource_groups: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    meters: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)  # key -> value ("" means "tag missing")

    def replace(self, **kw) -> Scope:
        data = {**self.__dict__, **kw}
        return Scope(**data)

    def where(self, alias: str = "c") -> tuple[str, list]:
        a = alias
        clauses = [f"{a}.tenant_id = ?"]
        params: list = [self.tenant_id]
        if self.date_from:
            clauses.append(f"{a}.charge_date >= ?")
            params.append(self.date_from)
        if self.date_to:
            clauses.append(f"{a}.charge_date <= ?")
            params.append(self.date_to)
        for col, values in (
            ("account_id", self.accounts),
            ("resource_group", [v.lower() for v in self.resource_groups]),
            ("service_name", self.services),
            ("location", self.locations),
            ("resource_id", [v.lower() for v in self.resources]),
            ("meter_name", self.meters),
            ("provider", self.providers),
        ):
            if values:
                clauses.append(f"{a}.{col} IN ({', '.join('?' for _ in values)})")
                params.extend(values)
        for key, value in self.tags.items():
            if value == "":
                clauses.append(f"COALESCE(json_extract_string({a}.tags, ?), '') = ''")
                params.append(tag_path(key))
            else:
                clauses.append(f"json_extract_string({a}.tags, ?) = ?")
                params.extend([tag_path(key), value])
        return " AND ".join(clauses), params
