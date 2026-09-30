"""Plain-language narrative for reports (FR-808).

The default writer is deterministic: it turns computed figures into sentences.
An optional LLM pass may rephrase the text, but only when the data-handling
policy is approved (CLOUDARC_LLM_DATA_POLICY_APPROVED) and only if every number
in the LLM's output also appears in the facts supplied to it; otherwise the
deterministic text is kept. Figures therefore always come from platform data.
"""
from __future__ import annotations

import json
import logging
import re

from ..config import get_settings
from ..timeutil import fmt_inr

log = logging.getLogger(__name__)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def cost_observations(month_label: str, total: float, services: list[dict], meters: list[dict]) -> list[str]:
    obs = []
    if meters:
        top = meters[0]
        obs.append(
            f"{top['service']} ({top['meter']}) is the largest single cost driver at {fmt_inr(top['cost'], 0)}, "
            f"roughly {top['share_pct']:.0f}% of {month_label} spend."
        )
    if len(services) >= 2:
        s2 = services[1]
        obs.append(f"{s2['service']} is the second-largest service at {fmt_inr(s2['cost'], 0)} ({s2['share_pct']:.0f}%).")
        top2 = services[0]["share_pct"] + s2["share_pct"]
        obs.append(
            f"Together, {services[0]['service']} and {s2['service']} account for {top2:.0f}% of monthly spend, "
            "making them the primary optimization targets."
        )
    return obs


def daily_observations(stats: dict, anomalies: list[dict]) -> list[str]:
    if not stats:
        return ["No daily cost data for the period."]
    out = []
    if anomalies:
        a = max(anomalies, key=lambda x: abs(x["deviation"]))
        out.append(
            f"{len(anomalies)} anomalous day(s) detected; the largest was {a['date']} at {fmt_inr(a['cost'])} "
            f"against a baseline of {fmt_inr(a['baseline'])}."
        )
    else:
        out.append("No sudden spikes or drops in expenditure.")
    out.append(f"Daily variation of about {fmt_inr(stats['range'], 0)} between the highest and lowest day.")
    out.append(
        {
            "stable": "Stable, predictable and consistent operational workload.",
            "moderate": "Moderate day-to-day variation; review scaling events and usage-based services.",
            "volatile": "Volatile daily spend; investigate the drivers of the swings before setting tight budgets.",
        }[stats["stability"]]
    )
    return out


def llm_rewrite(section: str, facts: dict, draft: str) -> str:
    """Optionally polish ``draft`` with an LLM; returns ``draft`` unless every figure checks out."""
    s = get_settings()
    if s.llm_provider != "anthropic" or not s.llm_data_policy_approved:
        return draft
    try:
        import anthropic  # optional dependency

        client = anthropic.Anthropic()
        msg = client.beta.messages.create(
            model=s.llm_model or "claude-opus-5-5",
            max_tokens=16000,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=(
                "You rewrite sections of a client cloud-cost report for clarity and a professional tone. "
                "Use ONLY the figures in FACTS and DRAFT; never introduce, round differently or compute new numbers. "
                "Return only the rewritten text."
            ),
            messages=[{"role": "user", "content": f"SECTION: {section}\nFACTS: {json.dumps(facts, default=str)}\nDRAFT: {draft}"}],
        )
        if msg.stop_reason != "end_turn":
            return draft
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as exc:  # noqa: BLE001 - narrative polish is best-effort
        log.warning("LLM narrative unavailable: %s", type(exc).__name__)
        return draft
    allowed = set(_NUM.findall(draft + " " + json.dumps(facts, default=str)))
    if not text or any(n not in allowed for n in _NUM.findall(text)):
        log.warning("LLM narrative rejected: contained figures not present in platform data")
        return draft
    return text
