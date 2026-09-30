from datetime import date, timedelta

import pytest

from cloudarc import alerts, budgets
from cloudarc.analytics import allocation, costs
from cloudarc.analytics.anomaly import detect_anomalies
from cloudarc.analytics.filters import FilterError, Scope
from cloudarc.analytics.forecast import month_end_forecast, multi_month_forecast
from cloudarc.timeutil import fmt_inr, period_bounds
from tests.conftest import AS_OF


def test_inr_formatting_uses_indian_grouping():
    assert fmt_inr(12345678.9) == "₹1,23,45,678.90"
    assert fmt_inr(999.5) == "₹999.50"
    assert fmt_inr(-185924.01, 0) == "-₹1,85,924"


def test_fiscal_periods():
    assert period_bounds("quarterly", date(2026, 9, 29), 4) == (date(2026, 7, 1), date(2026, 9, 30))
    assert period_bounds("annual", date(2026, 2, 1), 4) == (date(2025, 4, 1), date(2026, 3, 31))


def test_summary_labels_month_to_date_and_compares_like_for_like(seeded):
    db, _ = seeded
    s = costs.summary(db, Scope("demo-a"))
    assert s["as_of"] == AS_OF.isoformat()
    assert s["current"]["is_partial"] is True
    assert "month-to-date" in s["current"]["label"] and "partial" in s["current"]["label"]
    assert "full month" in s["previous_month"]["label"]
    mom = s["month_over_month"]
    assert "like-for-like" in mom["basis"]
    # like-for-like compares 1–29 Sep with 1–29 Aug, never MTD with a full month
    aug_1_29 = costs.total(db, Scope("demo-a", date(2026, 8, 1), date(2026, 8, 29)))
    assert mom["previous_amount"] == pytest.approx(aug_1_29, abs=0.01)
    assert s["forecast"]["low"] <= s["forecast"]["expected"] <= s["forecast"]["high"]
    assert s["forecast"]["expected"] >= s["current"]["amount"]


def test_june_matches_the_evaluated_shape(seeded):
    db, _ = seeded
    mv = costs.month_view(db, Scope("demo-a"), date(2026, 6, 1))
    assert mv["is_partial"] is False
    assert mv["total"] == pytest.approx(11264.53, rel=0.002)
    assert mv["trend"]["stats"]["stability"] == "stable"
    assert mv["top_meters"][0]["meter"] == "B4als v2"
    assert mv["trend"]["stats"]["total"] == pytest.approx(mv["total"], abs=0.01)  # report daily table reconciles


def test_month_end_forecast_math():
    fc = month_end_forecast(date(2026, 9, 1), [100.0] * 10)
    assert fc.expected == pytest.approx(3000.0)
    assert fc.low == fc.high == pytest.approx(3000.0)
    empty = month_end_forecast(date(2026, 9, 1), [], fallback_daily=50)
    assert empty.expected == pytest.approx(1500.0) and empty.low < empty.expected < empty.high


def test_multi_month_forecast_follows_trend():
    hist = [(date(2026, m, 1), 1000.0 + 100 * i) for i, m in enumerate(range(1, 7))]
    out = multi_month_forecast(hist, 3)
    assert [o["month"] for o in out] == ["2026-07", "2026-08", "2026-09"]
    assert out[0]["expected"] == pytest.approx(1600.0)


def test_anomaly_detection_flags_spike_not_noise():
    start = date(2026, 9, 1)
    series = [(start + timedelta(days=i), 100 + (i % 3)) for i in range(20)]
    series[15] = (series[15][0], 400.0)
    found = detect_anomalies(series)
    assert [a["date"] for a in found] == [series[15][0].isoformat()]
    assert found[0]["direction"] == "spike"


def test_seeded_spike_is_detected_per_service(seeded):
    db, _ = seeded
    found = costs.anomalies_by(db, Scope("demo-a"), "service", AS_OF)
    assert any(a["value"] == "Bandwidth" and a["date"] == "2026-09-18" for a in found)


