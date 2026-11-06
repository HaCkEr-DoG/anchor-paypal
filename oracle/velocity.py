"""
Dispute Velocity Forecaster — predict when you'll cross PayPal's thresholds.

PayPal's dispute rate program operates on a rolling 30-day window. Cross a
threshold and you get:

  < 0.5%  → Normal (no action)
  ≥ 0.5%  → Elevated — warning email
  ≥ 1.0%  → Limited — features restricted
  ≥ 1.5%  → High Dispute Rate (HDR) — $30 per dispute fee
  ≥ 3.0%  → Account suspension risk

Merchants rarely know exactly where they are in this window until PayPal sends
a warning. This module:

  1. Calculates current dispute rate from rolling 30-day data
  2. Forecasts how many more disputes you can absorb before crossing each threshold
  3. Identifies which open disputes to settle vs fight to stay under threshold
  4. Projects the threshold date if current dispute velocity continues
  5. Calculates the financial cost of crossing each tier

The key insight: settling a $50 dispute costs $50. The HDR fee adds $30 per
dispute AND restricts your account. If settling 2 disputes keeps you under 1.5%,
that's $100 in settlements vs potentially thousands in HDR fees.

Terminology:
  - Dispute rate = (disputes filed) / (transactions completed) × 100%
  - Rolling window = last 30 calendar days
  - HDR = High Dispute Rate program
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional


class ThresholdTier(str, Enum):
    NORMAL   = "NORMAL"    # < 0.5%
    ELEVATED = "ELEVATED"  # 0.5% – 0.99%
    LIMITED  = "LIMITED"   # 1.0% – 1.49%
    HDR      = "HDR"       # 1.5% – 2.99%
    CRITICAL = "CRITICAL"  # ≥ 3.0%


# PayPal threshold values
THRESHOLDS = {
    ThresholdTier.ELEVATED: 0.005,
    ThresholdTier.LIMITED:  0.010,
    ThresholdTier.HDR:      0.015,
    ThresholdTier.CRITICAL: 0.030,
}

# Monthly HDR fee per dispute while in HDR tier
HDR_FEE_PER_DISPUTE = 30.0


@dataclass
class DisputeRecord:
    """A single dispute in the rolling window."""
    dispute_id: str
    amount: float
    filed_date: str           # ISO 8601
    is_open: bool             # Still pending
    settlement_cost: float = 0.0  # 0 = fighting, >0 = estimated settle cost


@dataclass
class VelocityReport:
    current_rate: float                # e.g. 0.012 = 1.2%
    current_tier: ThresholdTier
    transactions_30d: int
    disputes_30d: int
    open_disputes: int

    # Headroom before next tier
    disputes_until_next_tier: int      # 0 if already at or above that tier
    next_tier: Optional[ThresholdTier]

    # Projection
    projected_tier_cross_date: Optional[str]   # ISO date — when next tier will be crossed
    daily_dispute_velocity: float              # Disputes per day (last 30d)

    # Financial exposure
    hdr_fee_if_crossed: float          # Total HDR fees if rate stays above 1.5%
    settle_vs_fight_recommendation: str

    # Settlement analysis (if open disputes provided)
    disputes_to_settle: list[str]      # dispute_ids that should be settled
    disputes_to_fight: list[str]       # dispute_ids worth fighting

    reasoning: list[str] = field(default_factory=list)


def analyze_velocity(
    transactions_30d: int,
    disputes_30d: int,
    *,
    open_disputes: Optional[list[DisputeRecord]] = None,
    daily_transaction_rate: float = 0.0,  # Average new transactions per day
    daily_dispute_rate_observed: float = 0.0,  # Average new disputes per day
    target_tier: ThresholdTier = ThresholdTier.NORMAL,  # What tier to stay under
) -> VelocityReport:
    """
    Analyze dispute velocity and forecast threshold crossings.

    The goal is to know, right now:
      - What tier are you in?
      - How many more disputes until you cross the next tier?
      - Which open disputes should you settle to stay safe?
      - When will you cross the next tier if velocity continues?
    """
    reasoning = []
    open_disputes = open_disputes or []

    if transactions_30d == 0:
        return VelocityReport(
            current_rate=0.0,
            current_tier=ThresholdTier.NORMAL,
            transactions_30d=0,
            disputes_30d=0,
            open_disputes=0,
            disputes_until_next_tier=0,
            next_tier=None,
            projected_tier_cross_date=None,
            daily_dispute_velocity=0.0,
            hdr_fee_if_crossed=0.0,
            settle_vs_fight_recommendation="Insufficient transaction data",
            disputes_to_settle=[],
            disputes_to_fight=[],
            reasoning=["No transactions in 30-day window — cannot compute rate"],
        )

    current_rate = disputes_30d / transactions_30d
    reasoning.append(
        f"Current rate: {disputes_30d}/{transactions_30d} = {current_rate:.3%}"
    )

    # ── Current tier ─────────────────────────────────────────────────────────
    if current_rate >= THRESHOLDS[ThresholdTier.CRITICAL]:
        current_tier = ThresholdTier.CRITICAL
    elif current_rate >= THRESHOLDS[ThresholdTier.HDR]:
        current_tier = ThresholdTier.HDR
    elif current_rate >= THRESHOLDS[ThresholdTier.LIMITED]:
        current_tier = ThresholdTier.LIMITED
    elif current_rate >= THRESHOLDS[ThresholdTier.ELEVATED]:
        current_tier = ThresholdTier.ELEVATED
    else:
        current_tier = ThresholdTier.NORMAL

    # ── Next tier and headroom ────────────────────────────────────────────────
    tier_order = [
        ThresholdTier.ELEVATED,
        ThresholdTier.LIMITED,
        ThresholdTier.HDR,
        ThresholdTier.CRITICAL,
    ]
    next_tier = None
    disputes_until_next = 0

    for tier in tier_order:
        if current_rate < THRESHOLDS[tier]:
            next_tier = tier
            next_threshold_count = THRESHOLDS[tier] * transactions_30d
            disputes_until_next = max(0, int(next_threshold_count - disputes_30d))
            reasoning.append(
                f"Next tier {tier.value} at {THRESHOLDS[tier]:.1%}: "
                f"{disputes_until_next} more disputes until crossed "
                f"(threshold = {next_threshold_count:.1f} disputes)"
            )
            break

    # ── Velocity projection ───────────────────────────────────────────────────
    if daily_dispute_rate_observed <= 0:
        daily_dispute_rate_observed = disputes_30d / 30.0

    daily_velocity = daily_dispute_rate_observed
    projected_cross_date = None

    if next_tier and daily_velocity > 0 and disputes_until_next > 0:
        days_to_cross = disputes_until_next / daily_velocity
        cross_date = datetime.now(timezone.utc) + timedelta(days=days_to_cross)
        projected_cross_date = cross_date.strftime("%Y-%m-%d")
        reasoning.append(
            f"At current velocity ({daily_velocity:.2f} disputes/day), "
            f"{next_tier.value} crossed in {days_to_cross:.0f} days ({projected_cross_date})"
        )

    # ── HDR financial exposure ────────────────────────────────────────────────
    # If in HDR, estimate monthly HDR fee cost
    hdr_fee = 0.0
    if current_tier in (ThresholdTier.HDR, ThresholdTier.CRITICAL):
        hdr_fee = disputes_30d * HDR_FEE_PER_DISPUTE
        reasoning.append(
            f"HDR tier: ${hdr_fee:.0f}/month in dispute fees "
            f"({disputes_30d} disputes × ${HDR_FEE_PER_DISPUTE})"
        )
    elif next_tier == ThresholdTier.HDR and disputes_until_next <= 3:
        hdr_fee = (disputes_30d + disputes_until_next) * HDR_FEE_PER_DISPUTE
        reasoning.append(
            f"WARNING: {disputes_until_next} disputes from HDR tier — "
            f"projected HDR fee: ${hdr_fee:.0f}/month"
        )

    # ── Open dispute triage ───────────────────────────────────────────────────
    to_settle = []
    to_fight = []

    if open_disputes:
        # Sort by settlement cost (cheapest to settle first)
        open_sorted = sorted(open_disputes, key=lambda d: d.settlement_cost)

        # How many can we absorb before crossing the target tier?
        target_threshold = THRESHOLDS.get(
            ThresholdTier(list(THRESHOLDS.keys())[list(THRESHOLDS.values()).index(
                min(v for v in THRESHOLDS.values() if v > current_rate)
            )]) if any(v > current_rate for v in THRESHOLDS.values()) else ThresholdTier.CRITICAL,
            THRESHOLDS[ThresholdTier.HDR]
        )
        # Simpler: just use the next tier threshold
        safe_dispute_budget = max(0, int(
            (THRESHOLDS.get(next_tier, 0.03) if next_tier else 0.03) * transactions_30d
        ) - disputes_30d - 1)  # -1 for safety buffer

        absorb_count = safe_dispute_budget
        for d in open_sorted:
            if absorb_count > 0:
                to_fight.append(d.dispute_id)
                absorb_count -= 1
            else:
                to_settle.append(d.dispute_id)

        if to_settle:
            settle_cost = sum(
                d.settlement_cost for d in open_disputes if d.dispute_id in to_settle
            )
            reasoning.append(
                f"Recommend settling {len(to_settle)} dispute(s) "
                f"(est. cost: ${settle_cost:.0f}) to stay under {next_tier.value if next_tier else 'HDR'} threshold"
            )

    # ── Recommendation ────────────────────────────────────────────────────────
    if current_tier == ThresholdTier.NORMAL and disputes_until_next > 5:
        recommendation = (
            f"Safe zone: {disputes_until_next} disputes of headroom before {next_tier.value if next_tier else 'next'} tier. "
            "Fight disputes on merit."
        )
    elif current_tier in (ThresholdTier.ELEVATED, ThresholdTier.LIMITED) or disputes_until_next <= 3:
        settle_total = sum(
            d.settlement_cost for d in open_disputes if d.dispute_id in to_settle
        ) if to_settle else 0
        recommendation = (
            f"CAUTION: {disputes_until_next} disputes until "
            f"{next_tier.value if next_tier else 'next'} tier. "
            f"Settle low-value open disputes to preserve headroom. "
            + (f"Estimated settle cost: ${settle_total:.0f}." if settle_total else "")
        )
    elif current_tier in (ThresholdTier.HDR, ThresholdTier.CRITICAL):
        recommendation = (
            f"CRITICAL: In {current_tier.value} tier. "
            f"${hdr_fee:.0f}/month in fees. Settle open disputes aggressively. "
            "Win rate must improve before fighting any new disputes."
        )
    else:
        recommendation = "Monitor dispute rate — approaching threshold."

    return VelocityReport(
        current_rate=round(current_rate, 5),
        current_tier=current_tier,
        transactions_30d=transactions_30d,
        disputes_30d=disputes_30d,
        open_disputes=len(open_disputes),
        disputes_until_next_tier=disputes_until_next,
        next_tier=next_tier,
        projected_tier_cross_date=projected_cross_date,
        daily_dispute_velocity=round(daily_velocity, 3),
        hdr_fee_if_crossed=round(hdr_fee, 2),
        settle_vs_fight_recommendation=recommendation,
        disputes_to_settle=to_settle,
        disputes_to_fight=to_fight,
        reasoning=reasoning,
    )


def threshold_safe_dispute_count(
    transactions_30d: int,
    tier: ThresholdTier = ThresholdTier.HDR,
) -> int:
    """How many disputes can be open before crossing the given tier threshold?"""
    return max(0, int(THRESHOLDS[tier] * transactions_30d) - 1)


if __name__ == "__main__":
    # Scenario: 800 transactions, 10 disputes — approaching LIMITED
    report = analyze_velocity(
        transactions_30d=800,
        disputes_30d=10,
        open_disputes=[
            DisputeRecord("D001", 45.0, "2026-11-01", True, settlement_cost=45.0),
            DisputeRecord("D002", 320.0, "2026-11-02", True, settlement_cost=0.0),
            DisputeRecord("D003", 80.0, "2026-11-03", True, settlement_cost=80.0),
        ],
        daily_transaction_rate=27.0,
        daily_dispute_rate_observed=0.4,
    )
    print(f"Rate: {report.current_rate:.2%} ({report.current_tier.value})")
    print(f"Headroom: {report.disputes_until_next_tier} disputes until {report.next_tier}")
    print(f"Projected cross: {report.projected_tier_cross_date}")
    print(f"Recommendation: {report.settle_vs_fight_recommendation}")
    print(f"Settle: {report.disputes_to_settle}")
    print(f"Fight: {report.disputes_to_fight}")
