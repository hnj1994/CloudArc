"""Forecasting (FR-701, FR-702).

Month-end: MTD actual + remaining days x run-rate, where the run-rate blends
the last 7 days with the whole month-to-date mean so one odd day does not
swing the forecast. The low/high range uses the day-to-day standard deviation
(an 80% band, z = 1.28), which suits the steady workloads typical of managed
subscriptions and widens automatically when spend is volatile.

Multi-month: least-squares linear trend over closed monthly totals, with a
range based on residual error.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

from ..timeutil import add_months, days_in_month

Z80 = 1.28


@dataclass
class MonthEndForecast:
    month: str
    mtd_actual: float
    days_with_data: int
    days_in_month: int
    run_rate: float
    expected: float
    low: float
    high: float
    method: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def month_end_forecast(
    month_start: date,
    daily: list[float],
    fallback_daily: float | None = None,
) -> MonthEndForecast:
    """``daily`` holds this month's daily costs from day 1 up to the last day with data."""
    dim = days_in_month(month_start)
    mtd = float(sum(daily))
    n = len(daily)
    remaining = max(dim - n, 0)
    if n == 0:
        rate = fallback_daily or 0.0
        sd = 0.15 * rate
        method = "previous-month daily average (no data yet this month)"
    else:
        recent = daily[-7:]
        rate = 0.6 * statistics.fmean(recent) + 0.4 * statistics.fmean(daily)
        sd = statistics.pstdev(daily) if n > 1 else 0.1 * rate
        method = "run-rate (60% last 7 days, 40% month-to-date mean)"
    expected = mtd + rate * remaining
    low = mtd + max(rate - Z80 * sd, 0) * remaining
    high = mtd + (rate + Z80 * sd) * remaining
    return MonthEndForecast(
        month=month_start.strftime("%Y-%m"),
        mtd_actual=round(mtd, 2),
        days_with_data=n,
        days_in_month=dim,
        run_rate=round(rate, 2),
        expected=round(expected, 2),
        low=round(low, 2),
        high=round(high, 2),
        method=method,
    )


def multi_month_forecast(history: list[tuple[date, float]], horizon: int) -> list[dict]:
    """``history``: closed months (month start, total), oldest first."""
    if not history:
        return []
    ys = [v for _, v in history]
    n = len(ys)
    last = history[-1][0]
    if n < 3:
        mean = statistics.fmean(ys)
        spread = (max(ys) - min(ys)) / 2 if n > 1 else 0.1 * mean
        slope, intercept, resid = 0.0, mean, spread
    else:
        xs = list(range(n))
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        intercept = my - slope * mx
        residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        resid = statistics.pstdev(residuals)
    out = []
    for h in range(1, horizon + 1):
        x = n - 1 + h
        value = max(intercept + slope * x, 0)
        band = Z80 * resid * (1 + h / max(n, 1)) ** 0.5
        out.append(
            {
                "month": add_months(last, h).strftime("%Y-%m"),
                "expected": round(value, 2),
                "low": round(max(value - band, 0), 2),
                "high": round(value + band, 2),
            }
        )
    return out
