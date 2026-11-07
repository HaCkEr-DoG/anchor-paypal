"""
Buyer Satisfaction Risk Scorer — predict post-dispute buyer outcomes.

After a dispute resolves, there are three outcomes a merchant doesn't want:
  1. Buyer churns (never orders again) — lost LTV
  2. Buyer leaves a negative review — brand damage
  3. Buyer escalates to bank chargeback — doubles the loss (refund + chargeback fee)

This module scores the risk of each outcome and recommends retention actions.

The key insight: how a dispute is RESOLVED matters more than who "wins."
A merchant who wins a dispute but leaves the buyer feeling dismissed will:
  - Lose all future LTV from that buyer
  - Get a 1-star review
  - Trigger a bank chargeback (outside PayPal's control)

A merchant who loses gracefully keeps the buyer relationship.

Risk signals scored:
  - Outcome type (WIN vs. REFUND vs. PARTIAL)
  - Response time (slow = buyer frustrated before resolution)
  - Communication quality (did merchant respond before escalation?)
  - Buyer LTV tier (high-value buyer loss = high impact)
  - Dispute reason code (UNAUTHORIZED disputes = more likely to churn)
  - Prior positive order history (cushions negative outcome)
  - Number of prior disputes with this merchant
  - Was refund immediate or delayed?
  - Was merchant's response empathetic or adversarial?

PayPal benefit:
  - Identifies buyers at chargeback risk (bank chargebacks outside PayPal)
    before they file, reducing PayPal's total loss exposure
  - Identifies merchants with poor post-dispute buyer handling as
    early indicators of declining merchant health
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeOutcome(str, Enum):
    WON_BY_MERCHANT     = "WON_BY_MERCHANT"
    FULL_REFUND         = "FULL_REFUND"
    PARTIAL_REFUND      = "PARTIAL_REFUND"
    ESCALATED_TO_PAYPAL = "ESCALATED_TO_PAYPAL"


class BuyerLTVTier(str, Enum):
    VIP         = "VIP"         # > $500 lifetime spend
    HIGH        = "HIGH"        # $200-$500
    MEDIUM      = "MEDIUM"      # $50-$200
    LOW         = "LOW"         # < $50
    NEGLIGIBLE  = "NEGLIGIBLE"  # first order only


class SatisfactionRiskTier(str, Enum):
    LOW      = "LOW"     # buyer likely stays
    MEDIUM   = "MEDIUM"  # buyer may churn; outreach recommended
    HIGH     = "HIGH"    # buyer likely churns; urgent outreach
    CRITICAL = "CRITICAL" # chargeback likely; immediate intervention


class RetentionActionType(str, Enum):
    SEND_APOLOGY_EMAIL          = "SEND_APOLOGY_EMAIL"
    OFFER_DISCOUNT_NEXT_ORDER   = "OFFER_DISCOUNT_NEXT_ORDER"
    IMMEDIATE_FULL_REFUND       = "IMMEDIATE_FULL_REFUND"
    PERSONAL_PHONE_CALL         = "PERSONAL_PHONE_CALL"
    UPGRADE_TO_VIP_PROGRAM      = "UPGRADE_TO_VIP_PROGRAM"
    MONITOR_FOR_BANK_CHARGEBACK = "MONITOR_FOR_BANK_CHARGEBACK"
    NO_ACTION_NEEDED            = "NO_ACTION_NEEDED"


@dataclass
class ResolvedDisputeContext:
    dispute_id: str
    merchant_id: str
    buyer_id: str

    # Dispute details
    reason_code: str            # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION
    dispute_outcome: DisputeOutcome
    dispute_amount: float
    days_to_resolution: int     # total days from open to resolved

    # Buyer profile
    buyer_ltv_tier: BuyerLTVTier
    buyer_total_orders_at_merchant: int
    buyer_prior_disputes_at_merchant: int   # before this one
    buyer_lifetime_spend: float = 0.0

    # Resolution quality
    merchant_responded_before_escalation: bool = True
    days_until_refund_issued: int = 0       # 0 = immediate; >7 = very slow
    was_tone_empathetic: bool = True        # did merchant communicate empathy?
    buyer_contacted_merchant_first: bool = True  # or went straight to PayPal?

    # Outcome context
    partial_refund_amount: float = 0.0
    is_repeat_issue: bool = False   # buyer had same issue type before


@dataclass
class RetentionAction:
    action_type: RetentionActionType
    urgency: str            # "IMMEDIATE" / "TODAY" / "THIS_WEEK"
    description: str
    expected_impact: str


@dataclass
class BuyerSatisfactionRiskReport:
    dispute_id: str
    merchant_id: str
    buyer_id: str

    # Scores (each 0.0 - 1.0)
    churn_probability: float
    negative_review_probability: float
    bank_chargeback_probability: float

    # Composite
    overall_satisfaction_risk: float    # max of the three
    risk_tier: SatisfactionRiskTier

    # Signals
    outcome_signal: float
    response_speed_signal: float
    empathy_signal: float
    ltv_signal: float               # weight of this buyer to the merchant
    prior_dispute_signal: float

    # Actions
    retention_actions: list[RetentionAction]

    key_insight: str
    summary: str


# ─── Signal functions ──────────────────────────────────────────────────────────

def _outcome_signal(ctx: ResolvedDisputeContext) -> float:
    """Higher signal = more buyer dissatisfaction risk."""
    if ctx.dispute_outcome == DisputeOutcome.WON_BY_MERCHANT:
        return 0.40    # buyer paid and got nothing back — highest churn risk
    if ctx.dispute_outcome == DisputeOutcome.PARTIAL_REFUND:
        partial_pct = ctx.partial_refund_amount / max(ctx.dispute_amount, 0.01)
        if partial_pct < 0.50:
            return 0.25
        return 0.10
    if ctx.dispute_outcome == DisputeOutcome.ESCALATED_TO_PAYPAL:
        return 0.30
    return 0.0  # FULL_REFUND = buyer satisfied


def _response_speed_signal(ctx: ResolvedDisputeContext) -> float:
    if ctx.days_to_resolution > 20:
        return 0.20
    if ctx.days_to_resolution > 10:
        return 0.10
    if ctx.days_to_resolution > 5:
        return 0.05
    return 0.0


def _empathy_signal(ctx: ResolvedDisputeContext) -> float:
    signal = 0.0
    if not ctx.was_tone_empathetic:
        signal += 0.15
    if not ctx.merchant_responded_before_escalation:
        signal += 0.10
    if ctx.days_until_refund_issued > 7:
        signal += 0.10
    if not ctx.buyer_contacted_merchant_first:
        signal += 0.05
    return signal


def _ltv_signal(ctx: ResolvedDisputeContext) -> float:
    """High LTV buyer = high risk weight (losing them costs more)."""
    tier_weights = {
        BuyerLTVTier.VIP:        0.0,   # VIP buyers get more patience
        BuyerLTVTier.HIGH:       0.05,
        BuyerLTVTier.MEDIUM:     0.03,
        BuyerLTVTier.LOW:        0.02,
        BuyerLTVTier.NEGLIGIBLE: 0.01,
    }
    return tier_weights.get(ctx.buyer_ltv_tier, 0.02)


def _prior_dispute_signal(ctx: ResolvedDisputeContext) -> float:
    """Repeat disputes at same merchant = buyer already frustrated."""
    if ctx.buyer_prior_disputes_at_merchant >= 2:
        return 0.15
    if ctx.buyer_prior_disputes_at_merchant == 1:
        return 0.08
    return 0.0


def _repeat_issue_signal(ctx: ResolvedDisputeContext) -> float:
    return 0.10 if ctx.is_repeat_issue else 0.0


def _churn_probability(
    outcome: float, speed: float, empathy: float, ltv: float, prior: float, repeat: float,
) -> float:
    # Churn is driven mostly by outcome + empathy
    raw = (outcome * 1.2) + (empathy * 1.0) + (speed * 0.6) + (prior * 0.8) + repeat
    return round(min(0.95, raw), 4)


def _review_probability(
    outcome: float, empathy: float, prior: float, repeat: float,
) -> float:
    # Reviews driven by emotional frustration: empathy failure + bad outcome
    raw = (outcome * 0.7) + (empathy * 1.0) + (prior * 0.5) + (repeat * 0.4)
    return round(min(0.90, raw), 4)


def _chargeback_probability(
    outcome: float, empathy: float, reason_code: str, prior: float,
) -> float:
    # Bank chargebacks: buyer didn't get refund AND is motivated to escalate
    unauth_boost = 0.15 if reason_code == "UNAUTHORIZED" else 0.0
    raw = (outcome * 1.0) + (empathy * 0.5) + prior * 0.5 + unauth_boost
    return round(min(0.90, raw), 4)


def _tier(risk: float) -> SatisfactionRiskTier:
    if risk < 0.20:
        return SatisfactionRiskTier.LOW
    if risk < 0.40:
        return SatisfactionRiskTier.MEDIUM
    if risk < 0.60:
        return SatisfactionRiskTier.HIGH
    return SatisfactionRiskTier.CRITICAL


def _build_actions(
    ctx: ResolvedDisputeContext,
    churn_prob: float,
    cb_prob: float,
    tier: SatisfactionRiskTier,
) -> list[RetentionAction]:
    actions = []
    is_vip_or_high = ctx.buyer_ltv_tier in (BuyerLTVTier.VIP, BuyerLTVTier.HIGH)
    urgent = tier == SatisfactionRiskTier.CRITICAL

    if tier == SatisfactionRiskTier.LOW:
        actions.append(RetentionAction(
            action_type=RetentionActionType.NO_ACTION_NEEDED,
            urgency="N/A",
            description="Buyer satisfaction risk is low. No immediate action required.",
            expected_impact="Maintains current relationship",
        ))
        return actions

    if cb_prob >= 0.30:
        actions.append(RetentionAction(
            action_type=RetentionActionType.MONITOR_FOR_BANK_CHARGEBACK,
            urgency="IMMEDIATE",
            description=(
                f"Bank chargeback probability is {cb_prob:.0%}. Monitor this buyer's "
                f"transaction for a chargeback outside PayPal in the next 60 days. "
                f"Flag for dispute team watch."
            ),
            expected_impact="Early detection of bank chargeback allows evidence preparation",
        ))

    if churn_prob >= 0.40 and is_vip_or_high:
        actions.append(RetentionAction(
            action_type=RetentionActionType.PERSONAL_PHONE_CALL,
            urgency="TODAY" if urgent else "THIS_WEEK",
            description=(
                f"VIP/HIGH-value buyer (LTV: {ctx.buyer_ltv_tier.value}) has high churn risk. "
                f"A personal call acknowledging the dispute experience and offering resolution "
                f"retains significantly more LTV than an email."
            ),
            expected_impact="Retains VIP buyer relationship and prevents bank chargeback",
        ))
    elif churn_prob >= 0.35:
        actions.append(RetentionAction(
            action_type=RetentionActionType.SEND_APOLOGY_EMAIL,
            urgency="TODAY" if urgent else "THIS_WEEK",
            description=(
                "Send a personalized apology email acknowledging the buyer's experience, "
                "not just a boilerplate message. Mention the specific order."
            ),
            expected_impact="Reduces churn probability by 15-25%",
        ))

    if ctx.dispute_outcome == DisputeOutcome.WON_BY_MERCHANT and churn_prob >= 0.30:
        actions.append(RetentionAction(
            action_type=RetentionActionType.OFFER_DISCOUNT_NEXT_ORDER,
            urgency="THIS_WEEK",
            description=(
                "Merchant won the dispute, but buyer received nothing. "
                "Offer a 10-15% discount on next order as a goodwill gesture — "
                "this converts a frustrated buyer into a retained customer."
            ),
            expected_impact="Converts dispute winner into retained customer; prevents churn",
        ))

    if is_vip_or_high and ctx.buyer_total_orders_at_merchant >= 5:
        actions.append(RetentionAction(
            action_type=RetentionActionType.UPGRADE_TO_VIP_PROGRAM,
            urgency="THIS_WEEK",
            description=(
                "Long-term buyer with high LTV. Offer VIP status upgrade (priority support, "
                "free returns) to reinforce loyalty after a negative experience."
            ),
            expected_impact="Increases long-term retention for high-value buyer",
        ))

    return actions[:3]


# ─── Main function ─────────────────────────────────────────────────────────────

def score_buyer_satisfaction_risk(ctx: ResolvedDisputeContext) -> BuyerSatisfactionRiskReport:
    """
    Score the risk that a resolved dispute leads to buyer churn,
    negative review, or bank chargeback.
    """
    outcome_s   = _outcome_signal(ctx)
    speed_s     = _response_speed_signal(ctx)
    empathy_s   = _empathy_signal(ctx)
    ltv_s       = _ltv_signal(ctx)
    prior_s     = _prior_dispute_signal(ctx)
    repeat_s    = _repeat_issue_signal(ctx)

    churn_prob  = _churn_probability(outcome_s, speed_s, empathy_s, ltv_s, prior_s, repeat_s)
    review_prob = _review_probability(outcome_s, empathy_s, prior_s, repeat_s)
    cb_prob     = _chargeback_probability(outcome_s, empathy_s, ctx.reason_code, prior_s)

    overall = round(max(churn_prob, review_prob, cb_prob), 4)
    tier = _tier(overall)

    actions = _build_actions(ctx, churn_prob, cb_prob, tier)

    # Key insight
    if tier == SatisfactionRiskTier.CRITICAL:
        key_insight = (
            f"Critical buyer retention risk: churn {churn_prob:.0%}, "
            f"bank CB {cb_prob:.0%}. Immediate outreach required. "
            f"Buyer LTV at risk: {ctx.buyer_ltv_tier.value}."
        )
    elif tier == SatisfactionRiskTier.HIGH:
        key_insight = (
            f"High churn risk ({churn_prob:.0%}) after {ctx.dispute_outcome.value} outcome. "
            f"Buyer satisfaction outreach recommended this week."
        )
    elif tier == SatisfactionRiskTier.MEDIUM:
        key_insight = (
            f"Moderate satisfaction risk. Buyer may churn ({churn_prob:.0%}). "
            f"A goodwill gesture (email or discount) would reduce this."
        )
    else:
        key_insight = (
            f"Low satisfaction risk. Dispute resolved with minimal buyer impact. "
            f"No immediate retention action needed."
        )

    summary = (
        f"BuyerRisk {ctx.dispute_id}: tier={tier.value} "
        f"churn={churn_prob:.0%} review={review_prob:.0%} cb={cb_prob:.0%} "
        f"ltv={ctx.buyer_ltv_tier.value} outcome={ctx.dispute_outcome.value}"
    )

    return BuyerSatisfactionRiskReport(
        dispute_id=ctx.dispute_id,
        merchant_id=ctx.merchant_id,
        buyer_id=ctx.buyer_id,
        churn_probability=churn_prob,
        negative_review_probability=review_prob,
        bank_chargeback_probability=cb_prob,
        overall_satisfaction_risk=overall,
        risk_tier=tier,
        outcome_signal=outcome_s,
        response_speed_signal=speed_s,
        empathy_signal=empathy_s,
        ltv_signal=ltv_s,
        prior_dispute_signal=prior_s,
        retention_actions=actions,
        key_insight=key_insight,
        summary=summary,
    )
