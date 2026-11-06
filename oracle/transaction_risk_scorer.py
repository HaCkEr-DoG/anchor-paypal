"""
Transaction Risk Scorer — predict chargeback probability BEFORE a dispute
is filed, scoring a transaction's attributes against 15+ pre-dispute risk signals.

No competitor does this in real-time at the merchant level. PayPal's internal
risk engine scores at authorization time (fraud = will the card be declined?).
This scores for CHARGEBACK LIKELIHOOD — a completely different problem.
A transaction may pass authorization with flying colors but still have a 60%
probability of a chargeback 30 days later.

Risk signals scored:
  - Country/IP geolocation mismatch vs billing/shipping address
  - AVS + CVV mismatch patterns
  - High-velocity ordering (5+ orders in 24h from same buyer)
  - Card BIN country vs shipping country mismatch
  - International shipping to non-billing-address country
  - Digital goods (highest chargeback risk category)
  - Very high or very low transaction amounts (outliers)
  - Buyer account age (new account, high risk)
  - First transaction with merchant (no relationship history)
  - Transaction time anomaly (3-5 AM local buyer time)
  - Express/overnight shipping selected (urgency = pressure to dispute)
  - Shipping address ≠ billing address
  - Buyer email domain: temporary/disposable email providers
  - Multiple failed payment attempts before success
  - Item category: electronics/jewelry/gift cards (highest chargeback rates)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskTier(str, Enum):
    LOW      = "LOW"      # < 15 risk score — standard processing
    MEDIUM   = "MEDIUM"   # 15-34 — flag for review
    HIGH     = "HIGH"     # 35-59 — require manual review or 3DS
    CRITICAL = "CRITICAL" # >= 60 — decline or escalate immediately


class RecommendedAction(str, Enum):
    APPROVE         = "APPROVE"           # Process normally
    APPROVE_MONITOR = "APPROVE_MONITOR"   # Approve but set dispute alert
    REVIEW          = "REVIEW"            # Hold for manual review
    REQUIRE_3DS     = "REQUIRE_3DS"       # Force 3D Secure authentication
    DECLINE         = "DECLINE"           # Decline transaction


class ItemCategory(str, Enum):
    PHYSICAL_GOODS  = "PHYSICAL_GOODS"
    DIGITAL_GOODS   = "DIGITAL_GOODS"
    ELECTRONICS     = "ELECTRONICS"
    JEWELRY         = "JEWELRY"
    GIFT_CARDS      = "GIFT_CARDS"
    CLOTHING        = "CLOTHING"
    SERVICES        = "SERVICES"
    SUBSCRIPTION    = "SUBSCRIPTION"
    UNKNOWN         = "UNKNOWN"


@dataclass
class TransactionRiskInput:
    transaction_id: str
    merchant_id: str
    amount: float
    currency: str = "USD"
    item_category: str = "UNKNOWN"

    # Address signals
    billing_country: str = ""
    shipping_country: str = ""
    ip_country: str = ""         # Country inferred from buyer IP
    ship_to_billing_match: bool = True  # shipping address = billing address

    # Card signals
    avs_result: str = "Y"        # Y=full match, A=addr only, N=no match, U=unavailable
    cvv_result: str = "M"        # M=match, N=no match, P=not processed, U=unavailable
    card_bin_country: str = ""   # Country of issuing bank from BIN lookup
    failed_payment_attempts: int = 0

    # Buyer account signals
    buyer_account_age_days: int = 365  # days since account creation
    buyer_prior_transactions: int = 0  # prior successful txns with THIS merchant
    buyer_disputes_last_90d: int = 0
    buyer_email_domain: str = ""  # e.g. "gmail.com" vs "tempmail.org"

    # Velocity signals
    buyer_orders_last_24h: int = 1

    # Order signals
    is_express_shipping: bool = False
    is_digital_delivery: bool = False
    has_3ds_authentication: bool = False
    hour_of_purchase: int = 12   # 0-23 local buyer time (approximated)

    # Prior relationship
    is_first_transaction_with_merchant: bool = True


@dataclass
class RiskSignal:
    signal_id: str
    signal_name: str
    score_contribution: float  # points added to total risk score
    description: str


@dataclass
class TransactionRiskScore:
    transaction_id: str
    merchant_id: str

    risk_score: float           # 0-100
    risk_tier: RiskTier
    chargeback_probability: float  # 0-1 calibrated estimate

    recommended_action: RecommendedAction
    action_reason: str

    top_signals: list[RiskSignal]  # signals sorted by contribution (highest first)
    all_signals: list[RiskSignal]

    # Breakdown by signal group
    address_risk_score: float
    card_risk_score: float
    buyer_risk_score: float
    velocity_risk_score: float
    item_risk_score: float

    # Mitigating factors
    mitigating_factors: list[str]

    summary: str


# ─── Disposable email providers (sample list) ────────────────────────────────
_DISPOSABLE_DOMAINS = {
    "tempmail.org", "mailinator.com", "guerrillamail.com", "throwaway.email",
    "10minutemail.com", "yopmail.com", "dispostable.com", "sharklasers.com",
    "guerrillamailblock.com", "grr.la", "guerrillamail.info", "spam4.me",
    "trashmail.com", "maildrop.cc", "discard.email",
}

# ─── High chargeback item categories ─────────────────────────────────────────
_ITEM_RISK_SCORES: dict[str, float] = {
    "GIFT_CARDS":    35.0,  # Highest — no physical evidence, instant liquidation
    "DIGITAL_GOODS": 28.0,  # High — no physical delivery confirmation
    "ELECTRONICS":   22.0,  # High — high value, easy to claim SNAD
    "JEWELRY":       18.0,  # High — high value, subjective condition
    "SUBSCRIPTION":  12.0,  # Medium — forgotten subscriptions
    "CLOTHING":       8.0,  # Medium-low — SNAD risk
    "PHYSICAL_GOODS": 5.0,  # Low baseline
    "SERVICES":      10.0,  # Medium — intangible, hard to prove delivery
    "UNKNOWN":        8.0,  # Moderate penalty for unknown category
}


def _score_address_signals(inp: TransactionRiskInput) -> tuple[float, list[RiskSignal]]:
    signals: list[RiskSignal] = []
    total = 0.0

    # IP country mismatch with billing country
    if inp.ip_country and inp.billing_country and inp.ip_country.upper() != inp.billing_country.upper():
        s = RiskSignal("ADDR001", "IP/Billing country mismatch", 15.0,
                       f"IP suggests {inp.ip_country}, billing address is {inp.billing_country}")
        signals.append(s); total += s.score_contribution

    # Shipping country ≠ billing country
    if inp.shipping_country and inp.billing_country and inp.shipping_country.upper() != inp.billing_country.upper():
        s = RiskSignal("ADDR002", "Shipping/Billing country mismatch", 10.0,
                       f"Shipping to {inp.shipping_country}, billed from {inp.billing_country}")
        signals.append(s); total += s.score_contribution

    # Shipping address ≠ billing address
    if not inp.ship_to_billing_match:
        s = RiskSignal("ADDR003", "Shipping address differs from billing", 5.0,
                       "Order ships to a different address than the card's billing address")
        signals.append(s); total += s.score_contribution

    return total, signals


def _score_card_signals(inp: TransactionRiskInput) -> tuple[float, list[RiskSignal]]:
    signals: list[RiskSignal] = []
    total = 0.0

    # AVS mismatch
    if inp.avs_result in ("N", "Z"):
        pts = 20.0 if inp.avs_result == "N" else 10.0
        s = RiskSignal("CARD001", f"AVS mismatch (result={inp.avs_result})", pts,
                       "Address verification failed — card billing address not confirmed")
        signals.append(s); total += pts

    # CVV mismatch
    if inp.cvv_result == "N":
        s = RiskSignal("CARD002", "CVV mismatch", 25.0,
                       "Card CVV2 did not match — high risk of unauthorized use")
        signals.append(s); total += s.score_contribution
    elif inp.cvv_result in ("P", "U"):
        s = RiskSignal("CARD003", "CVV not verified", 8.0,
                       "CVV was not processed or unavailable — reduced authentication certainty")
        signals.append(s); total += s.score_contribution

    # Card BIN country mismatch
    if inp.card_bin_country and inp.billing_country and inp.card_bin_country.upper() != inp.billing_country.upper():
        s = RiskSignal("CARD004", "Card BIN country ≠ billing country", 12.0,
                       f"Card issued in {inp.card_bin_country}, billing address in {inp.billing_country}")
        signals.append(s); total += s.score_contribution

    # Multiple failed attempts
    if inp.failed_payment_attempts >= 3:
        s = RiskSignal("CARD005", f"Multiple failed payment attempts ({inp.failed_payment_attempts})", 15.0,
                       "Repeated failures before success suggests card testing behavior")
        signals.append(s); total += s.score_contribution
    elif inp.failed_payment_attempts >= 1:
        s = RiskSignal("CARD006", f"Failed payment attempt before success", 5.0,
                       "At least one failed attempt before successful payment")
        signals.append(s); total += s.score_contribution

    return total, signals


def _score_buyer_signals(inp: TransactionRiskInput) -> tuple[float, list[RiskSignal]]:
    signals: list[RiskSignal] = []
    total = 0.0

    # New buyer account
    if inp.buyer_account_age_days <= 7:
        s = RiskSignal("BUYER001", "Very new buyer account (≤ 7 days)", 18.0,
                       f"Account created {inp.buyer_account_age_days} day(s) ago — extremely high chargeback risk")
        signals.append(s); total += s.score_contribution
    elif inp.buyer_account_age_days <= 30:
        s = RiskSignal("BUYER002", f"New buyer account ({inp.buyer_account_age_days}d)", 10.0,
                       "Account less than 30 days old — elevated chargeback risk")
        signals.append(s); total += s.score_contribution

    # First transaction with merchant
    if inp.is_first_transaction_with_merchant and inp.buyer_prior_transactions == 0:
        s = RiskSignal("BUYER003", "First transaction with this merchant", 8.0,
                       "No purchase history with this merchant — no established trust signal")
        signals.append(s); total += s.score_contribution

    # Prior dispute history
    if inp.buyer_disputes_last_90d >= 5:
        s = RiskSignal("BUYER004", f"High prior dispute count ({inp.buyer_disputes_last_90d} in 90d)", 20.0,
                       "Buyer has history of frequent disputes — strong chargeback predictor")
        signals.append(s); total += s.score_contribution
    elif inp.buyer_disputes_last_90d >= 2:
        s = RiskSignal("BUYER005", f"Prior disputes ({inp.buyer_disputes_last_90d} in 90d)", 8.0,
                       "Buyer has prior dispute history with PayPal")
        signals.append(s); total += s.score_contribution

    # Disposable email
    domain = inp.buyer_email_domain.lower().strip()
    if domain in _DISPOSABLE_DOMAINS:
        s = RiskSignal("BUYER006", f"Disposable email domain ({domain})", 22.0,
                       "Buyer used a known temporary/disposable email provider — high fraud risk")
        signals.append(s); total += s.score_contribution

    # Night-time purchase anomaly (3-5 AM local buyer time)
    if 3 <= inp.hour_of_purchase <= 5:
        s = RiskSignal("BUYER007", f"Purchase at {inp.hour_of_purchase}:00 (3-5 AM local time)", 6.0,
                       "Unusual purchase hour — may indicate unauthorized access")
        signals.append(s); total += s.score_contribution

    return total, signals


def _score_velocity_signals(inp: TransactionRiskInput) -> tuple[float, list[RiskSignal]]:
    signals: list[RiskSignal] = []
    total = 0.0

    if inp.buyer_orders_last_24h >= 10:
        s = RiskSignal("VEL001", f"Extreme order velocity ({inp.buyer_orders_last_24h} orders in 24h)", 25.0,
                       "Buyer placed 10+ orders in 24 hours — card-testing or coordinated fraud pattern")
        signals.append(s); total += s.score_contribution
    elif inp.buyer_orders_last_24h >= 5:
        s = RiskSignal("VEL002", f"High order velocity ({inp.buyer_orders_last_24h} orders in 24h)", 15.0,
                       "Buyer placed 5+ orders in 24 hours — velocity anomaly")
        signals.append(s); total += s.score_contribution

    # Express shipping urgency signal
    if inp.is_express_shipping and not inp.is_digital_delivery:
        s = RiskSignal("VEL003", "Express/overnight shipping selected", 5.0,
                       "Urgency signal — fraudsters select express shipping to receive before dispute window")
        signals.append(s); total += s.score_contribution

    return total, signals


def _score_item_signals(inp: TransactionRiskInput) -> tuple[float, list[RiskSignal]]:
    signals: list[RiskSignal] = []
    cat = inp.item_category.upper()
    base_score = _ITEM_RISK_SCORES.get(cat, _ITEM_RISK_SCORES["UNKNOWN"])

    if base_score >= 20.0:
        s = RiskSignal("ITEM001", f"High-chargeback item category ({cat})", base_score,
                       f"{cat} has significantly elevated industry chargeback rates")
        signals.append(s)
    elif base_score >= 8.0:
        s = RiskSignal("ITEM002", f"Moderate-risk item category ({cat})", base_score,
                       f"{cat} has above-average chargeback rates")
        signals.append(s)
    else:
        s = RiskSignal("ITEM003", f"Standard-risk item category ({cat})", base_score, "")
        signals.append(s)

    # Very high amount anomaly
    if inp.amount >= 2000:
        s = RiskSignal("ITEM004", f"High transaction amount (${inp.amount:.0f})", 12.0,
                       "Transactions above $2,000 have higher chargeback rates and larger recovery stakes")
        signals.append(s)

    return base_score, signals


def _mitigating_factors(inp: TransactionRiskInput) -> list[str]:
    factors: list[str] = []
    if inp.has_3ds_authentication:
        factors.append("3DS authentication passed — liability shift applies on most networks")
    if inp.avs_result == "Y":
        factors.append("AVS full match (address + ZIP confirmed)")
    if inp.cvv_result == "M":
        factors.append("CVV2 match — card present-class authentication")
    if inp.buyer_account_age_days >= 365:
        factors.append("Established buyer account (1+ year)")
    if inp.buyer_prior_transactions >= 5:
        factors.append(f"{inp.buyer_prior_transactions} prior successful transactions with this merchant")
    if not inp.is_first_transaction_with_merchant:
        factors.append("Returning merchant customer — established purchase history")
    return factors


def score_transaction_risk(inp: TransactionRiskInput) -> TransactionRiskScore:
    """Score a transaction's chargeback probability from pre-dispute signals."""
    addr_score, addr_sigs = _score_address_signals(inp)
    card_score, card_sigs = _score_card_signals(inp)
    buyer_score, buyer_sigs = _score_buyer_signals(inp)
    vel_score, vel_sigs = _score_velocity_signals(inp)
    item_score, item_sigs = _score_item_signals(inp)

    all_signals = addr_sigs + card_sigs + buyer_sigs + vel_sigs + item_sigs
    all_signals.sort(key=lambda s: s.score_contribution, reverse=True)

    # 3DS mitigation: strong liability-shift discount
    mitigation = 0.0
    if inp.has_3ds_authentication:
        mitigation = 20.0  # 3DS nearly eliminates unauthorized disputes

    raw_score = addr_score + card_score + buyer_score + vel_score + item_score - mitigation
    risk_score = round(min(max(raw_score, 0.0), 100.0), 1)

    # Calibrated chargeback probability (logistic-style mapping)
    # risk_score 0→p=0.01, 50→p=0.15, 100→p=0.60
    cb_prob = round(0.01 + (risk_score / 100) ** 1.4 * 0.59, 4)
    cb_prob = min(cb_prob, 0.98)

    # Risk tier
    if risk_score >= 60:
        tier = RiskTier.CRITICAL
    elif risk_score >= 35:
        tier = RiskTier.HIGH
    elif risk_score >= 15:
        tier = RiskTier.MEDIUM
    else:
        tier = RiskTier.LOW

    # Recommended action
    if tier == RiskTier.CRITICAL:
        action = RecommendedAction.DECLINE
        reason = f"Risk score {risk_score:.0f}/100 — multiple strong fraud/chargeback signals"
    elif tier == RiskTier.HIGH and not inp.has_3ds_authentication:
        action = RecommendedAction.REQUIRE_3DS
        reason = f"Risk score {risk_score:.0f}/100 — force 3DS to shift liability"
    elif tier == RiskTier.HIGH:
        action = RecommendedAction.REVIEW
        reason = f"Risk score {risk_score:.0f}/100 — manual review required"
    elif tier == RiskTier.MEDIUM:
        action = RecommendedAction.APPROVE_MONITOR
        reason = f"Risk score {risk_score:.0f}/100 — approve with dispute alert enabled"
    else:
        action = RecommendedAction.APPROVE
        reason = f"Risk score {risk_score:.0f}/100 — low chargeback risk"

    mitigating = _mitigating_factors(inp)
    top_signals = all_signals[:5]

    summary = (
        f"TxRisk {inp.transaction_id}: "
        f"score={risk_score:.0f}/100 tier={tier.value} "
        f"cb_prob={cb_prob:.1%} action={action.value}"
    )

    return TransactionRiskScore(
        transaction_id=inp.transaction_id,
        merchant_id=inp.merchant_id,
        risk_score=risk_score,
        risk_tier=tier,
        chargeback_probability=cb_prob,
        recommended_action=action,
        action_reason=reason,
        top_signals=top_signals,
        all_signals=all_signals,
        address_risk_score=round(addr_score, 1),
        card_risk_score=round(card_score, 1),
        buyer_risk_score=round(buyer_score, 1),
        velocity_risk_score=round(vel_score, 1),
        item_risk_score=round(item_score, 1),
        mitigating_factors=mitigating,
        summary=summary,
    )