def test_group_by_tag_and_unknown_dimension(seeded):
    db, _ = seeded
    rows = costs.group_by(db, Scope("demo-a", date(2026, 9, 1), AS_OF), ["tag:Environment"])
    assert {r["tag:Environment"] for r in rows} == {"Production", "(untagged)"}
    assert sum(r["share_pct"] for r in rows) == pytest.approx(100, abs=0.05)
    with pytest.raises(FilterError):
        costs.group_by(db, Scope("demo-a"), ["tenant_id; drop table tenants"])
    with pytest.raises(FilterError):
        costs.group_by(db, Scope("demo-a"), ['tag:x") OR 1=1 --'])


def test_tag_coverage_lists_violations(seeded):
    db, _ = seeded
    cov = costs.tag_coverage(db, Scope("demo-a", date(2026, 9, 1), AS_OF), ["Environment"])
    assert cov["required_tags"][0]["coverage_pct"] < 100
    assert cov["violations"][0]["name"] == "privatelink.database.windows.net"


def test_cost_center_allocation_sums_to_total(seeded):
    db, _ = seeded
    scope = Scope("demo-b", date(2026, 9, 1), AS_OF)
    allocation.create_center(db, "demo-b", "Shared gateway 40%", [{"services": ["Application Gateway"]}], percent=40)
    rows = allocation.allocate(db, scope)
    assert sum(r["cost"] for r in rows) == pytest.approx(costs.total(db, scope), abs=0.05)
    with pytest.raises(FilterError):
        allocation.create_center(db, "demo-b", "bad", [{}])


def test_budget_calibration_flags_budget_below_trailing_spend(seeded):
    db, _ = seeded
    cal = budgets.calibration(db, "demo-a", "tenant", None, "monthly", 7117.89, AS_OF)
    assert cal["status"] == "below_trailing_spend" and cal["gap_pct"] > 30
    ok = budgets.calibration(db, "demo-a", "tenant", None, "monthly", 12000, AS_OF)
    assert ok["status"] == "ok"
    q = budgets.calibration(db, "demo-a", "tenant", None, "quarterly", 12000, AS_OF)
    assert q["status"] == "below_trailing_spend"  # a quarter needs ~3x the monthly average


def test_budget_alerts_are_deduplicated(seeded):
    db, _ = seeded
    before = db.scalar("SELECT count(*) FROM alerts WHERE tenant_id = 'demo-a'")
    assert budgets.evaluate(db, "demo-a", AS_OF, notify=False) == []  # seed already evaluated
    assert db.scalar("SELECT count(*) FROM alerts WHERE tenant_id = 'demo-a'") == before


def test_forecast_alert_fires_before_actual_breach(seeded):
    db, _ = seeded
    # Mid-month: actual below budget but the run-rate projects past it.
    res = budgets.create_budget(db, "demo-a", name="Forecast probe", scope_type="tenant", scope_value=None, period="monthly",
                                amount=8000, thresholds=[100], as_of=date(2026, 9, 15))
    raised = budgets.evaluate(db, "demo-a", date(2026, 9, 15), notify=False)
    kinds = {(a["kind"]) for a in raised}
    assert "budget_forecast" in kinds
    st = budgets.status(db, "demo-a", budgets.get_budget(db, "demo-a", res["id"]), date(2026, 9, 15))
    assert st["actual"] < 8000 < st["forecast"]


def test_budget_scopes(seeded):
    db, _ = seeded
    dr = budgets.scope_total(db, "demo-b", "resource_group", "hp-dr-rg", date(2026, 9, 1), AS_OF)
    tag = budgets.scope_total(db, "demo-b", "tag", "Application=HealthPortal", date(2026, 9, 1), AS_OF)
    assert 0 < dr < tag
    with pytest.raises(budgets.BudgetError):
        budgets.create_budget(db, "demo-b", name="x", scope_type="tag", scope_value="novalue", period="monthly", amount=1, thresholds=[80])


def test_acknowledge_alert(seeded):
    db, _ = seeded
    a = alerts.list_alerts(db, "demo-a")[0]
    assert alerts.acknowledge(db, "demo-a", a["id"], "me@x")
    assert not alerts.acknowledge(db, "demo-b", a["id"], "me@x")  # other tenant's alert id
