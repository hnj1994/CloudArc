"""Monthly client "Cost & Governance Report" (FR-901, FR-905).

The report is a list of neutral blocks (heading, paragraph, bullets, table,
key-value, image) rendered to DOCX or PDF. Every figure comes from the same
analytics functions the dashboards use, so report and dashboard reconcile for
the same scope and period.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .. import budgets as budgets_mod
from .. import inventory
from ..analytics import allocation, costs
from ..analytics.filters import Scope
from ..analytics.forecast import multi_month_forecast
from ..config import get_settings
from ..db import Database
from ..recommendations import engine
from ..tenants import get_tenant, list_accounts
from ..timeutil import add_months, fmt_date, fmt_inr, fmt_month, month_end, month_start, region_display
from . import boq, narrative

HORIZONS = [("immediate", "Immediate actions (0–30 days)"), ("short_term", "Short-term actions (30–90 days)"),
            ("ongoing", "Ongoing optimization")]
TYPE_NAMES = {
    "microsoft.compute/virtualmachines": "Virtual machine", "microsoft.compute/disks": "Disk",
    "microsoft.sql/servers": "SQL server", "microsoft.sql/servers/databases": "SQL database",
    "microsoft.network/privateendpoints": "Private endpoint", "microsoft.network/networkinterfaces": "Network interface",
    "microsoft.network/publicipaddresses": "Public IP address", "microsoft.network/networksecuritygroups": "Network security group",
    "microsoft.network/virtualnetworks": "Virtual network", "microsoft.network/networkwatchers": "Network Watcher",
    "microsoft.network/privatednszones": "Private DNS zone", "microsoft.compute/snapshots": "Snapshot",
    "microsoft.network/applicationgateways": "Application gateway", "microsoft.recoveryservices/vaults": "Recovery Services vault",
    "microsoft.storage/storageaccounts": "Storage account", "microsoft.network/virtualnetworkgateways": "VPN gateway",
    "microsoft.resources/subscriptions/resourcegroups": "Resource group",
}


@dataclass
class Report:
    title: str
    subtitle: str
    tenant: str
    month: str
    blocks: list[tuple] = field(default_factory=list)
    figures: dict = field(default_factory=dict)  # headline numbers, for reconciliation tests / API

    def h1(self, t): self.blocks.append(("h1", t))
    def h2(self, t): self.blocks.append(("h2", t))
    def p(self, t): self.blocks.append(("p", t))
    def bullets(self, items): self.blocks.append(("bullets", list(items)))
    def numbered(self, items): self.blocks.append(("numbered", list(items)))
    def kv(self, pairs): self.blocks.append(("kv", list(pairs)))
    def table(self, columns, rows, numeric=()): self.blocks.append(("table", {"columns": columns, "rows": rows, "numeric": set(numeric)}))
    def image(self, path, width_in=6.0): self.blocks.append(("image", str(path), width_in))
    def pagebreak(self): self.blocks.append(("pagebreak",))


def _chart_daily(series: list[dict], anomalies: list[dict], title: str, out: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xs = [date.fromisoformat(p["date"]) for p in series]
    ys = [p["cost"] for p in series]
    fig, ax = plt.subplots(figsize=(8, 2.8), dpi=150)
    ax.plot(xs, ys, color="#2a6fdb", linewidth=1.8)
    ax.fill_between(xs, ys, color="#2a6fdb", alpha=0.08)
    for a in anomalies:
        d = date.fromisoformat(a["date"])
        ax.scatter([d], [a["cost"]], color="#d9480f", zorder=3, s=22)
    ax.set_title(title, fontsize=10, loc="left")
    ax.set_ylabel("₹ / day", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    lo = min(ys) if ys else 0
    ax.set_ylim(bottom=max(0, lo * 0.8) if lo > 0 else 0)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    return out


def _chart_monthly(history: list[tuple[date, float]], out: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 2.6), dpi=150)
    labels = [d.strftime("%b %Y") for d, _ in history]
    ax.bar(labels, [v for _, v in history], color="#2a6fdb", width=0.55)
    ax.set_title("Monthly cost (₹)", fontsize=10, loc="left")
    ax.tick_params(labelsize=7)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    return out


def default_report_month(db: Database, tenant_id: str) -> date:
    """The most recent closed month with data."""
    as_of = costs.data_as_of(db, tenant_id) or date.today()
    return month_start(as_of) if as_of == month_end(as_of) else add_months(month_start(as_of), -1)


def build(db: Database, tenant_id: str, month: date | None = None, workdir: Path | None = None) -> Report:
    tenant = get_tenant(db, tenant_id)
    month = month_start(month or default_report_month(db, tenant_id))
    workdir = workdir or Path(tempfile.mkdtemp(prefix="cloudarc-report-"))
    base = Scope(tenant_id)
    as_of = costs.data_as_of(db, tenant_id) or date.today()
    mv = costs.month_view(db, base, month)
    cur = costs.summary(db, base, as_of)
    prev_total = costs.total(db, base.replace(date_from=add_months(month, -1), date_to=month_end(add_months(month, -1))))
    accounts = list_accounts(db, tenant_id)
    resources = inventory.list_resources(db, tenant_id)
    recs = [r for r in engine.list_recs(db, tenant_id) if r["status"] in ("open", "accepted")]
    budget_states = [budgets_mod.status(db, tenant_id, b, as_of, tenant["fiscal_year_start"]) for b in budgets_mod.list_budgets(db, tenant_id)]
    history = costs.monthly_totals(db, base.replace(date_from=add_months(month, -11), date_to=month_end(month)))
    closed_history = [(m, v) for m, v in history if month_end(m) <= as_of]
    outlook = multi_month_forecast(closed_history, 3)

    r = Report(
        title="Cloud Cost Management & Governance Report",
        subtitle=f"{tenant['name']} · {fmt_month(month)}",
        tenant=tenant["name"],
        month=month.strftime("%Y-%m"),
    )
    r.figures = {"month_total": mv["total"], "month_label": mv["label"], "current_mtd": cur["current"]["amount"],
                 "current_label": cur["current"]["label"], "forecast": cur["forecast"]["expected"]}
    providers = sorted({a["provider"] for a in accounts}) or ["azure"]
    platform = ", ".join({"azure": "Microsoft Azure", "aws": "Amazon Web Services", "gcp": "Google Cloud"}.get(p, p) for p in providers)
    r.kv([("Platform", platform), ("Client", tenant["name"]),
          ("Accounts / subscriptions", ", ".join(a["name"] or a["external_id"] for a in accounts) or "—"),
          ("Reporting period", f"{fmt_date(month)} – {fmt_date(month_end(month))}"),
          ("Data as of", fmt_date(as_of)), ("Report date", fmt_date(date.today())),
          ("Currency", f"{tenant['currency']} (pre-tax)"), ("Prepared by", get_settings().org_name)])
    r.pagebreak()

    # 1. Executive summary ---------------------------------------------------------------------
    r.h1("1. Executive Summary")
    r.h2("Objective")
    r.p(narrative.llm_rewrite("objective", {"client": tenant["name"], "month": fmt_month(month)},
        f"This report summarizes cloud spending for {tenant['name']} in {fmt_month(month)}, the current "
        f"{fmt_month(as_of)} trend, budget adherence, governance posture and prioritized recommendations "
        "to control and optimize cloud cost."))
    r.h2("Cost position")
    total_savings = sum(x["est_monthly_saving"] for x in recs)
    mom = (100 * (mv["total"] - prev_total) / prev_total) if prev_total else None
    r.kv([
        (mv["label"], fmt_inr(mv["total"])),
        (cur["current"]["label"], fmt_inr(cur["current"]["amount"])),
        (cur["forecast"]["label"], f"{fmt_inr(cur['forecast']['expected'])} (range {fmt_inr(cur['forecast']['low'])} – {fmt_inr(cur['forecast']['high'])})"),
        ("Change vs previous full month", f"{mom:+.1f}%" if mom is not None else "n/a"),
        ("Identified optimization potential", f"{fmt_inr(total_savings)} / month ({100 * total_savings / mv['total']:.1f}% of {fmt_month(month)} spend)" if mv["total"] else fmt_inr(total_savings)),
    ])
    r.h2("Key outcomes")
    outcomes = [
        f"{len(accounts)} cloud account(s) synchronized; {len(resources)} resources in inventory.",
        "Cost allocation visibility across subscription, resource group, service, location and tags.",
        f"{len(budget_states)} budget(s) monitored with forecast-based alerting.",
        f"{len(recs)} evidence-backed optimization recommendation(s) worth {fmt_inr(total_savings, 0)} per month.",
    ]
    miscal = [b for b in budget_states if b["calibration"]["status"] == "below_trailing_spend"]
    if miscal:
        outcomes.append(f"{len(miscal)} budget(s) set below trailing actual spend — recalibration recommended (section 6).")
    r.bullets(outcomes)

    # 2. Integration overview ------------------------------------------------------------------
    r.h1("2. Integration Overview")
    r.table(["Account", "Provider", "ID", "Permission check", "Last sync"],
            [[a["name"] or "—", a["provider"].upper(), a["external_id"], a["permission_status"] or "not checked",
              f"{a['last_sync_status'] or '—'} ({a['last_sync_at']:%d %b %Y %H:%M})" if a["last_sync_at"] else "—"] for a in accounts])
    r.bullets([
        "Access via a dedicated Entra ID application registration (client credentials).",
        "Read-only roles at subscription scope: Reader, Cost Management Reader, Billing Reader.",
        "Daily automated synchronization of cost, usage, inventory and utilization metrics, with a restatement look-back window.",
    ])

    # 3. Inventory ------------------------------------------------------------------------------
    r.h1("3. Subscription & Resource Inventory")
    rgs = sorted({x["resource_group"] for x in resources if x["resource_group"]})
    locs = sorted({region_display(x["location"]) for x in resources if x["location"]})
    r.p(f"{len(resources)} resources across {len(rgs)} resource group(s), hosted in {', '.join(locs) or '—'}.")
    rows = [[x["name"], TYPE_NAMES.get(x["type"], x["type"]), region_display(x["location"])]
            for x in resources if x["type"] != "microsoft.resources/subscriptions/resourcegroups"]
    r.table(["Name", "Type", "Location"], rows[:60])
    if len(rows) > 60:
        r.p(f"… and {len(rows) - 60} more (see the Resource Explorer export).")

    # 4. Cost analysis --------------------------------------------------------------------------
    r.h1("4. Cost Analysis")
    r.h2("Spend summary")
    spend_rows = [[f"{fmt_month(m)} ({'full month, actual' if month_end(m) <= as_of else 'month-to-date, partial'})", fmt_inr(v)] for m, v in history[-4:]]
    r.table(["Period", "Cost (₹)"], spend_rows, numeric=[1])
    if len(history) > 1:
        r.image(_chart_monthly(history, workdir / "monthly.png"))
    r.h2(f"Top cost-driving services — {fmt_month(month)}")
    r.p(f"Total for {mv['label']}: {fmt_inr(mv['total'])}")
    r.table(["Service", "Meter", "Cost (₹)", "Share"],
            [[m["service"], m["meter"], fmt_inr(m["cost"]), f"{m['share_pct']:.1f}%"] for m in mv["top_meters"]], numeric=[2, 3])
    r.h2("Observations")
    obs = narrative.cost_observations(fmt_month(month), mv["total"], mv["top_services"], mv["top_meters"])
    r.bullets(obs)

    # 5. Daily pattern --------------------------------------------------------------------------
    st, anomalies = mv["trend"]["stats"], mv["trend"]["anomalies"]
    r.h1(f"5. Daily Cost Pattern — {fmt_month(month)}")
    if st:
        r.kv([("Period", f"{fmt_date(month)} – {fmt_date(month_end(month))}"), ("Total cost", fmt_inr(st["total"])),
              ("Average daily spend", f"{fmt_inr(st['average'])} / day"),
              ("Highest daily cost", f"{fmt_inr(st['max'])} ({fmt_date(date.fromisoformat(st['max_date']))})"),
              ("Lowest daily cost", f"{fmt_inr(st['min'])} ({fmt_date(date.fromisoformat(st['min_date']))})"),
              ("Stability", st["stability"].capitalize())])
        r.image(_chart_daily(mv["trend"]["series"], anomalies, f"Daily cost — {fmt_month(month)}", workdir / "daily.png"))
    r.h2("Cost trend observations")
    r.bullets(narrative.daily_observations(st, anomalies))

    # 6. Budget & forecasting -------------------------------------------------------------------
    r.h1("6. Budget and Forecasting")
    if budget_states:
        r.table(["Budget", "Scope", "Period", "Amount (₹)", "Thresholds", "Actual to date", "Forecast"],
                [[b["name"], b["scope_type"] + (f": {b['scope_value']}" if b["scope_value"] else ""), b["period"], fmt_inr(b["amount"]),
                  ", ".join(f"{t:g}%" for t in b["thresholds"]), f"{fmt_inr(b['actual'])} ({b['actual_pct']:.0f}%)",
                  f"{fmt_inr(b['forecast'])} ({b['forecast_pct']:.0f}%)"] for b in budget_states], numeric=[3, 5, 6])
        for b in miscal:
            r.p(f"Calibration finding — {b['name']}: {b['calibration']['message']}")
    else:
        r.p("No budgets are configured. Recommended: a monthly budget at the trailing 3-month average with 50/80/100% thresholds.")
    if outlook:
        r.h2("Outlook")
        r.table(["Month", "Forecast (₹)", "Range (₹)"],
                [[o["month"], fmt_inr(o["expected"]), f"{fmt_inr(o['low'])} – {fmt_inr(o['high'])}"] for o in outlook], numeric=[1])

    # 7. Tagging & allocation -------------------------------------------------------------------
    r.h1("7. Tagging and Cost Allocation")
    month_scope = base.replace(date_from=month, date_to=month_end(month))
    cov = costs.tag_coverage(db, month_scope, tenant["required_tags"])
    r.h2("Tag coverage (share of cost carrying each required tag)")
    r.table(["Tag", "Tagged cost (₹)", "Coverage"], [[t["tag"], fmt_inr(t["tagged_cost"]), f"{t['coverage_pct']:.1f}%"] for t in cov["required_tags"]], numeric=[1, 2])
    if cov["violations"]:
        r.p("Largest resources missing required tags: " + "; ".join(
            f"{v['name']} ({', '.join(v['missing_tags'])}; {fmt_inr(v['cost'], 0)})" for v in cov["violations"][:5]) + ".")
    tag_values = {}
    for key in tenant["required_tags"]:
        vals = costs.group_by(db, month_scope, [f"tag:{key}"], limit=3)
        tag_values[key] = ", ".join(v[f"tag:{key}"] for v in vals)
    r.table(["Tag", "Values (by cost)"], [[k, v] for k, v in tag_values.items()])
    r.h2("Allocation by location")
    r.table(["Location", "Cost (₹)", "Share"], [[region_display(x["location"]), fmt_inr(x["cost"]), f"{x['share_pct']:.1f}%"] for x in mv["by_location"]], numeric=[1, 2])
    r.h2("Allocation by resource group")
    r.table(["Resource group", "Cost (₹)", "Share"], [[x["resource_group"] or "—", fmt_inr(x["cost"]), f"{x['share_pct']:.1f}%"] for x in mv["by_resource_group"]], numeric=[1, 2])
    centers = allocation.allocate(db, month_scope)
    if len(centers) > 1:
        r.h2("Cost centers")
        r.table(["Cost center", "Cost (₹)", "Share"], [[c["cost_center"], fmt_inr(c["cost"]), f"{c['share_pct']:.1f}%"] for c in centers], numeric=[1, 2])

    # BOQ variance (if an approved estimate exists)
    boqs = boq.list_boqs(db, tenant_id)
    if boqs:
        v = boq.variance(db, tenant_id, boqs[0]["boq_name"], month)
        r.h2(f"Approved estimate vs actual — {v['boq_name']}")
        r.p(f"{v['label']}: actual {fmt_inr(v['actual_total'])} against an approved {fmt_inr(v['estimate_total'])} "
            f"({v['difference_pct']:+.1f}%, {fmt_inr(v['difference'])}).")
        r.table(["Component", "Estimate (₹)", "Actual (₹)", "Difference (₹)", "Note"],
                [[x["component"], fmt_inr(x["estimate"]), fmt_inr(x["actual"]), fmt_inr(x["difference"]), x["note"]] for x in v["lines"]],
                numeric=[1, 2, 3])

    # 8. Security & governance ------------------------------------------------------------------
    r.h1("8. Security and Governance Review")
    r.p("Platform access to the client environment uses least-privilege, read-only roles appropriate for a cost-management "
        "platform. No write or resource-modification permissions are granted; recommendations are advisory and changes follow "
        "the client's change-management process.")
    perm_rows = []
    for a in accounts:
        detail = json.loads(a["permission_detail"]) if a["permission_detail"] else {}
        perm_rows.append([a["name"] or a["external_id"], a["permission_status"] or "not checked",
                          "Yes" if detail.get("write_actions") else "No" if detail else "—"])
    r.table(["Account", "Required read access", "Write access detected"], perm_rows)
    r.bullets(["Reader — resource inventory and utilization metrics.", "Cost Management Reader — cost and usage data.",
               "Billing Reader — billing and reservation visibility.",
               "Credentials stored encrypted (AES-256-GCM); every onboarding, credential, budget and report action is audit-logged."])

    # 9. Recommendations ------------------------------------------------------------------------
    r.h1("9. Optimization Recommendations")
    if recs:
        r.p(f"{len(recs)} recommendation(s) with a combined estimated saving of {fmt_inr(total_savings)} per month, "
            "prioritized by time-to-value. Each is backed by the cost and utilization evidence shown.")
        n = 0
        for key, label in HORIZONS:
            group = [x for x in recs if x["horizon"] == key]
            if not group:
                continue
            r.h2(label)
            items = []
            for x in group:
                n += 1
                ev = x["evidence"]
                evidence = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in ev.items() if not isinstance(v, dict))[:400]
                items.append(f"{x['title']} — est. saving {fmt_inr(x['est_monthly_saving'], 0)}/month "
                             f"(confidence {x['confidence']}, effort {x['effort']}, risk {x['risk']}). {x['action']} Evidence: {evidence}.")
            r.numbered(items)
    else:
        r.p("No open recommendations for this period.")

    # 10. Conclusion ----------------------------------------------------------------------------
    r.h1("10. Benefits and Conclusion")
    r.bullets(["Centralized cost visibility across subscription, location, service and tags.", "Resource inventory with change tracking.",
               "Forecasting and budget monitoring with calibration checks.", "Evidence-backed, prioritized optimization recommendations.",
               "Least-privilege governance reporting."])
    r.p(narrative.llm_rewrite("conclusion", {"month_total": mv["total"], "savings": round(total_savings, 2)},
        f"{tenant['name']}'s environment spent {fmt_inr(mv['total'])} in {fmt_month(month)}. Acting on the immediate and "
        f"short-term recommendations would reduce monthly spend by up to {fmt_inr(total_savings, 0)} while keeping budgets "
        "meaningful and governance within least-privilege controls."))
    return r
