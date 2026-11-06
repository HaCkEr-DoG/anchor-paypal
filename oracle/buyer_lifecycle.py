"""
Buyer Lifecycle Engine — tracks and manages the full buyer relationship arc.

Most dispute systems treat each dispute in isolation.
This module tracks the RELATIONSHIP: a buyer who files one dispute is
different from a buyer who files their fourth this quarter.

Lifecycle stages (in order of trust degradation):
  FIRST_TIME     → New buyer, give benefit of the doubt
  REPEAT_CLEAN   → Multiple orders, zero disputes
  VIP            → High-spend, repeat buyer with no disputes
  ELEVATED_WATCH → 1 dispute, could be legitimate
  SUSPICIOUS     → 2+ disputes OR pattern signals fraud
  BLOCKED        → Confirmed fraudster, refuse orders

The key insight: dispute PREVENTION is more valuable than dispute winning.
Once a buyer is SUSPICIOUS, every order they place has an expected loss
attached to it. For a SUSPICIOUS buyer with 50% dispute rate on $200 orders:
  EV(accept order) = $200 × 0.5 × (1 - win_rate) = negative at any win rate under 100%

This module calculates:
  1. Current lifecycle stage
  2. Order handling recommendation (accept/review/reject)
  3. Lifetime value and estimated future dispute cost
  4. Per-stage thresholds for escalation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import date, timedelta


class BuyerStage(str, Enum):
    FIRST_TIME     = "FIRST_TIME"      # < 2 orders, 0 disputes
    REPEAT_CLEAN   = "REPEAT_CLEAN"    # 2+ orders, 0 disputes
    VIP            = "VIP"             # 5+ orders, $500+ spend, 0 disputes
    ELEVATED_WATCH = "ELEVATED_WATCH"  # 1 dispute
    SUSPICIOUS     = "SUSPICIOUS"      # 2+ disputes OR fraud pattern
    BLOCKED        = "BLOCKED"         # Confirmed fraudster


class OrderHandling(str, Enum):
    ACCEPT           = "ACCEPT"           # Process normally
    ACCEPT_WITH_HOLD = "ACCEPT_WITH_HOLD" # Accept but hold fulfillment 24h for review
    REQUIRE_REVIEW   = "REQUIRE_REVIEW"   # Manual review before shipping
    REQUIRE_SIGNATURE = "REQUIRE_SIGNATURE" # Require signature on delivery
    REJECT           = "REJECT"           # Refuse order


@dataclass
class BuyerRecord:
    """Everything we know about a buyer."""
    buyer_id: str
    email: str
    paypal_account_age_days: int
    total_orders: int
    total_spend: float
    # Dispute history
    total_disputes: int
    disputes_last_30d: int
    disputes_last_90d: int
    disputes_last_12m: int
    # Win/loss on disputes
    disputes_won_by_merchant: int    # We won against this buyer
    disputes_lost_by_merchant: int   # Buyer won against us
    # Order details
    avg_order_value: float
    max_order_value: float
    # Fraud signals
    is_flagged_by_network: bool = False   # Cross-merchant fraud network
    fraud_pattern_detected: Optional[str] = None   # Pattern type if any
    # Relationship date
    first_order_date: Optional[date] = None
    last_order_date: Optional[date] = None


@dataclass
class BuyerLifecycleProfile:
    """
    Current lifecycle assessment and order handling guidance for a buyer.
    """
    buyer_id: str
    email: str
    stage: BuyerStage

    # Scores
    trust_score: float          # 0–100, higher = more trustworthy
    dispute_rate: float         # disputes / total_orders
    merchant_win_rate: float    # % of disputes merchant has won against this buyer

    # Order handling
    order_recommendation: OrderHandling
    order_recommendation_reason: str
    max_safe_order_value: float    # Max order to accept without review

    # Lifetime value
    lifetime_value: float          # Total spend to date
    projected_lifetime_value: float  # Estimated future value
    dispute_cost_to_date: float     # Estimated cost of past disputes

    # Stage escalation
    next_stage: Optional[BuyerStage]       # What stage they're heading toward
    next_stage_trigger: Optional[str]      # What would push them to next stage
    de_escalation_possible: bool           # Can they earn back trust?
    de_escalation_path: Optional[str]      # How to earn back trust

    # Risk
    expected_loss_next_order: float        # EV of loss on a typical order from this buyer
    risk_flags: list[str] = field(default_factory=list)

    reasoning: list[str] = field(default_factory=list)


def assess_buyer(
    record: BuyerRecord,
    today: Optional[date] = None,
    dispute_fight_cost: float = 25.0,
) -> BuyerLifecycleProfile:
    """
    Assess a buyer's current lifecycle stage and generate order handling guidance.

    The assessment uses a 100-point trust score:
      - Base: 50 points
      - Dispute rate penalty: up to -40 points
      - Account age bonus: up to +10 points
      - Spend bonus: up to +10 points
      - Win rate adjustment: up to ±10 points
      - Fraud network penalty: -25 points
    """
    if today is None:
        today = date.today()

    reasoning: list[str] = []
    risk_flags: list[str] = []

    # Dispute rate
    if record.total_orders > 0:
        dispute_rate = record.total_disputes / record.total_orders
    else:
        dispute_rate = 0.0

    # Merchant win rate against this buyer
    total_resolved = record.disputes_won_by_merchant + record.disputes_lost_by_merchant
    if total_resolved > 0:
        merchant_win_rate = record.disputes_won_by_merchant / total_resolved
    else:
        merchant_win_rate = 0.65  # Assume average

    # Trust score computation
    trust_score = 50.0

    # Account age bonus (new accounts are risky)
    if record.paypal_account_age_days >= 365:
        trust_score += 10.0
        reasoning.append("Account age >= 1yr: +10 trust")
    elif record.paypal_account_age_days >= 90:
        trust_score += 5.0
        reasoning.append("Account age 90-365d: +5 trust")
    elif record.paypal_account_age_days < 30:
        trust_score -= 10.0
        risk_flags.append("NEW_ACCOUNT: PayPal account < 30 days old")
        reasoning.append("New account < 30d: -10 trust")

    # Dispute rate penalty
    if dispute_rate == 0:
        trust_score += 10.0
        reasoning.append("Zero disputes: +10 trust")
    elif dispute_rate <= 0.05:
        trust_score -= 5.0
        reasoning.append(f"Low dispute rate {dispute_rate:.1%}: -5 trust")
    elif dispute_rate <= 0.15:
        trust_score -= 20.0
        risk_flags.append(f"ELEVATED_DISPUTE_RATE: {dispute_rate:.1%}")
        reasoning.append(f"Elevated dispute rate {dispute_rate:.1%}: -20 trust")
    elif dispute_rate <= 0.30:
        trust_score -= 35.0
        risk_flags.append(f"HIGH_DISPUTE_RATE: {dispute_rate:.1%}")
        reasoning.append(f"High dispute rate {dispute_rate:.1%}: -35 trust")
    else:
        trust_score -= 40.0
        risk_flags.append(f"EXTREME_DISPUTE_RATE: {dispute_rate:.1%} — almost certain fraud")
        reasoning.append(f"Extreme dispute rate {dispute_rate:.1%}: -40 trust")

    # Spend bonus (high-value buyers are usually legitimate)
    if record.total_spend >= 1000:
        trust_score += 10.0
        reasoning.append(f"High spend ${record.total_spend:.0f}: +10 trust")
    elif record.total_spend >= 300:
        trust_score += 5.0
        reasoning.append(f"Moderate spend ${record.total_spend:.0f}: +5 trust")

    # Recent velocity flag
    if record.disputes_last_30d >= 2:
        trust_score -= 15.0
        risk_flags.append(f"VELOCITY_SPIKE: {record.disputes_last_30d} disputes in 30d")
        reasoning.append(f"Recent dispute spike {record.disputes_last_30d} in 30d: -15 trust")
    elif record.disputes_last_30d >= 1 and record.disputes_last_90d >= 2:
        trust_score -= 10.0
        reasoning.append("Accelerating dispute pattern: -10 trust")

    # Fraud network
    if record.is_flagged_by_network:
        trust_score -= 25.0
        risk_flags.append("CROSS_MERCHANT_FRAUD_FLAG: Flagged by cross-merchant fraud network")
        reasoning.append("Cross-merchant fraud flag: -25 trust")

    # Fraud pattern
    if record.fraud_pattern_detected:
        trust_score -= 20.0
        risk_flags.append(f"FRAUD_PATTERN: {record.fraud_pattern_detected}")
        reasoning.append(f"Fraud pattern detected ({record.fraud_pattern_detected}): -20 trust")

    trust_score = max(0.0, min(100.0, trust_score))

    # Determine lifecycle stage
    stage = _compute_stage(record, trust_score, dispute_rate, risk_flags)

    # Order recommendation
    recommendation, reason, max_order = _order_recommendation(stage, record, trust_score)

    # Lifetime value
    lifetime_value = record.total_spend
    dispute_cost_to_date = record.total_disputes * dispute_fight_cost + record.disputes_lost_by_merchant * record.avg_order_value

    # Projected LTV (simple model)
    if stage in (BuyerStage.BLOCKED, BuyerStage.SUSPICIOUS):
        projected_ltv = 0.0  # Won't do business with them
    elif stage == BuyerStage.VIP:
        projected_ltv = lifetime_value * 2.0  # VIPs typically stick around
    elif stage == BuyerStage.REPEAT_CLEAN:
        projected_ltv = lifetime_value * 1.5
    else:
        projected_ltv = lifetime_value * 1.2

    # Expected loss on next order from this buyer
    expected_loss = _expected_loss(record.avg_order_value, dispute_rate, merchant_win_rate, dispute_fight_cost)

    # Stage progression
    next_stage, trigger, de_esc_possible, de_esc_path = _stage_progression(stage, record)

    return BuyerLifecycleProfile(
        buyer_id=record.buyer_id,
        email=record.email,
        stage=stage,
        trust_score=round(trust_score, 1),
        dispute_rate=round(dispute_rate, 4),
        merchant_win_rate=round(merchant_win_rate, 3),
        order_recommendation=recommendation,
        order_recommendation_reason=reason,
        max_safe_order_value=max_order,
        lifetime_value=round(lifetime_value, 2),
        projected_lifetime_value=round(projected_ltv, 2),
        dispute_cost_to_date=round(dispute_cost_to_date, 2),
        next_stage=next_stage,
        next_stage_trigger=trigger,
        de_escalation_possible=de_esc_possible,
        de_escalation_path=de_esc_path,
        expected_loss_next_order=round(expected_loss, 2),
        risk_flags=risk_flags,
        reasoning=reasoning,
    )


def _compute_stage(
    record: BuyerRecord,
    trust_score: float,
    dispute_rate: float,
    risk_flags: list[str],
) -> BuyerStage:
    """Determine lifecycle stage from buyer signals."""
    # Hard block conditions
    if record.is_flagged_by_network and record.total_disputes >= 2:
        return BuyerStage.BLOCKED
    if record.fraud_pattern_detected and record.total_disputes >= 3:
        return BuyerStage.BLOCKED
    if trust_score < 15:
        return BuyerStage.BLOCKED

    # Suspicious
    if record.disputes_last_30d >= 2:
        return BuyerStage.SUSPICIOUS
    if record.total_disputes >= 2 and dispute_rate >= 0.15:
        return BuyerStage.SUSPICIOUS
    if record.is_flagged_by_network:
        return BuyerStage.SUSPICIOUS
    if trust_score < 30:
        return BuyerStage.SUSPICIOUS

    # Elevated watch
    if record.total_disputes == 1:
        return BuyerStage.ELEVATED_WATCH
    if trust_score < 50:
        return BuyerStage.ELEVATED_WATCH

    # VIP
    if (record.total_orders >= 5
            and record.total_spend >= 500
            and record.total_disputes == 0):
        return BuyerStage.VIP

    # Repeat clean
    if record.total_orders >= 2 and record.total_disputes == 0:
        return BuyerStage.REPEAT_CLEAN

    # First time
    return BuyerStage.FIRST_TIME


def _order_recommendation(
    stage: BuyerStage,
    record: BuyerRecord,
    trust_score: float,
) -> tuple[OrderHandling, str, float]:
    """Return (recommendation, reason, max_safe_order_value)."""
    if stage == BuyerStage.BLOCKED:
        return (
            OrderHandling.REJECT,
            "Buyer is BLOCKED: confirmed fraud pattern or cross-merchant flag",
            0.0,
        )
    if stage == BuyerStage.SUSPICIOUS:
        return (
            OrderHandling.REQUIRE_REVIEW,
            f"Buyer is SUSPICIOUS (trust={trust_score:.0f}): manual review required before fulfillment",
            100.0,
        )
    if stage == BuyerStage.ELEVATED_WATCH:
        # Require signature for high-value orders, hold for review otherwise
        if record.avg_order_value >= 200:
            return (
                OrderHandling.REQUIRE_SIGNATURE,
                "ELEVATED_WATCH buyer: require signature confirmation on delivery",
                500.0,
            )
        return (
            OrderHandling.ACCEPT_WITH_HOLD,
            "ELEVATED_WATCH buyer: accept with 24h fulfillment hold",
            300.0,
        )
    if stage == BuyerStage.FIRST_TIME:
        # New buyers get normal service but with signature for high-value
        if record.avg_order_value >= 300:
            return (
                OrderHandling.REQUIRE_SIGNATURE,
                "FIRST_TIME buyer + high order value: require signature",
                500.0,
            )
        return (
            OrderHandling.ACCEPT,
            "FIRST_TIME buyer: standard processing",
            200.0,
        )
    if stage == BuyerStage.REPEAT_CLEAN:
        return (
            OrderHandling.ACCEPT,
            "REPEAT_CLEAN buyer: trusted, standard processing",
            1000.0,
        )
    # VIP
    return (
        OrderHandling.ACCEPT,
        "VIP buyer: expedited processing, premium service",
        float("inf"),
    )


def _expected_loss(
    avg_order_value: float,
    dispute_rate: float,
    merchant_win_rate: float,
    dispute_fight_cost: float,
) -> float:
    """
    Expected dollar loss per order from this buyer.
    = P(dispute) × (P(merchant loses) × order_value + fight_cost)
    """
    p_dispute = dispute_rate
    p_loss = 1.0 - merchant_win_rate
    return p_dispute * (p_loss * avg_order_value + dispute_fight_cost)


def _stage_progression(
    stage: BuyerStage,
    record: BuyerRecord,
) -> tuple[Optional[BuyerStage], Optional[str], bool, Optional[str]]:
    """Return (next_stage, trigger, de_escalation_possible, de_escalation_path)."""
    if stage == BuyerStage.VIP:
        return (
            BuyerStage.ELEVATED_WATCH,
            "Filing one dispute",
            False,
            None,
        )
    if stage == BuyerStage.REPEAT_CLEAN:
        return (
            BuyerStage.ELEVATED_WATCH,
            "Filing one dispute",
            False,
            None,
        )
    if stage == BuyerStage.FIRST_TIME:
        return (
            BuyerStage.REPEAT_CLEAN,
            "Completing second order with no disputes",
            False,
            None,
        )
    if stage == BuyerStage.ELEVATED_WATCH:
        return (
            BuyerStage.SUSPICIOUS,
            "Filing a second dispute OR dispute within 30 days of first",
            True,
            "Complete 3+ dispute-free orders over 90 days to return to REPEAT_CLEAN",
        )
    if stage == BuyerStage.SUSPICIOUS:
        return (
            BuyerStage.BLOCKED,
            "Third dispute, cross-merchant flag, or confirmed fraud pattern",
            True,
            "Contact account manager; requires manual review and 90-day dispute-free period",
        )
    # BLOCKED
    return (None, None, False, None)


def bulk_triage(
    buyers: list[BuyerRecord],
    today: Optional[date] = None,
) -> dict[str, list[BuyerLifecycleProfile]]:
    """
    Triage an entire buyer list, grouping profiles by stage.
    Returns dict keyed by stage name.
    """
    profiles = [assess_buyer(b, today=today) for b in buyers]
    result: dict[str, list[BuyerLifecycleProfile]] = {
        stage.value: [] for stage in BuyerStage
    }
    for p in profiles:
        result[p.stage.value].append(p)
    # Sort each bucket by trust_score
    for bucket in result.values():
        bucket.sort(key=lambda p: p.trust_score)
    return result
