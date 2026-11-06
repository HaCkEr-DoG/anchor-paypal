"""
Friendly Fraud Scorer — estimate the probability that a dispute is
"friendly fraud" (also called first-party fraud): the buyer made a
legitimate purchase but is now disputing it to get a free item/refund.

Friendly fraud signals:
  - High delivery confirmation (item received but disputed)
  - High purchase history without prior disputes
  - Digital goods / no-return dispute
  - Short time between delivery and dispute
  - Buyer has prior successful dispute history
  - IP/device fingerprint matches prior successful transactions

This score influences fight vs. settle decisions across the system.
A high fraud score increases the value of fighting the dispute.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class FraudRiskLevel(str, Enum):
    VERY_HIGH  = "VERY_HIGH"   # ≥0.75 — almost certainly fraud
    HIGH       = "HIGH"        # 0.55–0.74
    MODERATE   = "MODERATE"    # 0.35–0.54
    LOW        = "LOW"         # 0.15–0.34
    VERY_LOW   = "VERY_LOW"    # <0.15 — genuine dispute likely


class RecommendedAction(str, Enum):
    FIGHT_AGGRESSIVELY = "FIGHT_AGGRESSIVELY"
    FIGHT              = "FIGHT"
    GATHER_EVIDENCE    = "GATHER_EVIDENCE"
    EVALUATE           = "EVALUATE"
    CONSIDER_SETTLE    = "CONSIDER_SETTLE"


@dataclass
class FriendlyFraudInput:
    """Input signals for friendly fraud scoring."""
    dispute_id: str
    claim_type: str              # INR / UNAUTHORIZED / SNAD / etc.
    disputed_amount: float

    # Delivery evidence
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    item_delivered_before_dispute: bool = False
    days_between_delivery_and_dispute: Optional[int] = None

    # Buyer history
    total_prior_orders: int = 0
    total_prior_disputes: int = 0
    prior_disputes_won_by_buyer: int = 0  # Buyer won chargeback previously
    is_repeat_dispute_same_merchant: bool = False
    account_age_days: int = 0

    # Order characteristics
    is_digital_goods: bool = False
    is_downloadable: bool = False
    item_accessed_after_purchase: bool = False  # e.g., app/digital was used
    has_usage_logs: bool = False

    # Device / location
    same_device_as_prior_orders: bool = True
    same_ip_as_prior_orders: bool = True
    shipping_matches_billing: bool = True

    # Merchant-side evidence
    has_buyer_communication: bool = False  # Buyer contacted support before dispute
    buyer_acknowledged_receipt: bool = False  # Email / message confirming delivery


def _base_score_from_claim(claim_type: str) -> float:
    """Base fraud suspicion by claim type."""
    return {
        "INR":          0.20,  # Item not received — common, often genuine
        "SNAD":         0.30,  # Item not as described — sometimes fraud
        "UNAUTHORIZED": 0.10,  # Unauthorized — usually genuine card fraud
        "DUPLICATE":    0.15,  # Duplicate — usually merchant error
        "SUBSCRIPTION": 0.25,  # Subscription — often buyer confusion
    }.get(claim_type.upper(), 0.25)


def _compute_fraud_score(inp: FriendlyFraudInput) -> float:
    score = _base_score_from_claim(inp.claim_type)

    # Strong signals of fraud
    if inp.has_delivery_confirmation:
        score += 0.25
    if inp.has_signature_confirmation:
        score += 0.10
    if inp.item_delivered_before_dispute and inp.days_between_delivery_and_dispute is not None:
        if inp.days_between_delivery_and_dispute <= 3:
            score += 0.15
        elif inp.days_between_delivery_and_dispute <= 7:
            score += 0.08
    if inp.buyer_acknowledged_receipt:
        score += 0.20

    # Digital goods signals
    if inp.is_digital_goods or inp.is_downloadable:
        score += 0.15
    if inp.item_accessed_after_purchase:
        score += 0.20
    if inp.has_usage_logs and inp.item_accessed_after_purchase:
        score += 0.10

    # Repeat / habitual disputer
    if inp.is_repeat_dispute_same_merchant:
        score += 0.20
    if inp.total_prior_orders > 0:
        dispute_rate = inp.total_prior_disputes / inp.total_prior_orders
        if dispute_rate > 0.20:
            score += 0.15
        elif dispute_rate > 0.10:
            score += 0.08
    if inp.prior_disputes_won_by_buyer >= 2:
        score += 0.12  # Pattern of winning chargebacks

    # New account risk
    if inp.account_age_days < 30:
        score += 0.10
    elif inp.account_age_days < 90:
        score += 0.05

    # Signals that REDUCE fraud probability
    if not inp.same_device_as_prior_orders:
        score -= 0.10
    if not inp.same_ip_as_prior_orders:
        score -= 0.10
    if not inp.shipping_matches_billing:
        score -= 0.05
    if inp.has_buyer_communication and not inp.buyer_acknowledged_receipt:
        score -= 0.05  # Legitimate buyers contact support first

    return min(0.97, max(0.02, round(score, 2)))


def _risk_level(score: float) -> FraudRiskLevel:
    if score >= 0.75:
        return FraudRiskLevel.VERY_HIGH
    if score >= 0.55:
        return FraudRiskLevel.HIGH
    if score >= 0.35:
        return FraudRiskLevel.MODERATE
    if score >= 0.15:
        return FraudRiskLevel.LOW
    return FraudRiskLevel.VERY_LOW


def _recommended_action(risk: FraudRiskLevel) -> RecommendedAction:
    return {
        FraudRiskLevel.VERY_HIGH:  RecommendedAction.FIGHT_AGGRESSIVELY,
        FraudRiskLevel.HIGH:       RecommendedAction.FIGHT,
        FraudRiskLevel.MODERATE:   RecommendedAction.GATHER_EVIDENCE,
        FraudRiskLevel.LOW:        RecommendedAction.EVALUATE,
        FraudRiskLevel.VERY_LOW:   RecommendedAction.CONSIDER_SETTLE,
    }[risk]


@dataclass
class FriendlyFraudReport:
    """Friendly fraud assessment for one dispute."""
    dispute_id: str
    fraud_score: float
    risk_level: FraudRiskLevel
    recommended_action: RecommendedAction

    top_fraud_signals: list[str]
    missing_evidence_to_prove_fraud: list[str]

    adjusted_win_probability: float  # Base win probability adjusted for fraud signals
    summary: str


def score_friendly_fraud(inp: FriendlyFraudInput) -> FriendlyFraudReport:
    """Assess the probability that a dispute is friendly fraud."""
    score = _compute_fraud_score(inp)
    risk = _risk_level(score)
    action = _recommended_action(risk)

    # Build top signals list
    signals: list[str] = []
    if inp.has_delivery_confirmation:
        signals.append("Delivery confirmed by carrier")
    if inp.has_signature_confirmation:
        signals.append("Signature confirmation obtained")
    if inp.buyer_acknowledged_receipt:
        signals.append("Buyer previously acknowledged receipt")
    if inp.item_accessed_after_purchase:
        signals.append("Digital item was accessed after purchase")
    if inp.is_repeat_dispute_same_merchant:
        signals.append("Buyer has prior dispute with this merchant")
    if inp.prior_disputes_won_by_buyer >= 2:
        signals.append(f"Buyer has won {inp.prior_disputes_won_by_buyer} prior chargebacks")
    if inp.total_prior_orders > 0:
        rate = inp.total_prior_disputes / inp.total_prior_orders
        if rate > 0.10:
            signals.append(f"High historical dispute rate: {rate:.0%}")
    if inp.days_between_delivery_and_dispute is not None and inp.days_between_delivery_and_dispute <= 3:
        signals.append(f"Dispute filed only {inp.days_between_delivery_and_dispute}d after delivery")

    # Missing evidence that would increase confidence
    missing: list[str] = []
    if not inp.has_delivery_confirmation:
        missing.append("delivery_confirmation")
    if not inp.has_usage_logs and (inp.is_digital_goods or inp.is_downloadable):
        missing.append("digital_product_access_logs")
    if not inp.buyer_acknowledged_receipt:
        missing.append("buyer_receipt_acknowledgment_email")

    # Adjusted win probability: fraud evidence raises win probability
    base_win = 0.55  # Baseline INR/SNAD win rate
    if risk == FraudRiskLevel.VERY_HIGH:
        adj_win = min(0.88, base_win + 0.33)
    elif risk == FraudRiskLevel.HIGH:
        adj_win = min(0.82, base_win + 0.22)
    elif risk == FraudRiskLevel.MODERATE:
        adj_win = min(0.70, base_win + 0.10)
    else:
        adj_win = base_win

    summary = (
        f"FraudScore {inp.dispute_id}: "
        f"score={score:.2f} | risk={risk.value} | "
        f"action={action.value} | adj_win={adj_win:.0%}"
    )

    return FriendlyFraudReport(
        dispute_id=inp.dispute_id,
        fraud_score=score,
        risk_level=risk,
        recommended_action=action,
        top_fraud_signals=signals,
        missing_evidence_to_prove_fraud=missing,
        adjusted_win_probability=round(adj_win, 2),
        summary=summary,
    )
