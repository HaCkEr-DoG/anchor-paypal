"""
Subscription Dispute Classifier — classify subscription chargebacks and
determine the appropriate defense strategy.

Subscription disputes are uniquely complex because:
  1. Buyer may claim they cancelled but were still charged
  2. Buyer may claim the product/service stopped working
  3. Free trial auto-renewals create a high chargeback risk
  4. Recurring billing requires explicit consent records

PayPal Seller Protection does NOT cover subscription disputes unless
the merchant can prove:
  - Clear disclosure of recurring terms at sign-up
  - Cancellation confirmation was sent
  - Buyer's cancellation request was honored

Dispute defense requirements:
  CANCELLED_BEFORE_CHARGE  → show cancellation was pending, offer refund
  NEVER_CANCELLED          → show billing agreement + usage logs
  SERVICE_NOT_DELIVERED    → show service uptime/delivery logs
  FREE_TRIAL_RENEWAL       → show trial terms disclosure + reminder emails
  WRONG_AMOUNT             → show billing agreement with stated amount

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SubscriptionDisputeType(str, Enum):
    CANCELLED_BEFORE_CHARGE  = "CANCELLED_BEFORE_CHARGE"
    NEVER_CANCELLED          = "NEVER_CANCELLED"
    SERVICE_NOT_DELIVERED    = "SERVICE_NOT_DELIVERED"
    FREE_TRIAL_RENEWAL       = "FREE_TRIAL_RENEWAL"
    WRONG_AMOUNT             = "WRONG_AMOUNT"
    UNKNOWN                  = "UNKNOWN"


class SubscriptionDefenseStrength(str, Enum):
    STRONG    = "STRONG"    # Clear evidence, good chance of winning
    ADEQUATE  = "ADEQUATE"  # Some evidence, fight is reasonable
    WEAK      = "WEAK"      # Limited evidence, settlement preferred
    CONCEDE   = "CONCEDE"   # No viable defense


@dataclass
class SubscriptionDisputeInput:
    """Input for subscription dispute classification."""
    dispute_id: str
    transaction_amount: float

    # Buyer's claim signals
    buyer_requested_cancellation: bool = False
    buyer_cancel_date: Optional[int] = None   # Days before charge
    charge_occurred_after_cancel_request: bool = False

    # Merchant evidence
    has_billing_agreement_record: bool = False
    has_cancellation_confirmation: bool = False
    cancellation_was_honored: bool = False
    has_service_usage_logs: bool = False
    has_trial_terms_disclosure: bool = False
    had_trial_reminder_email: bool = False
    charged_amount_matches_agreement: bool = True

    # Subscription state
    is_free_trial: bool = False
    trial_days: int = 0
    is_active_at_time_of_dispute: bool = True
    service_uptime_pct: float = 99.0    # 0–100

    # Additional
    has_prior_refunds_to_buyer: bool = False
    is_serial_subscription_disputer: bool = False


def _classify_dispute_type(inp: SubscriptionDisputeInput) -> SubscriptionDisputeType:
    if not inp.charged_amount_matches_agreement:
        return SubscriptionDisputeType.WRONG_AMOUNT
    if inp.is_free_trial and not inp.has_trial_terms_disclosure:
        return SubscriptionDisputeType.FREE_TRIAL_RENEWAL
    if inp.charge_occurred_after_cancel_request or (
            inp.buyer_requested_cancellation and
            inp.buyer_cancel_date is not None and
            inp.buyer_cancel_date > 0):
        return SubscriptionDisputeType.CANCELLED_BEFORE_CHARGE
    if inp.service_uptime_pct < 90.0:
        return SubscriptionDisputeType.SERVICE_NOT_DELIVERED
    if inp.buyer_requested_cancellation and not inp.cancellation_was_honored:
        return SubscriptionDisputeType.NEVER_CANCELLED
    return SubscriptionDisputeType.UNKNOWN


def _assess_defense_strength(
        dt: SubscriptionDisputeType,
        inp: SubscriptionDisputeInput) -> SubscriptionDefenseStrength:

    if dt == SubscriptionDisputeType.CANCELLED_BEFORE_CHARGE:
        if inp.cancellation_was_honored and inp.has_cancellation_confirmation:
            return SubscriptionDefenseStrength.STRONG
        if inp.charge_occurred_after_cancel_request:
            return SubscriptionDefenseStrength.CONCEDE  # We clearly messed up
        return SubscriptionDefenseStrength.WEAK

    if dt == SubscriptionDisputeType.NEVER_CANCELLED:
        if inp.has_billing_agreement_record and inp.has_service_usage_logs:
            return SubscriptionDefenseStrength.STRONG
        if inp.has_billing_agreement_record:
            return SubscriptionDefenseStrength.ADEQUATE
        return SubscriptionDefenseStrength.WEAK

    if dt == SubscriptionDisputeType.SERVICE_NOT_DELIVERED:
        if inp.service_uptime_pct >= 99.0:
            return SubscriptionDefenseStrength.STRONG
        if inp.service_uptime_pct >= 95.0:
            return SubscriptionDefenseStrength.ADEQUATE
        return SubscriptionDefenseStrength.CONCEDE

    if dt == SubscriptionDisputeType.FREE_TRIAL_RENEWAL:
        if inp.has_trial_terms_disclosure and inp.had_trial_reminder_email:
            return SubscriptionDefenseStrength.STRONG
        if inp.has_trial_terms_disclosure:
            return SubscriptionDefenseStrength.ADEQUATE
        return SubscriptionDefenseStrength.CONCEDE  # No disclosure = concede

    if dt == SubscriptionDisputeType.WRONG_AMOUNT:
        return SubscriptionDefenseStrength.CONCEDE  # Always refund wrong amount

    return SubscriptionDefenseStrength.WEAK


def _win_probability(dt: SubscriptionDisputeType,
                     strength: SubscriptionDefenseStrength,
                     inp: SubscriptionDisputeInput) -> float:
    base = {
        SubscriptionDefenseStrength.STRONG:   0.70,
        SubscriptionDefenseStrength.ADEQUATE: 0.50,
        SubscriptionDefenseStrength.WEAK:     0.25,
        SubscriptionDefenseStrength.CONCEDE:  0.05,
    }[strength]

    if inp.is_serial_subscription_disputer:
        base = min(base + 0.10, 0.90)  # Fraud pattern — higher chance of winning
    if inp.has_prior_refunds_to_buyer and dt == SubscriptionDisputeType.CANCELLED_BEFORE_CHARGE:
        base = max(base - 0.10, 0.02)  # Prior refunds for same issue = pattern

    return round(base, 2)


def _required_evidence(dt: SubscriptionDisputeType) -> list[str]:
    base = ["billing_agreement_record"]
    if dt == SubscriptionDisputeType.CANCELLED_BEFORE_CHARGE:
        return base + [
            "cancellation_confirmation_email",
            "cancellation_processing_logs",
            "timeline_showing_cancel_before_charge",
        ]
    if dt == SubscriptionDisputeType.NEVER_CANCELLED:
        return base + [
            "service_usage_logs",
            "login_access_records",
            "terms_accepted_at_signup",
        ]
    if dt == SubscriptionDisputeType.SERVICE_NOT_DELIVERED:
        return base + [
            "service_uptime_logs",
            "delivery_confirmation_records",
            "support_ticket_responses",
        ]
    if dt == SubscriptionDisputeType.FREE_TRIAL_RENEWAL:
        return [
            "trial_terms_disclosure_at_signup",
            "trial_reminder_email_sent",
            "explicit_upgrade_consent",
            billing_agreement_record := "billing_agreement_record",
        ]
    if dt == SubscriptionDisputeType.WRONG_AMOUNT:
        return [
            "original_billing_agreement_with_amount",
            "charge_amount_history",
        ]
    return base


@dataclass
class SubscriptionDisputeReport:
    """Classification and defense plan for a subscription dispute."""
    dispute_id: str
    dispute_type: SubscriptionDisputeType
    defense_strength: SubscriptionDefenseStrength
    win_probability: float
    recommended_action: str

    required_evidence: list[str]
    missing_evidence: list[str]

    sp_coverage: bool    # Subscription disputes rarely get SP
    fee_risk: float      # Estimated cost if lost

    key_defense_points: list[str]
    summary: str


def classify_subscription_dispute(
        inp: SubscriptionDisputeInput) -> SubscriptionDisputeReport:
    """
    Classify a subscription dispute and generate a defense plan.
    """
    dt = _classify_dispute_type(inp)
    strength = _assess_defense_strength(dt, inp)
    p_win = _win_probability(dt, strength, inp)

    if strength == SubscriptionDefenseStrength.CONCEDE:
        recommended_action = f"REFUND: Issue full refund of ${inp.transaction_amount:.2f}"
    elif strength == SubscriptionDefenseStrength.WEAK:
        recommended_action = "SETTLE: Offer partial refund or service credit"
    elif strength == SubscriptionDefenseStrength.ADEQUATE:
        recommended_action = "FIGHT: Submit available evidence"
    else:
        recommended_action = "FIGHT: Strong evidence available — contest dispute"

    required = _required_evidence(dt)
    missing: list[str] = []
    if not inp.has_billing_agreement_record and "billing_agreement_record" in required:
        missing.append("billing_agreement_record")
    if not inp.has_cancellation_confirmation and "cancellation_confirmation_email" in required:
        missing.append("cancellation_confirmation_email")
    if not inp.has_service_usage_logs and "service_usage_logs" in required:
        missing.append("service_usage_logs")
    if not inp.has_trial_terms_disclosure and "trial_terms_disclosure_at_signup" in required:
        missing.append("trial_terms_disclosure_at_signup")
    if not inp.had_trial_reminder_email and "trial_reminder_email_sent" in required:
        missing.append("trial_reminder_email_sent")

    # Build key defense points
    points: list[str] = []
    if dt == SubscriptionDisputeType.CANCELLED_BEFORE_CHARGE:
        if inp.cancellation_was_honored:
            points.append("Cancellation was processed — show cancellation confirmation email")
        else:
            points.append("Show cancellation request was received and honored promptly")
    elif dt == SubscriptionDisputeType.NEVER_CANCELLED:
        points.append("Show billing agreement with buyer's consent to recurring charges")
        if inp.has_service_usage_logs:
            points.append("Include service usage logs proving buyer accessed the service")
    elif dt == SubscriptionDisputeType.SERVICE_NOT_DELIVERED:
        points.append(f"Show service uptime logs ({inp.service_uptime_pct:.1f}% uptime)")
    elif dt == SubscriptionDisputeType.FREE_TRIAL_RENEWAL:
        points.append("Show trial terms disclosure was shown at signup")
        points.append("Include trial expiration reminder email to buyer")

    if inp.is_serial_subscription_disputer:
        points.append("Pattern evidence: buyer has prior subscription disputes — potential abuse")

    summary = (
        f"SubDispute {inp.dispute_id}: "
        f"type={dt.value} | strength={strength.value} | "
        f"p_win={p_win:.0%} | action={recommended_action[:20]}"
    )

    return SubscriptionDisputeReport(
        dispute_id=inp.dispute_id,
        dispute_type=dt,
        defense_strength=strength,
        win_probability=p_win,
        recommended_action=recommended_action,
        required_evidence=required,
        missing_evidence=missing,
        sp_coverage=False,  # SP doesn't cover subscription disputes
        fee_risk=round(inp.transaction_amount * (1 - p_win), 2),
        key_defense_points=points,
        summary=summary,
    )
