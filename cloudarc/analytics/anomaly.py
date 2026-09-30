"""Daily cost anomaly detection (FR-403, FR-605).

Robust z-score against a trailing window: z = 0.6745 * (x - median) / MAD.
Median/MAD ignore the occasional spike inside the baseline, unlike mean/stddev.
A point must also move by a minimum relative amount so that near-constant
series (MAD ~ 0) do not flag rounding noise.
"""
from __future__ import annotations

import statistics
from datetime import date


def detect_anomalies(
    series: list[tuple[date, float]],
    sensitivity: float = 3.5,
    window: int = 14,
    min_history: int = 7,
    min_relative_change: float = 0.2,
) -> list[dict]:
    anomalies = []
    for i in range(len(series)):
        if i < min_history:
            continue
        day, value = series[i]
        base = [v for _, v in series[max(0, i - window):i]]
        med = statistics.median(base)
        mad = statistics.median([abs(v - med) for v in base])
        deviation = value - med
        rel = abs(deviation) / med if med else (1.0 if value else 0.0)
        if rel < min_relative_change:
            continue
        z = 0.6745 * deviation / mad if mad > 0 else (float("inf") if deviation else 0.0)
        if abs(z) >= sensitivity:
            anomalies.append(
                {
                    "date": day.isoformat(),
                    "cost": round(value, 2),
                    "baseline": round(med, 2),
                    "deviation": round(deviation, 2),
                    "deviation_pct": round(100 * rel, 1),
                    "direction": "spike" if deviation > 0 else "drop",
                    "score": None if z in (float("inf"), float("-inf")) else round(z, 2),
                }
            )
    return anomalies
