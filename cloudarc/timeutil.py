"""Period arithmetic and INR / date formatting (NFR-11)."""
from __future__ import annotations

import calendar
from datetime import date, timedelta


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def prev_month_start(d: date) -> date:
    return add_months(month_start(d), -1)


def parse_month(s: str) -> date:
    """'2026-06' -> date(2026, 6, 1)."""
    y, m = s.split("-")[:2]
    return date(int(y), int(m), 1)


def period_bounds(period: str, as_of: date, fiscal_year_start: int = 4) -> tuple[date, date]:
    """Start and end dates of the monthly / quarterly / annual (fiscal) period containing ``as_of``."""
    if period == "monthly":
        return month_start(as_of), month_end(as_of)
    fy_start_year = as_of.year if as_of.month >= fiscal_year_start else as_of.year - 1
    fy_start = date(fy_start_year, fiscal_year_start, 1)
    if period == "annual":
        return fy_start, add_months(fy_start, 12) - timedelta(days=1)
    if period == "quarterly":
        months_in = (as_of.year - fy_start.year) * 12 + as_of.month - fy_start.month
        q_start = add_months(fy_start, (months_in // 3) * 3)
        return q_start, add_months(q_start, 3) - timedelta(days=1)
    raise ValueError(f"unknown period {period}")


def period_months(period: str) -> int:
    return {"monthly": 1, "quarterly": 3, "annual": 12}[period]


def fmt_date(d: date) -> str:
    """dd MMM yyyy, e.g. 01 Jun 2026."""
    return d.strftime("%d %b %Y")


def fmt_month(d: date) -> str:
    return d.strftime("%B %Y")


def fmt_inr(amount: float | None, decimals: int = 2, symbol: str = "₹") -> str:
    """Indian digit grouping: 12345678.9 -> ₹1,23,45,678.90."""
    if amount is None:
        return "—"
    neg = amount < 0
    s = f"{abs(amount):.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    out = f"{symbol}{whole}" + (f".{frac}" if decimals else "")
    return f"-{out}" if neg else out


def fmt_money(amount: float | None, currency: str = "INR", decimals: int = 2) -> str:
    if currency == "INR":
        return fmt_inr(amount, decimals)
    symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
    if amount is None:
        return "—"
    return f"{symbols.get(currency, currency + ' ')}{amount:,.{decimals}f}"


REGION_NAMES = {
    "centralindia": "Central India",
    "southindia": "South India",
    "westindia": "West India",
    "jioindiawest": "Jio India West",
    "jioindiacentral": "Jio India Central",
    "eastasia": "East Asia",
    "southeastasia": "Southeast Asia",
    "eastus": "East US",
    "eastus2": "East US 2",
    "westus": "West US",
    "westeurope": "West Europe",
    "northeurope": "North Europe",
    "uksouth": "UK South",
    "global": "Global",
}


def region_display(key: str | None) -> str:
    if not key:
        return "—"
    return REGION_NAMES.get(key, key)
