"""
Dispute Aging Monitor — track how long disputes have been open without action.

A dispute that sits for 3 days without a response loses 25% of its remaining
response window. This oracle scans open disputes, buckets them by age tier,
and flags cases at risk of default judgment due to inaction.

Age tiers:
  FRESH    — opened ≤ 24h ago; no urgency
  ACTIVE   — 1–3 days; response should be in progress
  AGING    — 4–7 days; needs attention today
  CRITICAL — 8–14 days; deadline approaching, escalate immediately
  OVERDUE  — deadline passed without response; default judgment likely

For each case the monitor produces:
  - age_hours: how long the dispute has been open
  - age_tier: FRESH / ACTIVE / AGING / CRITICAL / OVERDUE
  - hours_until_deadline: remaining response window
  - inaction_risk_score: 0.0 – 1.0 (higher = more at risk of default)
  - escalation_flag: True if the dispute needs immediate human attention

Aggregate report:
  - tier breakdown counts
  - total_at_risk_usd: sum of amounts in AGING + CRITICAL + OVERDUE
  - critical_cases: sorted list of disputes most at risk

PayPal benefit:
  - Early detection of stale disputes reduces default judgment rate
    (defaults cost PayPal review bandwidth and hurt merchant relationships)
  - Aging distribution is a leading indicator of merchant operational health
  - Merchants with many AGING/CRITICAL disputes are candidates for proactive
    outreach by PayPal's merchant success team
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class AgeTier(str, Enum):
    FRESH    = "FRESH"
    ACTIVE   = "ACTIVE"
    AGING    = "AGING"
    CRITICAL = "CRITICAL"
    OVERDUE  = "OVERDUE"


@dataclass
class OpenDisputeAge:
    dispute_id: str
    amount: float
    reason_code: str
    opened_hours_ago: float        # hours since dispute was filed
    deadline_hours_total: float    # total response window in hours
    last_action_hours_ago: float   # hours since last merchant action (0 = never touched)


@dataclass
class AgedDispute:
    dispute_id: str
    amount: float
    reason_code: str
    age_hours: float
    age_tier: AgeTier
    hours_until_deadline: float
    inaction_risk_score: float
    escalation_flag: bool
    recommendation: str


@dataclass
class AgingReport:
    merchant_id: str
    total_open: int
    tier_counts: dict          # AgeTier → count
    total_at_risk_usd: float   # AGING + CRITICAL + OVERDUE
    critical_cases: List[AgedDispute]
    all_disputes: List[AgedDispute]
    overall_health: str        # HEALTHY / AT_RISK / CRITICAL
    paypal_signal: str


@dataclass
class AgingMonitorInput:
    merchant_id: str
    disputes: List[OpenDisputeAge]


# ─── Scoring ──────────────────────────────────────────────────────────────────

def _age_tier(opened_hours_ago: float, hours_remaining: float) -> AgeTier:
    if hours_remaining <= 0:
        return AgeTier.OVERDUE
    if opened_hours_ago <= 24:
        return AgeTier.FRESH
    if opened_hours_ago <= 72:
        return AgeTier.ACTIVE
    if opened_hours_ago <= 168:  # 7 days
        return AgeTier.AGING
    return AgeTier.CRITICAL


def _inaction_risk(
    opened_hours_ago: float,
    hours_remaining: float,
    last_action_hours_ago: float,
) -> float:
    """
    Composite inaction risk:
    - 0.50 × deadline fraction consumed (opened / total)
    - 0.30 × time since last action (capped at 7 days)
    - 0.20 × proximity to deadline (1 - remaining/total)
    """
    total = opened_hours_ago + max(0.0, hours_remaining)
    if total <= 0:
        return 1.0

    deadline_consumed = min(1.0, opened_hours_ago / total)
    action_staleness = min(1.0, last_action_hours_ago / (7 * 24))
    deadline_proximity = 1.0 - min(1.0, max(0.0, hours_remaining) / total)

    score = 0.50 * deadline_consumed + 0.30 * action_staleness + 0.20 * deadline_proximity
    return round(min(1.0, max(0.0, score)), 4)


_TIER_RECOMMENDATION = {
    AgeTier.FRESH:    "No action needed — dispute is fresh. Queue for review within 24 hours.",
    AgeTier.ACTIVE:   "Response should be in progress. Confirm evidence gathering is underway.",
    AgeTier.AGING:    "Dispute is aging — assign to a team member and begin drafting response today.",
    AgeTier.CRITICAL: "CRITICAL — deadline approaching. Escalate to senior dispute handler immediately.",
    AgeTier.OVERDUE:  "OVERDUE — deadline passed. Contact PayPal dispute resolution to request extension or accept outcome.",
}


def monitor_aging(inp: AgingMonitorInput) -> AgingReport:
    """
    Scan open disputes for aging patterns and produce an escalation report.
    """
    aged: List[AgedDispute] = []

    for d in inp.disputes:
        hours_remaining = d.deadline_hours_total - d.opened_hours_ago
        tier = _age_tier(d.opened_hours_ago, hours_remaining)
        risk = _inaction_risk(d.opened_hours_ago, hours_remaining, d.last_action_hours_ago)
        escalate = tier in (AgeTier.CRITICAL, AgeTier.OVERDUE) or risk >= 0.75

        aged.append(AgedDispute(
            dispute_id=d.dispute_id,
            amount=d.amount,
            reason_code=d.reason_code,
            age_hours=round(d.opened_hours_ago, 1),
            age_tier=tier,
            hours_until_deadline=round(max(0.0, hours_remaining), 1),
            inaction_risk_score=risk,
            escalation_flag=escalate,
            recommendation=_TIER_RECOMMENDATION[tier],
        ))

    # Tier counts
    tier_counts = {t.value: 0 for t in AgeTier}
    for a in aged:
        tier_counts[a.age_tier.value] += 1

    at_risk_tiers = {AgeTier.AGING, AgeTier.CRITICAL, AgeTier.OVERDUE}
    at_risk_usd = sum(a.amount for a in aged if a.age_tier in at_risk_tiers)

    critical_cases = sorted(
        [a for a in aged if a.escalation_flag],
        key=lambda x: x.inaction_risk_score,
        reverse=True,
    )

    n_critical = tier_counts[AgeTier.CRITICAL.value] + tier_counts[AgeTier.OVERDUE.value]
    n_aging = tier_counts[AgeTier.AGING.value]

    if n_critical > 0:
        health = "CRITICAL"
    elif n_aging >= 3 or (len(aged) > 0 and n_aging / max(len(aged), 1) >= 0.4):
        health = "AT_RISK"
    else:
        health = "HEALTHY"

    paypal_signal = (
        f"Merchant {inp.merchant_id}: {len(aged)} open disputes — "
        f"CRITICAL/OVERDUE={n_critical}, AGING={n_aging}. "
        f"At-risk amount=${at_risk_usd:.2f}. Health={health}."
    )

    return AgingReport(
        merchant_id=inp.merchant_id,
        total_open=len(aged),
        tier_counts=tier_counts,
        total_at_risk_usd=round(at_risk_usd, 2),
        critical_cases=critical_cases,
        all_disputes=aged,
        overall_health=health,
        paypal_signal=paypal_signal,
    )
