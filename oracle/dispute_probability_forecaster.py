"""
Dispute Probability Forecaster — post-fulfillment order scoring.

After an order ships, the dispute clock starts. This module scores each
shipped order's probability of generating a PayPal dispute in the next 30 days,
based on delivery-window signals.

Key distinction from transaction_risk_scorer.py (authorization-time fraud):
  This runs AFTER fulfillment. It scores for DISPUTE likelihood, not fraud.
  An order may pass all fraud checks yet still generate a dispute because:
    - The carrier lost the package
    - The product didn't match expectations
    - The buyer forgot the subscription charge
    - Delivery confirmation was never captured

Risk signals scored:
  - No tracking number attached (highest INR predictor)
  - Carrier shows delivery exception / delay
  - International shipment (no delivery confirmation available)
  - High-value order ($200+) with no signature confirmation
  - Digital goods (SNAD: not what I expected)
  - New buyer (no prior order history with merchant)
  - Peak season shipment (Nov-Dec: volume overloads carriers)
  - Express shipping selected but NOT delivered on time
  - Previous buyer dispute history at this merchant
  - Subscription recurring charge (buyer churn risk)
  - No buyer communication in past 7 days for delayed order
  - Buyer in high-dispute-rate country

Outputs per order:
  - dispute_probability: float 0.0–1.0
  - primary_dispute_type: most likely reason code
  - risk_tier: LOW/MEDIUM/HIGH/CRITICAL
  - prevention_actions: 1-3 specific steps to take now

PayPal benefit:
  Aggregate forecasts across all Anchor merchants give PayPal's Risk team
  a 30-day forward-looking view of expected dispute volume by category,
  allowing proactive carrier outreach and merchant alerting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeProbabilityTier(str, Enum):
    LOW      = "LOW"      # < 5% probability
    MEDIUM   = "MEDIUM"   # 5-15%
    HIGH     = "HIGH"     # 15-30%
    CRITICAL = "CRITICAL" # > 30%


class LikelyDisputeType(str, Enum):
    INR          = "INR"           # Item not received
    SNAD         = "SNAD"          # Significantly not as described
    UNAUTHORIZED = "UNAUTHORIZED"  # Buyer didn't recognize charge
    SUBSCRIPTION = "SUBSCRIPTION"  # Subscription cancellation
    NONE         = "NONE"          # Low probability — no primary type


class PreventionActionType(str, Enum):
    SEND_TRACKING_UPDATE    = "SEND_TRACKING_UPDATE"
    PROACTIVE_OUTREACH      = "PROACTIVE_OUTREACH"
    ESCALATE_WITH_CARRIER   = "ESCALATE_WITH_CARRIER"
    OFFER_PARTIAL_REFUND    = "OFFER_PARTIAL_REFUND"
    SEND_SUBSCRIPTION_NOTICE = "SEND_SUBSCRIPTION_NOTICE"
    ADD_TRACKING_TO_ORDER   = "ADD_TRACKING_TO_ORDER"
    REQUEST_SIGNATURE_CONF  = "REQUEST_SIGNATURE_CONF"
    CONFIRM_DELIVERY        = "CONFIRM_DELIVERY"


@dataclass
class OrderProfile:
    """Snapshot of a shipped order for dispute probability scoring."""
    order_id: str
    merchant_id: str
    buyer_id: str

    # Order basics
    amount: float
    is_digital: bool = False
    is_subscription: bool = False
    item_category: str = ""         # "electronics", "clothing", "gift_card", etc.

    # Fulfillment
    has_tracking: bool = False
    tracking_number: str = ""
    carrier: str = ""
    days_since_ship: int = 0        # 0 = just shipped
    delivery_status: str = ""       # "DELIVERED", "IN_TRANSIT", "EXCEPTION", "PENDING"
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    is_international: bool = False
    express_on_time: bool = True    # only relevant if express was selected

    # Buyer signals
    buyer_prior_disputes_at_merchant: int = 0
    buyer_total_orders_at_merchant: int = 1
    is_new_buyer: bool = True       # first order with this merchant
    days_since_last_buyer_contact: int = -1   # -1 = no contact needed

    # Timing
    is_peak_season: bool = False    # Nov-Dec
    subscription_days_until_renewal: int = -1  # -1 = not subscription


@dataclass
class PreventionAction:
    action_type: PreventionActionType
    urgency: str            # "IMMEDIATE" / "TODAY" / "THIS_WEEK"
    description: str


@dataclass
class DisputeForecast:
    order_id: str
    merchant_id: str

    dispute_probability: float          # 0.0 - 1.0
    probability_tier: DisputeProbabilityTier
    likely_dispute_type: LikelyDisputeType

    # Score breakdown (each 0.0–1.0, additive weights)
    no_tracking_signal: float
    delivery_exception_signal: float
    high_value_no_sig_signal: float
    digital_goods_signal: float
    new_buyer_signal: float
    prior_dispute_signal: float
    international_signal: float
    peak_season_signal: float
    subscription_signal: float

    prevention_actions: list[PreventionAction]
    key_risk_factor: str
    summary: str


# ─── Signal functions ──────────────────────────────────────────────────────────

def _no_tracking_signal(order: OrderProfile) -> float:
    if order.is_digital:
        return 0.0
    if not order.has_tracking:
        return 0.30
    if order.delivery_status == "EXCEPTION":
        return 0.25
    if order.delivery_status == "PENDING" and order.days_since_ship > 7:
        return 0.15
    return 0.0


def _delivery_exception_signal(order: OrderProfile) -> float:
    if order.delivery_status == "EXCEPTION":
        return 0.20
    if not order.express_on_time:
        return 0.10
    return 0.0


def _high_value_no_sig_signal(order: OrderProfile) -> float:
    if order.amount >= 500 and not order.has_signature_confirmation:
        return 0.15
    if (order.amount >= 200 and not order.has_delivery_confirmation
            and not order.has_signature_confirmation and not order.is_digital):
        return 0.08
    return 0.0


def _digital_goods_signal(order: OrderProfile) -> float:
    if order.is_digital:
        return 0.08
    if order.item_category in ("gift_card", "electronics", "jewelry"):
        return 0.06
    return 0.0


def _new_buyer_signal(order: OrderProfile) -> float:
    if order.is_new_buyer:
        return 0.06
    return 0.0


def _prior_dispute_signal(order: OrderProfile) -> float:
    if order.buyer_prior_disputes_at_merchant >= 3:
        return 0.25
    if order.buyer_prior_disputes_at_merchant >= 1:
        return 0.10
    return 0.0


def _international_signal(order: OrderProfile) -> float:
    if order.is_international and not order.has_delivery_confirmation:
        return 0.12
    if order.is_international:
        return 0.05
    return 0.0


def _peak_season_signal(order: OrderProfile) -> float:
    return 0.07 if order.is_peak_season else 0.0


def _subscription_signal(order: OrderProfile) -> float:
    if order.is_subscription and 0 <= order.subscription_days_until_renewal <= 3:
        return 0.15
    if order.is_subscription:
        return 0.05
    return 0.0


def _cap_probability(raw: float) -> float:
    return round(min(0.95, max(0.0, raw)), 4)


def _tier(prob: float) -> DisputeProbabilityTier:
    if prob < 0.05:
        return DisputeProbabilityTier.LOW
    if prob < 0.15:
        return DisputeProbabilityTier.MEDIUM
    if prob < 0.30:
        return DisputeProbabilityTier.HIGH
    return DisputeProbabilityTier.CRITICAL


def _likely_type(
    order: OrderProfile,
    no_tracking: float,
    exception: float,
    subscription: float,
    digital: float,
    high_value: float,
) -> LikelyDisputeType:
    scores = {
        LikelyDisputeType.INR: no_tracking + exception + high_value * 0.5,
        LikelyDisputeType.SNAD: digital + (0.10 if order.item_category in ("clothing", "electronics") else 0.0),
        LikelyDisputeType.SUBSCRIPTION: subscription,
        LikelyDisputeType.UNAUTHORIZED: 0.0,  # auth-time risk; scored elsewhere
    }
    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 0.03:
        return LikelyDisputeType.NONE
    return best


def _build_prevention_actions(
    order: OrderProfile,
    tier: DisputeProbabilityTier,
    likely_type: LikelyDisputeType,
    no_tracking: float,
    exception: float,
    subscription: float,
) -> list[PreventionAction]:
    actions = []
    urgent = tier == DisputeProbabilityTier.CRITICAL

    if no_tracking >= 0.25 and not order.has_tracking and not order.is_digital:
        actions.append(PreventionAction(
            action_type=PreventionActionType.ADD_TRACKING_TO_ORDER,
            urgency="IMMEDIATE",
            description="Add a tracking number to this order immediately — no tracking is the top INR predictor.",
        ))

    if exception >= 0.15 or (order.delivery_status == "EXCEPTION"):
        actions.append(PreventionAction(
            action_type=PreventionActionType.ESCALATE_WITH_CARRIER,
            urgency="TODAY",
            description=f"File a trace with {order.carrier or 'the carrier'} for this delivery exception before the buyer notices.",
        ))

    if order.has_tracking and order.days_since_ship > 5 and order.delivery_status not in ("DELIVERED",):
        actions.append(PreventionAction(
            action_type=PreventionActionType.PROACTIVE_OUTREACH,
            urgency="TODAY" if urgent else "THIS_WEEK",
            description="Send the buyer a proactive status update with tracking link before they worry.",
        ))

    if subscription >= 0.10 and order.is_subscription:
        actions.append(PreventionAction(
            action_type=PreventionActionType.SEND_SUBSCRIPTION_NOTICE,
            urgency="TODAY",
            description="Send a renewal reminder email with a clear cancellation link before billing.",
        ))

    if order.amount >= 200 and not order.has_delivery_confirmation and not order.is_digital:
        actions.append(PreventionAction(
            action_type=PreventionActionType.CONFIRM_DELIVERY,
            urgency="THIS_WEEK",
            description="Confirm delivery with the carrier — high-value order with no delivery confirmation on record.",
        ))

    if not actions and tier not in (DisputeProbabilityTier.LOW,):
        actions.append(PreventionAction(
            action_type=PreventionActionType.SEND_TRACKING_UPDATE,
            urgency="THIS_WEEK",
            description="Send a courtesy tracking/delivery confirmation email to this buyer.",
        ))

    return actions[:3]


def _key_risk_factor(
    no_tracking: float,
    exception: float,
    high_value: float,
    prior_dispute: float,
    subscription: float,
    international: float,
) -> str:
    signals = [
        (no_tracking, "No tracking number attached"),
        (exception, "Delivery exception / carrier delay"),
        (prior_dispute, "Buyer has prior dispute history"),
        (high_value, "High-value order without delivery confirmation"),
        (subscription, "Subscription charge at renewal window"),
        (international, "International shipment without delivery confirmation"),
    ]
    top = max(signals, key=lambda x: x[0])
    return top[1] if top[0] > 0 else "No significant risk factors"


# ─── Main function ─────────────────────────────────────────────────────────────

def forecast_dispute_probability(order: OrderProfile) -> DisputeForecast:
    """
    Score a shipped order's probability of generating a PayPal dispute
    in the next 30 days, with prevention actions to take now.
    """
    no_tracking   = _no_tracking_signal(order)
    exception     = _delivery_exception_signal(order)
    high_value    = _high_value_no_sig_signal(order)
    digital       = _digital_goods_signal(order)
    new_buyer     = _new_buyer_signal(order)
    prior_dispute = _prior_dispute_signal(order)
    international = _international_signal(order)
    peak          = _peak_season_signal(order)
    subscription  = _subscription_signal(order)

    raw_prob = (
        no_tracking + exception + high_value + digital +
        new_buyer + prior_dispute + international + peak + subscription
    )
    prob = _cap_probability(raw_prob)
    tier = _tier(prob)
    likely_type = _likely_type(order, no_tracking, exception, subscription, digital, high_value)

    prevention = _build_prevention_actions(order, tier, likely_type, no_tracking, exception, subscription)
    key_risk = _key_risk_factor(no_tracking, exception, high_value, prior_dispute, subscription, international)

    summary = (
        f"Order {order.order_id} [{order.merchant_id}]: "
        f"dispute_prob={prob:.1%} tier={tier.value} "
        f"type={likely_type.value} risk='{key_risk[:40]}'"
    )

    return DisputeForecast(
        order_id=order.order_id,
        merchant_id=order.merchant_id,
        dispute_probability=prob,
        probability_tier=tier,
        likely_dispute_type=likely_type,
        no_tracking_signal=no_tracking,
        delivery_exception_signal=exception,
        high_value_no_sig_signal=high_value,
        digital_goods_signal=digital,
        new_buyer_signal=new_buyer,
        prior_dispute_signal=prior_dispute,
        international_signal=international,
        peak_season_signal=peak,
        subscription_signal=subscription,
        prevention_actions=prevention,
        key_risk_factor=key_risk,
        summary=summary,
    )


def batch_forecast(orders: list[OrderProfile]) -> list[DisputeForecast]:
    """Score a batch of orders and return sorted by probability (highest first)."""
    results = [forecast_dispute_probability(o) for o in orders]
    results.sort(key=lambda r: r.dispute_probability, reverse=True)
    return results
