"""
Dispute Predictor — scores an ORDER (not yet a dispute) for its probability
of becoming a dispute.

Most dispute management happens AFTER a dispute is filed.
The predictor acts BEFORE: at order placement, it computes a dispute
probability score that can gate fulfillment, trigger enhanced verification,
or flag for pre-emptive proactive contact.

Why this matters:
  P(dispute) × amount = expected loss per order
  At scale: 100 orders/day × $200 avg × 3% predicted dispute rate = $600/day at risk

The predictor uses:
  - Buyer risk signals (account age, prior disputes, network flags)
  - Product risk signals (category, return rate, price point)
  - Order structure signals (mismatch indicators, shipping address anomalies)
  - Carrier risk signals (selected carrier's historical dispute rate)
  - Timing signals (holiday surge, velocity patterns)

Output:
  - dispute_probability: 0.0–1.0
  - risk_tier: VERY_LOW / LOW / MODERATE / HIGH / CRITICAL
  - recommended_action: PROCESS / VERIFY_ADDRESS / REQUIRE_SIGNATURE /
                        HOLD_FOR_REVIEW / REJECT
  - contributing_factors: list of what drove the score
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import date


class DisputeRiskTier(str, Enum):
    VERY_LOW  = "VERY_LOW"    # < 1% predicted dispute rate
    LOW       = "LOW"         # 1–3%
    MODERATE  = "MODERATE"    # 3–8%
    HIGH      = "HIGH"        # 8–20%
    CRITICAL  = "CRITICAL"    # > 20%


class OrderAction(str, Enum):
    PROCESS            = "PROCESS"             # Normal fulfillment
    VERIFY_ADDRESS     = "VERIFY_ADDRESS"      # Confirm delivery address before shipping
    REQUIRE_SIGNATURE  = "REQUIRE_SIGNATURE"   # Require signature on delivery
    HOLD_FOR_REVIEW    = "HOLD_FOR_REVIEW"     # Manual review before fulfillment
    REJECT             = "REJECT"              # Refuse order entirely


# Product categories with their historical dispute rates
_CATEGORY_BASE_RATES = {
    "electronics":        0.045,
    "gift_cards":         0.080,
    "luxury":             0.035,
    "clothing":           0.025,
    "home_goods":         0.018,
    "books":              0.008,
    "software":           0.030,
    "collectibles":       0.040,
    "auto_parts":         0.022,
    "health_beauty":      0.020,
    "food":               0.012,
    "digital_goods":      0.055,  # High because no physical delivery
    "event_tickets":      0.070,
    "gaming":             0.038,
    "jewelry":            0.050,
    "general":            0.025,
}

# Carriers with their historical INR dispute rates
_CARRIER_DISPUTE_RATES = {
    "USPS_PRIORITY":      0.010,
    "USPS_FIRST_CLASS":   0.025,
    "FEDEX_EXPRESS":      0.008,
    "FEDEX_GROUND":       0.015,
    "UPS":                0.012,
    "DHL":                0.018,
    "AMAZON":             0.030,
    "ONTRAC":             0.055,
    "LASERSHIP":          0.080,
    "GENERIC":            0.030,
}


@dataclass
class OrderRiskInput:
    """Signals about an order at placement time."""
    order_id: str

    # Buyer signals
    buyer_account_age_days: int
    buyer_total_orders: int
    buyer_total_disputes: int
    buyer_disputes_last_90d: int
    buyer_is_network_flagged: bool = False
    buyer_dispute_rate: float = 0.0  # Total disputes / total orders

    # Order signals
    order_amount: float = 100.0
    product_category: str = "general"
    is_digital_goods: bool = False

    # Address / shipping signals
    billing_shipping_mismatch: bool = False    # Billing addr != shipping addr
    new_shipping_address: bool = False          # First time buyer uses this address
    shipping_to_freight_forwarder: bool = False # Signs of reshipping fraud

    # Carrier signals
    carrier: str = "GENERIC"

    # Timing signals
    is_holiday_period: bool = False       # Black Friday, Christmas, etc.
    orders_in_last_24h: int = 1           # How many orders this buyer placed today

    # Additional
    has_promo_code: bool = False          # Promo codes correlate with abuse
    is_first_order: bool = False


@dataclass
class DisputePrediction:
    """
    Pre-order dispute risk prediction.
    Generated at order placement before any dispute is filed.
    """
    order_id: str
    dispute_probability: float          # 0.0 – 1.0
    risk_tier: DisputeRiskTier
    recommended_action: OrderAction
    action_reason: str
    expected_loss: float                # dispute_probability × order_amount
    expected_loss_net_win: float        # dispute_probability × order_amount × (1 − avg_win_rate)

    # What drove the score
    contributing_factors: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    confidence: float = 0.70            # Confidence in the prediction


def predict_dispute_risk(
    order: OrderRiskInput,
    merchant_avg_win_rate: float = 0.65,
    today: Optional[date] = None,
) -> DisputePrediction:
    """
    Predict dispute probability for an order at placement time.

    Uses multiplicative risk factor model on top of a category base rate.
    """
    if today is None:
        today = date.today()

    factors: list[str] = []
    risk_flags: list[str] = []

    # Start with category base rate
    base_rate = _CATEGORY_BASE_RATES.get(order.product_category.lower(), 0.025)
    prob = base_rate

    # Carrier risk adjustment
    carrier_key = order.carrier.upper().replace(" ", "_")
    carrier_rate = _CARRIER_DISPUTE_RATES.get(carrier_key, 0.030)
    carrier_multiplier = carrier_rate / 0.025  # Relative to average carrier
    prob = prob * max(0.5, min(3.0, carrier_multiplier))
    if carrier_multiplier > 1.5:
        risk_flags.append(f"HIGH_RISK_CARRIER: {order.carrier} has {carrier_rate:.1%} dispute rate")
        factors.append(f"High-risk carrier {order.carrier}: +{(carrier_multiplier - 1.0) * 100:.0f}% dispute probability")

    # Buyer risk signals
    if order.buyer_account_age_days < 30:
        prob *= 2.5
        risk_flags.append("NEW_PAYPAL_ACCOUNT: < 30 days old")
        factors.append("New PayPal account (< 30 days): 2.5x risk")
    elif order.buyer_account_age_days < 90:
        prob *= 1.6
        factors.append("Young PayPal account (30-90 days): 1.6x risk")

    if order.buyer_dispute_rate > 0.20:
        prob *= 4.0
        risk_flags.append(f"EXTREME_BUYER_DISPUTE_RATE: {order.buyer_dispute_rate:.1%}")
        factors.append(f"Extreme buyer dispute rate {order.buyer_dispute_rate:.1%}: 4x risk")
    elif order.buyer_dispute_rate > 0.10:
        prob *= 2.5
        risk_flags.append(f"HIGH_BUYER_DISPUTE_RATE: {order.buyer_dispute_rate:.1%}")
        factors.append(f"High buyer dispute rate {order.buyer_dispute_rate:.1%}: 2.5x risk")
    elif order.buyer_dispute_rate > 0.05:
        prob *= 1.5
        factors.append(f"Elevated buyer dispute rate {order.buyer_dispute_rate:.1%}: 1.5x risk")

    if order.buyer_disputes_last_90d >= 2:
        prob *= 2.0
        risk_flags.append(f"RECENT_DISPUTE_VELOCITY: {order.buyer_disputes_last_90d} disputes in 90 days")
        factors.append(f"Recent dispute velocity ({order.buyer_disputes_last_90d} in 90d): 2x risk")

    if order.buyer_is_network_flagged:
        prob *= 3.0
        risk_flags.append("CROSS_MERCHANT_FRAUD_FLAG")
        factors.append("Cross-merchant fraud network flag: 3x risk")

    # Order structure signals
    if order.billing_shipping_mismatch:
        prob *= 1.8
        risk_flags.append("BILLING_SHIPPING_MISMATCH")
        factors.append("Billing/shipping address mismatch: 1.8x risk")

    if order.shipping_to_freight_forwarder:
        prob *= 2.5
        risk_flags.append("FREIGHT_FORWARDER_ADDRESS: Reshipping fraud indicator")
        factors.append("Freight forwarder shipping address: 2.5x risk")

    if order.new_shipping_address and order.buyer_total_orders >= 3:
        prob *= 1.4
        factors.append("New shipping address (account takeover signal): 1.4x risk")

    # Digital goods are high risk (no physical delivery to prove)
    # Floor applied AFTER all multiplications at the end
    _digital_floor = 0.06 if order.is_digital_goods else 0.0

    # High-value order increases dispute incentive
    if order.order_amount >= 500:
        prob *= 1.3
        factors.append(f"High-value order (${order.order_amount:.0f}): 1.3x risk")
    elif order.order_amount >= 200:
        prob *= 1.1

    # Holiday period
    if order.is_holiday_period:
        prob *= 1.4
        factors.append("Holiday period: 1.4x risk (elevated fraud activity)")

    # Order velocity
    if order.orders_in_last_24h >= 3:
        prob *= 1.8
        risk_flags.append(f"ORDER_VELOCITY_SPIKE: {order.orders_in_last_24h} orders in 24h")
        factors.append(f"Order velocity spike ({order.orders_in_last_24h} today): 1.8x risk")

    # Promo abuse
    if order.has_promo_code and order.buyer_total_orders <= 1:
        prob *= 1.3
        factors.append("Promo code on first order: 1.3x risk")

    # Trust discount for established clean buyers
    if order.buyer_total_orders >= 10 and order.buyer_dispute_rate == 0:
        prob *= 0.4
        factors.append(f"Established clean buyer ({order.buyer_total_orders} orders, 0 disputes): 0.4x risk")
    elif order.buyer_total_orders >= 5 and order.buyer_dispute_rate == 0:
        prob *= 0.6
        factors.append(f"Clean repeat buyer ({order.buyer_total_orders} orders): 0.6x risk")

    # Apply digital goods floor, then cap
    if order.is_digital_goods and prob < 0.06:
        prob = 0.06
        factors.append("Digital goods: minimum 6% dispute rate floor")

    prob = max(0.001, min(0.95, prob))

    # Risk tier
    if prob < 0.01:
        tier = DisputeRiskTier.VERY_LOW
    elif prob < 0.03:
        tier = DisputeRiskTier.LOW
    elif prob < 0.08:
        tier = DisputeRiskTier.MODERATE
    elif prob < 0.20:
        tier = DisputeRiskTier.HIGH
    else:
        tier = DisputeRiskTier.CRITICAL

    # Recommended action
    action, reason = _recommend_action(tier, order, risk_flags)

    # Expected loss
    expected_loss = prob * order.order_amount
    expected_loss_net = expected_loss * (1.0 - merchant_avg_win_rate)

    # Confidence (more signals = higher confidence)
    n_signals = sum([
        order.buyer_total_orders > 0,
        order.buyer_dispute_rate > 0,
        order.billing_shipping_mismatch,
        order.buyer_is_network_flagged,
        order.buyer_account_age_days < 365,
    ])
    confidence = min(0.90, 0.60 + n_signals * 0.06)

    return DisputePrediction(
        order_id=order.order_id,
        dispute_probability=round(prob, 4),
        risk_tier=tier,
        recommended_action=action,
        action_reason=reason,
        expected_loss=round(expected_loss, 2),
        expected_loss_net_win=round(expected_loss_net, 2),
        contributing_factors=factors,
        risk_flags=risk_flags,
        confidence=round(confidence, 2),
    )


def _recommend_action(
    tier: DisputeRiskTier,
    order: OrderRiskInput,
    risk_flags: list[str],
) -> tuple[OrderAction, str]:
    has_fraud_flag = any(
        "CROSS_MERCHANT_FRAUD_FLAG" in f or "FREIGHT_FORWARDER" in f
        for f in risk_flags
    )
    if has_fraud_flag:
        return (OrderAction.REJECT, "Hard fraud signals: cross-merchant flag or freight forwarder address")
    if tier == DisputeRiskTier.CRITICAL:
        return (OrderAction.HOLD_FOR_REVIEW, f"CRITICAL dispute risk — manual review before fulfillment")
    if tier == DisputeRiskTier.HIGH:
        if order.order_amount >= 300:
            return (OrderAction.HOLD_FOR_REVIEW, f"HIGH risk + high value: hold for review")
        return (OrderAction.REQUIRE_SIGNATURE, "HIGH dispute risk: require signature on delivery")
    if tier == DisputeRiskTier.MODERATE:
        if order.order_amount >= 500:
            return (OrderAction.REQUIRE_SIGNATURE, "Moderate risk + high value: require signature")
        return (OrderAction.VERIFY_ADDRESS, "Moderate dispute risk: verify shipping address")
    if tier == DisputeRiskTier.LOW:
        if "BILLING_SHIPPING_MISMATCH" in risk_flags:
            return (OrderAction.VERIFY_ADDRESS, "Low risk but address mismatch: verify before shipping")
        return (OrderAction.PROCESS, "Low dispute risk: process normally")
    # VERY_LOW
    return (OrderAction.PROCESS, "Very low dispute risk: process normally")


def batch_predict(
    orders: list[OrderRiskInput],
    merchant_avg_win_rate: float = 0.65,
    today: Optional[date] = None,
) -> list[DisputePrediction]:
    """Predict dispute risk for a batch of orders. Returns sorted by dispute_probability desc."""
    predictions = [predict_dispute_risk(o, merchant_avg_win_rate, today) for o in orders]
    predictions.sort(key=lambda p: p.dispute_probability, reverse=True)
    return predictions
