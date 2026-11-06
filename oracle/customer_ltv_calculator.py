"""
Customer LTV Calculator — estimate the lifetime value of a buyer to inform
dispute resolution decisions. High-LTV customers deserve more lenient
dispute handling; low-LTV or fraud-risk customers do not.

LTV estimation:
  historical_ltv = sum of past order values
  projected_ltv  = historical_ltv × churn_adjusted_multiplier
  adjusted_ltv   = projected_ltv × fraud_risk_discount

LTV tiers:
  VIP          ≥ $500 projected
  HIGH         $200 – $499
  MEDIUM       $50  – $199
  LOW          $10  – $49
  NEGLIGIBLE   < $10

Dispute thresholds (how much to concede to retain the customer):
  VIP          up to 100% of dispute amount
  HIGH         up to 70%
  MEDIUM       up to 40%
  LOW          up to 20%
  NEGLIGIBLE   0% (don't concede for retention)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LTVTier(str, Enum):
    VIP        = "VIP"
    HIGH       = "HIGH"
    MEDIUM     = "MEDIUM"
    LOW        = "LOW"
    NEGLIGIBLE = "NEGLIGIBLE"


_TIER_THRESHOLDS = [
    (500.0, LTVTier.VIP),
    (200.0, LTVTier.HIGH),
    (50.0,  LTVTier.MEDIUM),
    (10.0,  LTVTier.LOW),
]

_TIER_CONCESSION_PCT: dict[LTVTier, float] = {
    LTVTier.VIP:        1.00,
    LTVTier.HIGH:       0.70,
    LTVTier.MEDIUM:     0.40,
    LTVTier.LOW:        0.20,
    LTVTier.NEGLIGIBLE: 0.00,
}


@dataclass
class CustomerLTVInput:
    """Input for LTV calculation."""
    customer_id: str

    # Purchase history
    total_order_count: int = 0
    total_order_value: float = 0.0
    months_as_customer: int = 0

    # Recent activity
    orders_last_6_months: int = 0
    value_last_6_months: float = 0.0

    # Risk signals
    dispute_count: int = 0              # Historical dispute count
    dispute_rate: float = 0.0           # dispute_count / total_order_count
    fraud_risk_score: float = 0.0       # 0–100 (from buyer_fraud_detector)
    is_verified_buyer: bool = False
    has_payment_issues: bool = False

    # Context
    dispute_amount: float = 100.0       # Current dispute amount (for concession calc)
    is_vip_flagged: bool = False        # Explicitly flagged VIP by merchant


def _classify_ltv(projected_ltv: float, is_vip_flagged: bool) -> LTVTier:
    if is_vip_flagged:
        return LTVTier.VIP
    for threshold, tier in _TIER_THRESHOLDS:
        if projected_ltv >= threshold:
            return tier
    return LTVTier.NEGLIGIBLE


def _projected_ltv(inp: CustomerLTVInput) -> float:
    """
    Project future LTV from historical data.
    Uses recency-weighted average order value and estimated remaining months.
    """
    if inp.total_order_count == 0:
        return 0.0

    # Average order value
    aov = inp.total_order_value / inp.total_order_count

    # Monthly order frequency
    months = max(1, inp.months_as_customer)
    monthly_frequency = inp.total_order_count / months

    # Recency boost: if active in last 6 months, extrapolate
    if inp.orders_last_6_months > 0:
        recent_monthly = inp.orders_last_6_months / 6
        recent_aov = inp.value_last_6_months / inp.orders_last_6_months
        # Blend recent and historical
        monthly_frequency = (monthly_frequency + recent_monthly) / 2
        aov = (aov + recent_aov) / 2

    # Churn-adjusted projection: 24-month horizon, decayed by churn
    churn_factor = 1.0
    if inp.months_as_customer > 0:
        active_months = min(inp.months_as_customer, 24)
        # Simple decay: newer customers are assumed to have higher future LTV
        churn_factor = min(1.0, active_months / 24.0 + 0.4)

    projected = aov * monthly_frequency * 24 * churn_factor
    return round(projected, 2)


def _fraud_discount(inp: CustomerLTVInput) -> float:
    """Return a discount multiplier (0–1) based on fraud/dispute risk."""
    if inp.fraud_risk_score >= 70:
        return 0.0   # High fraud risk — LTV is worthless
    if inp.fraud_risk_score >= 40:
        return 0.3   # Medium fraud risk — heavy discount
    if inp.dispute_rate >= 0.20:  # > 20% orders are disputes
        return 0.4
    if inp.dispute_rate >= 0.10:
        return 0.7
    if inp.has_payment_issues:
        return 0.6
    return 1.0


@dataclass
class CustomerLTVReport:
    """Full LTV analysis for a customer."""
    customer_id: str

    # LTV estimates
    historical_ltv: float
    projected_ltv: float
    fraud_discounted_ltv: float
    fraud_discount_applied: float    # Multiplier used (0–1)

    # Tier
    ltv_tier: LTVTier
    tier_description: str

    # Dispute concession guidance
    max_concession_pct: float
    max_concession_amount: float     # Max $ to concede for this customer
    concession_rationale: str

    # Risk signals
    dispute_rate: float
    fraud_risk_score: float
    is_verified: bool

    # Guidance
    recommend_full_fight: bool       # True if LTV too low to concede
    recommend_vip_treatment: bool    # True if worth full concession

    immediate_actions: list[str]
    summary: str


def calculate_customer_ltv(inp: CustomerLTVInput) -> CustomerLTVReport:
    """
    Estimate customer LTV and derive dispute concession guidance.
    """
    historical_ltv = inp.total_order_value
    projected = _projected_ltv(inp)
    discount = _fraud_discount(inp)
    discounted_ltv = round(projected * discount, 2)

    tier = _classify_ltv(discounted_ltv, inp.is_vip_flagged)
    max_concession_pct = _TIER_CONCESSION_PCT[tier]
    max_concession_amount = round(max_concession_pct * inp.dispute_amount, 2)

    tier_desc = {
        LTVTier.VIP:        "VIP customer — concede up to 100% to retain",
        LTVTier.HIGH:       "High-value customer — concede up to 70%",
        LTVTier.MEDIUM:     "Medium-value customer — concede up to 40%",
        LTVTier.LOW:        "Low-value customer — concede up to 20%",
        LTVTier.NEGLIGIBLE: "Negligible value — do not concede for retention",
    }[tier]

    concession_rationale = (
        f"Projected LTV=${discounted_ltv:.2f} → {tier.value} tier → "
        f"max {max_concession_pct:.0%} concession on ${inp.dispute_amount:.2f} dispute"
    )

    recommend_full_fight = tier in (LTVTier.NEGLIGIBLE, LTVTier.LOW) or discount == 0.0
    recommend_vip = tier == LTVTier.VIP

    # Immediate actions
    actions: list[str] = []
    if discount == 0.0:
        actions.append("FRAUD: LTV discounted to zero — fight dispute, do not concede")
    elif tier == LTVTier.VIP:
        actions.append(f"VIP customer: offer full refund up to ${max_concession_amount:.2f} to retain")
    elif tier == LTVTier.HIGH:
        actions.append(f"High-value customer: consider partial refund up to ${max_concession_amount:.2f}")
    elif tier in (LTVTier.NEGLIGIBLE, LTVTier.LOW):
        actions.append(f"Low-value customer: fight dispute — not worth conceding for retention")
    else:
        actions.append(f"Medium-value customer: partial concession up to ${max_concession_amount:.2f}")

    if inp.dispute_rate >= 0.15:
        actions.append(f"High dispute rate ({inp.dispute_rate:.0%}) — treat with extra scrutiny")

    summary = (
        f"LTV {inp.customer_id}: "
        f"hist=${historical_ltv:.2f} | proj=${projected:.2f} | "
        f"adj=${discounted_ltv:.2f} | tier={tier.value} | "
        f"max_concession={max_concession_pct:.0%} (${max_concession_amount:.2f})"
    )

    return CustomerLTVReport(
        customer_id=inp.customer_id,
        historical_ltv=round(historical_ltv, 2),
        projected_ltv=projected,
        fraud_discounted_ltv=discounted_ltv,
        fraud_discount_applied=discount,
        ltv_tier=tier,
        tier_description=tier_desc,
        max_concession_pct=max_concession_pct,
        max_concession_amount=max_concession_amount,
        concession_rationale=concession_rationale,
        dispute_rate=inp.dispute_rate,
        fraud_risk_score=inp.fraud_risk_score,
        is_verified=inp.is_verified_buyer,
        recommend_full_fight=recommend_full_fight,
        recommend_vip_treatment=recommend_vip,
        immediate_actions=actions,
        summary=summary,
    )
