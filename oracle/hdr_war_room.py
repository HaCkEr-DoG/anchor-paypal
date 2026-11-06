"""
HDR War Room — emergency response when dispute rate is approaching thresholds.

PayPal's HDR program is the existential threat to any merchant's account.
Most merchants realize they have a problem AFTER they've crossed a threshold.
The war room detects the trajectory BEFORE the crossing and generates a
30-day battle plan to pull the rate back down.

Thresholds (rolling 90-day):
  0.50% → ELEVATED (monitoring starts, may contact merchant)
  1.00% → LIMITED (payment processing restrictions)
  1.50% → FEE_TIER ($30 per dispute above 1.5%)
  3.00% → SEVERE (further processing restrictions)
  5.00% → TERMINAL (account suspension risk)

The war room does three things:
  1. FORECAST: Project HDR for next 30/60/90 days given current trajectory
  2. TRIAGE: Identify which open disputes, if won, would most reduce HDR
  3. BATTLE_PLAN: 30-day playbook to get back below the nearest safe threshold

Key insight competitors miss:
  - Winning disputes does NOT remove them from the dispute count
  - Disputes count toward HDR regardless of outcome
  - Reducing HDR requires: more successful transactions + fewer new disputes
  - The only way to rapidly fix HDR is to PREVENT new disputes, not win old ones

Emergency levers (ranked by speed of impact):
  1. Delay/hold shipments to high-risk buyers (prevents new disputes)
  2. Proactive refunds to buyers who haven't filed yet (preempts disputes)
  3. Increase transaction volume (dilutes dispute rate)
  4. Restrict dispute-prone product categories temporarily
  5. Fight ALL open disputes aggressively (preserves current rate)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import date, timedelta


class HDRZone(str, Enum):
    SAFE         = "SAFE"           # < 0.50%
    ELEVATED     = "ELEVATED"       # 0.50% – 1.00%
    LIMITED      = "LIMITED"        # 1.00% – 1.50%
    FEE_TIER     = "FEE_TIER"       # 1.50% – 3.00%
    SEVERE       = "SEVERE"         # 3.00% – 5.00%
    TERMINAL     = "TERMINAL"       # > 5.00%


class ActionPriority(str, Enum):
    IMMEDIATE   = "IMMEDIATE"       # Do today
    THIS_WEEK   = "THIS_WEEK"
    THIS_MONTH  = "THIS_MONTH"
    ONGOING     = "ONGOING"


@dataclass
class HDRSnapshot:
    """Current and historical HDR state."""
    snapshot_date: date
    total_transactions_90d: int
    total_disputes_90d: int
    dispute_rate: float                 # total_disputes_90d / total_transactions_90d
    zone: HDRZone
    # Trend data (last 4 weeks)
    weekly_new_disputes: list[int]       # [week_ago_4, week_ago_3, week_ago_2, last_week]
    weekly_new_transactions: list[int]
    # Open disputes that are still counted
    open_disputes: int


@dataclass
class HDRForecast:
    """30/60/90 day projection of dispute rate."""
    days_30_projected: float
    days_60_projected: float
    days_90_projected: float
    zone_30d: HDRZone
    zone_60d: HDRZone
    zone_90d: HDRZone
    days_to_threshold_breach: Optional[int]   # Days until next threshold crossed (or None if improving)
    threshold_at_risk: Optional[float]
    trajectory: str    # "WORSENING" / "IMPROVING" / "STABLE"
    forecast_confidence: float


@dataclass
class HDRBattleAction:
    """One specific action in the HDR battle plan."""
    action_id: str
    priority: ActionPriority
    action: str                     # What to do
    expected_impact: str            # What it changes
    hdr_improvement_estimate: float # How many basis points this could reduce HDR
    implementation_effort: str      # "minutes" / "hours" / "days"
    risk: str                       # "none" / "low" / "medium" / "high"


@dataclass
class HDRWarRoomReport:
    """
    Complete HDR crisis assessment and battle plan.
    Generated when HDR zone is ELEVATED or worse.
    """
    generated_date: date
    current_rate: float
    current_zone: HDRZone
    # Forecast
    forecast: HDRForecast
    # Distance to thresholds
    basis_points_from_limited: float    # Positive = below LIMITED (safe)
    basis_points_from_fee_tier: float
    basis_points_from_severe: float
    # Battle plan
    immediate_actions: list[HDRBattleAction]    # Do today
    week_actions: list[HDRBattleAction]
    month_actions: list[HDRBattleAction]
    # Specific levers
    disputes_to_fight_aggressively: int     # How many open disputes to prioritize
    transactions_needed_to_normalize: int   # Additional transactions to dilute rate
    maximum_new_disputes_this_month: int    # Cap to stay below next threshold
    # Communication
    paypal_proactive_contact_recommended: bool
    executive_escalation_required: bool     # True if SEVERE or TERMINAL zone
    summary: str
    alarm_message: str                      # What to tell the team right now


# Threshold map: (rate, zone, label)
_THRESHOLDS = [
    (0.0050, HDRZone.ELEVATED,  "ELEVATED"),
    (0.0100, HDRZone.LIMITED,   "LIMITED"),
    (0.0150, HDRZone.FEE_TIER,  "FEE_TIER ($30/dispute)"),
    (0.0300, HDRZone.SEVERE,    "SEVERE"),
    (0.0500, HDRZone.TERMINAL,  "TERMINAL"),
]


def _rate_to_zone(rate: float) -> HDRZone:
    if rate >= 0.0500:
        return HDRZone.TERMINAL
    elif rate >= 0.0300:
        return HDRZone.SEVERE
    elif rate >= 0.0150:
        return HDRZone.FEE_TIER
    elif rate >= 0.0100:
        return HDRZone.LIMITED
    elif rate >= 0.0050:
        return HDRZone.ELEVATED
    else:
        return HDRZone.SAFE


def analyze_hdr_crisis(snapshot: HDRSnapshot) -> HDRWarRoomReport:
    """
    Assess HDR situation and generate battle plan.

    The war room triggers when zone is ELEVATED or worse. For SAFE zones,
    it still provides forecasting and monitoring guidance.
    """
    today = snapshot.snapshot_date
    current_rate = snapshot.dispute_rate
    current_zone = snapshot.zone

    # ── Forecast ───────────────────────────────────────────────────────────
    forecast = _forecast_hdr(snapshot)

    # ── Threshold distances ─────────────────────────────────────────────────
    bps_from_limited  = round((0.0100 - current_rate) * 10000, 1)
    bps_from_fee_tier = round((0.0150 - current_rate) * 10000, 1)
    bps_from_severe   = round((0.0300 - current_rate) * 10000, 1)

    # ── Battle plan actions ────────────────────────────────────────────────
    immediate: list[HDRBattleAction] = []
    week: list[HDRBattleAction] = []
    month: list[HDRBattleAction] = []

    # Always: Fight everything aggressively
    immediate.append(HDRBattleAction(
        action_id="FIGHT_ALL",
        priority=ActionPriority.IMMEDIATE,
        action="Fight ALL open disputes aggressively — submit maximum evidence",
        expected_impact="Prevents disputes from aging into losses; preserves current rate",
        hdr_improvement_estimate=0.0,  # Winning doesn't remove disputes
        implementation_effort="hours",
        risk="none",
    ))

    # Identify and block high-risk buyers
    immediate.append(HDRBattleAction(
        action_id="BLOCK_HIGH_RISK",
        priority=ActionPriority.IMMEDIATE,
        action=(
            "Block buyers with 2+ disputes in last 90 days from new orders. "
            "Use network intelligence to identify at-risk buyers before they order."
        ),
        expected_impact="Prevents 20-40% of future disputes from known bad actors",
        hdr_improvement_estimate=5.0,  # 5 basis points per month if 20% of disputes blocked
        implementation_effort="hours",
        risk="low",
    ))

    if current_zone in (HDRZone.LIMITED, HDRZone.FEE_TIER, HDRZone.SEVERE, HDRZone.TERMINAL):
        # Emergency: proactive pre-dispute refunds
        immediate.append(HDRBattleAction(
            action_id="PREEMPTIVE_REFUNDS",
            priority=ActionPriority.IMMEDIATE,
            action=(
                "Identify all recent orders with delivery issues (delayed/missing). "
                "Proactively refund BEFORE buyer files dispute. "
                "Target: orders > 10 days with no delivery confirmation."
            ),
            expected_impact="Each prevented dispute reduces HDR by 1/N transactions",
            hdr_improvement_estimate=10.0,
            implementation_effort="hours",
            risk="medium",  # Cash outflow
        ))

        # Emergency: pause high-dispute product categories
        immediate.append(HDRBattleAction(
            action_id="PAUSE_HIGH_RISK_PRODUCTS",
            priority=ActionPriority.IMMEDIATE,
            action=(
                "Temporarily pause or add extra verification for product categories "
                "with dispute rate > 2% (electronics, gift cards, luxury items). "
                "Re-enable after HDR returns to ELEVATED zone."
            ),
            expected_impact="Reduces new dispute volume immediately",
            hdr_improvement_estimate=20.0,
            implementation_effort="hours",
            risk="medium",  # Revenue impact
        ))

    # Increase transaction volume
    week.append(HDRBattleAction(
        action_id="INCREASE_VOLUME",
        priority=ActionPriority.THIS_WEEK,
        action=(
            "Run promotions to increase successful transaction volume. "
            "Target: +10-20% transaction volume dilutes dispute rate by same percentage."
        ),
        expected_impact=f"Each 10% volume increase reduces HDR rate by ~{current_rate * 0.10 * 10000:.1f} bps",
        hdr_improvement_estimate=current_rate * 0.10 * 10000,
        implementation_effort="days",
        risk="low",
    ))

    # Upgrade carriers
    week.append(HDRBattleAction(
        action_id="UPGRADE_CARRIERS",
        priority=ActionPriority.THIS_WEEK,
        action=(
            "Switch all shipments from BRONZE/RISKY carriers (LaserShip, OnTrac) "
            "to PLATINUM carriers (USPS Priority, FedEx Express). "
            "Carrier disputes are 60% of INR claims for most merchants."
        ),
        expected_impact="Reduces INR dispute rate by 20-40% over next 30 days",
        hdr_improvement_estimate=15.0,
        implementation_effort="days",
        risk="low",
    ))

    # Buyer verification
    month.append(HDRBattleAction(
        action_id="BUYER_VERIFICATION",
        priority=ActionPriority.THIS_MONTH,
        action=(
            "Implement phone/address verification for orders from: "
            "(a) new PayPal accounts < 90 days old, "
            "(b) first-time buyers from high-dispute ZIPs, "
            "(c) orders with mismatched billing/shipping address."
        ),
        expected_impact="Prevents account farm and triangulation fraud",
        hdr_improvement_estimate=8.0,
        implementation_effort="days",
        risk="low",
    ))

    # Required volume to normalize
    target_rate = 0.0045  # Target well below ELEVATED
    if snapshot.total_disputes_90d > 0:
        needed_transactions = max(0, int(snapshot.total_disputes_90d / target_rate) - snapshot.total_transactions_90d)
    else:
        needed_transactions = 0

    # Maximum new disputes this month to stay below next threshold
    next_threshold = 0.0050  # ELEVATED
    for thresh, zone, _ in _THRESHOLDS:
        if thresh > current_rate:
            next_threshold = thresh
            break
    max_new_disputes = max(0, int(snapshot.total_transactions_90d * next_threshold) - snapshot.total_disputes_90d)

    # PayPal proactive contact
    paypal_contact = current_zone in (HDRZone.LIMITED, HDRZone.FEE_TIER, HDRZone.SEVERE)
    exec_escalation = current_zone in (HDRZone.SEVERE, HDRZone.TERMINAL)

    if current_zone == HDRZone.SAFE:
        alarm = f"HDR at {current_rate:.2%} — SAFE. Monitor monthly."
    elif current_zone == HDRZone.ELEVATED:
        alarm = (
            f"⚠ HDR {current_rate:.2%} — ELEVATED. "
            f"{bps_from_limited:.0f} bps from LIMITED processing restrictions. "
            f"Take preventive action now."
        )
    elif current_zone == HDRZone.LIMITED:
        alarm = (
            f"🚨 HDR {current_rate:.2%} — LIMITED. Processing restrictions active. "
            f"{bps_from_fee_tier:.0f} bps from $30/dispute fee tier. "
            f"Emergency protocol: activate all immediate actions NOW."
        )
    elif current_zone == HDRZone.FEE_TIER:
        alarm = (
            f"🚨🚨 HDR {current_rate:.2%} — FEE TIER. "
            f"$30 fee applies to each dispute above 1.5% threshold. "
            f"Contact PayPal Merchant Relations proactively."
        )
    else:
        alarm = (
            f"⛔ HDR {current_rate:.2%} — {current_zone.value}. "
            f"CRITICAL: Account suspension risk. Executive escalation required."
        )

    summary = (
        f"HDR {current_rate:.2%} ({current_zone.value}) | "
        f"30d forecast: {forecast.days_30_projected:.2%} ({forecast.zone_30d.value}) | "
        f"immediate actions: {len(immediate)} | "
        f"volume needed to normalize: +{needed_transactions:,} transactions"
    )

    return HDRWarRoomReport(
        generated_date=today,
        current_rate=current_rate,
        current_zone=current_zone,
        forecast=forecast,
        basis_points_from_limited=bps_from_limited,
        basis_points_from_fee_tier=bps_from_fee_tier,
        basis_points_from_severe=bps_from_severe,
        immediate_actions=immediate,
        week_actions=week,
        month_actions=month,
        disputes_to_fight_aggressively=snapshot.open_disputes,
        transactions_needed_to_normalize=needed_transactions,
        maximum_new_disputes_this_month=max_new_disputes,
        paypal_proactive_contact_recommended=paypal_contact,
        executive_escalation_required=exec_escalation,
        summary=summary,
        alarm_message=alarm,
    )


def _forecast_hdr(snapshot: HDRSnapshot) -> HDRForecast:
    """Project HDR rate forward based on weekly trend."""
    weekly_disputes = snapshot.weekly_new_disputes
    weekly_tx = snapshot.weekly_new_transactions

    if not weekly_disputes or not weekly_tx:
        # No trend data — project flat
        return HDRForecast(
            days_30_projected=snapshot.dispute_rate,
            days_60_projected=snapshot.dispute_rate,
            days_90_projected=snapshot.dispute_rate,
            zone_30d=snapshot.zone,
            zone_60d=snapshot.zone,
            zone_90d=snapshot.zone,
            days_to_threshold_breach=None,
            threshold_at_risk=None,
            trajectory="STABLE",
            forecast_confidence=0.40,
        )

    # Compute weekly dispute rate trend
    weekly_rates = [
        d / max(t, 1)
        for d, t in zip(weekly_disputes, weekly_tx)
    ]

    # Simple linear trend from last 4 weeks
    n = len(weekly_rates)
    if n >= 2:
        # Slope of weekly rates (positive = worsening)
        slope = (weekly_rates[-1] - weekly_rates[0]) / max(n - 1, 1)
    else:
        slope = 0.0

    # Project 90-day rate forward (rolling window makes this complex — approximate)
    base_rate = snapshot.dispute_rate
    # 30 days = ~1/3 of rolling window replacement
    proj_30 = max(0.0, base_rate + slope * 4.3)   # ~4.3 weeks in 30 days
    proj_60 = max(0.0, base_rate + slope * 8.6)
    proj_90 = max(0.0, base_rate + slope * 13.0)

    # Cap at reasonable maximum
    proj_30 = min(proj_30, 0.20)
    proj_60 = min(proj_60, 0.20)
    proj_90 = min(proj_90, 0.20)

    # Trajectory
    if slope > 0.0001:
        trajectory = "WORSENING"
    elif slope < -0.0001:
        trajectory = "IMPROVING"
    else:
        trajectory = "STABLE"

    # Days to next threshold breach
    next_threshold: Optional[float] = None
    days_to_breach: Optional[int] = None
    for thresh, _, _ in _THRESHOLDS:
        if thresh > base_rate:
            next_threshold = thresh
            if slope > 0:
                weeks_to_breach = (thresh - base_rate) / slope
                days_to_breach = int(weeks_to_breach * 7)
            break

    return HDRForecast(
        days_30_projected=round(proj_30, 4),
        days_60_projected=round(proj_60, 4),
        days_90_projected=round(proj_90, 4),
        zone_30d=_rate_to_zone(proj_30),
        zone_60d=_rate_to_zone(proj_60),
        zone_90d=_rate_to_zone(proj_90),
        days_to_threshold_breach=days_to_breach,
        threshold_at_risk=next_threshold,
        trajectory=trajectory,
        forecast_confidence=0.60 if n >= 4 else 0.40,
    )
