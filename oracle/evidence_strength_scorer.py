"""
Evidence Strength Scorer — rate dispute evidence quality before submission.

Having Seller Protection eligibility is necessary but not sufficient. The
evidence package submitted to PayPal determines whether a winnable dispute
is actually won. This module scores each evidence category and returns:
  - overall_score: 0.0 – 1.0  (composite evidence strength)
  - strength_tier: STRONG / ADEQUATE / WEAK / INSUFFICIENT
  - missing_critical: list of missing must-have items
  - missing_beneficial: list of nice-to-have items that boost win rate
  - estimated_win_probability: adjusted P(win) given evidence quality
  - paypal_signal: used by PayPal seller success to identify coaching needs

Evidence categories scored (by reason code):
  INR  — tracking proof, delivery scan, carrier confirmation, timeline
  SNAD — product photos, description match, inspection records, comms
  UNAUTHORIZED — shipping address match, delivery proof, IP/device log
  SUBSCRIPTION — billing notice, terms acceptance, prior renewal history

PayPal benefit:
  - Merchants with weak evidence consume more dispute resolution bandwidth
    (back-and-forth requests, escalations, appeal cycles)
  - Early evidence scoring reduces average dispute resolution time
  - Surfaces which merchant segments need evidence coaching
  - Flags disputes likely to escalate so PayPal can pre-assign a reviewer
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StrengthTier(str, Enum):
    STRONG       = "STRONG"       # score ≥ 0.80 — high confidence
    ADEQUATE     = "ADEQUATE"     # score 0.55–0.79 — submit with improvements
    WEAK         = "WEAK"         # score 0.30–0.54 — gather more before submitting
    INSUFFICIENT = "INSUFFICIENT" # score < 0.30 — do not submit yet


class EvidenceItemStatus(str, Enum):
    PROVIDED    = "PROVIDED"
    MISSING     = "MISSING"
    PARTIAL     = "PARTIAL"


@dataclass
class EvidencePackage:
    """What the merchant has assembled so far."""
    dispute_id: str
    reason_code: str            # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION
    transaction_amount: float

    # Shipping & delivery
    has_tracking_number: bool = False
    tracking_number: Optional[str] = None
    has_delivery_scan: bool = False        # carrier scan at destination
    has_signature_proof: bool = False
    has_carrier_confirmation_letter: bool = False

    # Product / order
    has_order_confirmation: bool = False   # order receipt / confirmation email
    has_product_photos: bool = False       # photos of item shipped
    has_item_description_match: bool = False  # listing ≠ complaint basis
    has_inspection_record: bool = False    # 3rd-party condition report

    # Communication
    has_buyer_communication: bool = False  # emails, chats with buyer
    has_prior_resolution_attempt: bool = False  # tried to resolve before escalation
    has_refund_policy_acceptance: bool = False   # buyer acknowledged refund policy

    # Subscription-specific
    has_billing_notification: bool = False     # renewal email sent
    has_terms_acceptance: bool = False         # signed ToS / checked box
    has_prior_renewal_history: bool = False    # buyer renewed before

    # Unauthorized-specific
    has_ip_address_match: bool = False         # buyer IP matches transaction
    has_device_fingerprint: bool = False
    has_address_match_proof: bool = False      # shipped to PayPal-confirmed address

    # Supporting
    has_fraud_prevention_evidence: bool = False  # 3DS, CVV, AVS pass
    has_policy_documentation: bool = False


@dataclass
class EvidenceItem:
    name: str
    status: EvidenceItemStatus
    weight: float               # contribution to score (0–1 relative)
    critical: bool              # True = must-have for this reason code
    description: str


@dataclass
class EvidenceScorerReport:
    dispute_id: str
    reason_code: str
    overall_score: float          # 0.0 – 1.0
    strength_tier: StrengthTier
    items: list[EvidenceItem]

    missing_critical: list[str]
    missing_beneficial: list[str]

    estimated_win_probability: float  # adjusted P(win)
    submit_ready: bool

    paypal_signal: str
    recommendation: str


# ─── Scoring tables ───────────────────────────────────────────────────────────

def _score_inr(pkg: EvidencePackage) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            "tracking_number",
            EvidenceItemStatus.PROVIDED if pkg.has_tracking_number else EvidenceItemStatus.MISSING,
            weight=0.25, critical=True,
            description="Shipment tracking number",
        ),
        EvidenceItem(
            "delivery_scan",
            EvidenceItemStatus.PROVIDED if pkg.has_delivery_scan else EvidenceItemStatus.MISSING,
            weight=0.30, critical=True,
            description="Carrier delivery scan showing package reached destination",
        ),
        EvidenceItem(
            "order_confirmation",
            EvidenceItemStatus.PROVIDED if pkg.has_order_confirmation else EvidenceItemStatus.MISSING,
            weight=0.15, critical=True,
            description="Order confirmation or shipment notification email",
        ),
        EvidenceItem(
            "buyer_communication",
            EvidenceItemStatus.PROVIDED if pkg.has_buyer_communication else EvidenceItemStatus.MISSING,
            weight=0.15, critical=False,
            description="Communications with buyer about the order",
        ),
        EvidenceItem(
            "carrier_confirmation_letter",
            EvidenceItemStatus.PROVIDED if pkg.has_carrier_confirmation_letter else EvidenceItemStatus.MISSING,
            weight=0.10, critical=False,
            description="Official carrier confirmation letter (boosts credibility)",
        ),
        EvidenceItem(
            "signature_proof",
            EvidenceItemStatus.PROVIDED if pkg.has_signature_proof else EvidenceItemStatus.MISSING,
            weight=0.05, critical=pkg.transaction_amount >= 750.0,
            description=(
                "Signature confirmation (REQUIRED for transactions ≥ $750)"
                if pkg.transaction_amount >= 750.0
                else "Signature confirmation (optional but strongly helpful)"
            ),
        ),
    ]


def _score_snad(pkg: EvidencePackage) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            "product_photos",
            EvidenceItemStatus.PROVIDED if pkg.has_product_photos else EvidenceItemStatus.MISSING,
            weight=0.30, critical=True,
            description="Photos of item as shipped (must match listing)",
        ),
        EvidenceItem(
            "item_description_match",
            EvidenceItemStatus.PROVIDED if pkg.has_item_description_match else EvidenceItemStatus.MISSING,
            weight=0.25, critical=True,
            description="Evidence that item matches listing description",
        ),
        EvidenceItem(
            "order_confirmation",
            EvidenceItemStatus.PROVIDED if pkg.has_order_confirmation else EvidenceItemStatus.MISSING,
            weight=0.15, critical=True,
            description="Original order and listing documentation",
        ),
        EvidenceItem(
            "buyer_communication",
            EvidenceItemStatus.PROVIDED if pkg.has_buyer_communication else EvidenceItemStatus.MISSING,
            weight=0.15, critical=False,
            description="Communications showing buyer's claim contradicts evidence",
        ),
        EvidenceItem(
            "inspection_record",
            EvidenceItemStatus.PROVIDED if pkg.has_inspection_record else EvidenceItemStatus.MISSING,
            weight=0.10, critical=False,
            description="3rd-party inspection or condition report",
        ),
        EvidenceItem(
            "prior_resolution_attempt",
            EvidenceItemStatus.PROVIDED if pkg.has_prior_resolution_attempt else EvidenceItemStatus.MISSING,
            weight=0.05, critical=False,
            description="Evidence merchant tried to resolve before escalation",
        ),
    ]


def _score_unauthorized(pkg: EvidencePackage) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            "address_match_proof",
            EvidenceItemStatus.PROVIDED if pkg.has_address_match_proof else EvidenceItemStatus.MISSING,
            weight=0.30, critical=True,
            description="Proof item shipped to PayPal-confirmed address",
        ),
        EvidenceItem(
            "delivery_scan",
            EvidenceItemStatus.PROVIDED if pkg.has_delivery_scan else EvidenceItemStatus.MISSING,
            weight=0.25, critical=True,
            description="Carrier delivery confirmation to confirmed address",
        ),
        EvidenceItem(
            "fraud_prevention_evidence",
            EvidenceItemStatus.PROVIDED if pkg.has_fraud_prevention_evidence else EvidenceItemStatus.MISSING,
            weight=0.20, critical=False,
            description="3D Secure, CVV/AVS pass, or other fraud-prevention checks",
        ),
        EvidenceItem(
            "ip_address_match",
            EvidenceItemStatus.PROVIDED if pkg.has_ip_address_match else EvidenceItemStatus.MISSING,
            weight=0.15, critical=False,
            description="IP address at checkout matches buyer's known location",
        ),
        EvidenceItem(
            "tracking_number",
            EvidenceItemStatus.PROVIDED if pkg.has_tracking_number else EvidenceItemStatus.MISSING,
            weight=0.10, critical=False,
            description="Shipment tracking number",
        ),
    ]


def _score_subscription(pkg: EvidencePackage) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            "billing_notification",
            EvidenceItemStatus.PROVIDED if pkg.has_billing_notification else EvidenceItemStatus.MISSING,
            weight=0.30, critical=True,
            description="Pre-billing renewal notification sent to buyer",
        ),
        EvidenceItem(
            "terms_acceptance",
            EvidenceItemStatus.PROVIDED if pkg.has_terms_acceptance else EvidenceItemStatus.MISSING,
            weight=0.30, critical=True,
            description="Buyer's acceptance of subscription terms (checkbox, e-sign)",
        ),
        EvidenceItem(
            "prior_renewal_history",
            EvidenceItemStatus.PROVIDED if pkg.has_prior_renewal_history else EvidenceItemStatus.MISSING,
            weight=0.20, critical=False,
            description="History of prior successful renewals by this buyer",
        ),
        EvidenceItem(
            "refund_policy_acceptance",
            EvidenceItemStatus.PROVIDED if pkg.has_refund_policy_acceptance else EvidenceItemStatus.MISSING,
            weight=0.15, critical=False,
            description="Buyer acknowledged no-refund or limited-refund policy",
        ),
        EvidenceItem(
            "buyer_communication",
            EvidenceItemStatus.PROVIDED if pkg.has_buyer_communication else EvidenceItemStatus.MISSING,
            weight=0.05, critical=False,
            description="Communications with buyer about subscription terms",
        ),
    ]


_SCORERS = {
    "INR":           _score_inr,
    "SNAD":          _score_snad,
    "UNAUTHORIZED":  _score_unauthorized,
    "SUBSCRIPTION":  _score_subscription,
}


# ─── Main function ────────────────────────────────────────────────────────────

def score_evidence(pkg: EvidencePackage) -> EvidenceScorerReport:
    """
    Score the evidence package for a dispute and return a tier + action list.
    """
    reason = pkg.reason_code.upper()
    scorer = _SCORERS.get(reason, _score_inr)
    items = scorer(pkg)

    # Compute weighted score (only provided items contribute)
    total_weight = sum(i.weight for i in items)
    earned_weight = sum(i.weight for i in items if i.status == EvidenceItemStatus.PROVIDED)
    score = round(earned_weight / total_weight, 4) if total_weight > 0 else 0.0

    # Tier
    if score >= 0.80:
        tier = StrengthTier.STRONG
    elif score >= 0.55:
        tier = StrengthTier.ADEQUATE
    elif score >= 0.30:
        tier = StrengthTier.WEAK
    else:
        tier = StrengthTier.INSUFFICIENT

    # Critical items
    missing_critical = [
        i.description for i in items
        if i.critical and i.status != EvidenceItemStatus.PROVIDED
    ]
    missing_beneficial = [
        i.description for i in items
        if not i.critical and i.status != EvidenceItemStatus.PROVIDED
    ]

    # Estimated win probability (evidence quality multiplier on base rate)
    base_win_rate = {
        "INR": 0.72, "UNAUTHORIZED": 0.68, "SNAD": 0.45, "SUBSCRIPTION": 0.55,
    }.get(reason, 0.55)

    # Evidence quality multiplier: strong → 1.0×, insufficient → 0.25×
    multiplier = {
        StrengthTier.STRONG:       1.00,
        StrengthTier.ADEQUATE:     0.80,
        StrengthTier.WEAK:         0.50,
        StrengthTier.INSUFFICIENT: 0.25,
    }[tier]
    win_prob = round(min(0.95, base_win_rate * multiplier), 4)

    submit_ready = tier in (StrengthTier.STRONG, StrengthTier.ADEQUATE) and not missing_critical

    # Recommendation
    if submit_ready:
        recommendation = (
            f"Evidence is {tier.value}. Submit response now — "
            f"estimated win probability: {win_prob:.0%}."
            + (f" Consider adding: {missing_beneficial[0]}." if missing_beneficial else "")
        )
    elif missing_critical:
        recommendation = (
            f"DO NOT SUBMIT YET. Missing {len(missing_critical)} critical item(s): "
            + "; ".join(missing_critical[:2])
            + f". Gather these first. Win probability without them: {win_prob:.0%}."
        )
    else:
        recommendation = (
            f"Evidence is {tier.value}. Add more supporting items before submitting. "
            f"Current win probability: {win_prob:.0%}."
        )

    # PayPal signal
    if tier == StrengthTier.INSUFFICIENT:
        paypal_signal = (
            f"Dispute {pkg.dispute_id} ({reason}): evidence INSUFFICIENT "
            f"(score={score:.2f}). Merchant likely to lose. "
            f"Recommend proactive refund to avoid escalation. "
            f"Missing critical: {len(missing_critical)} items."
        )
    elif tier == StrengthTier.WEAK:
        paypal_signal = (
            f"Dispute {pkg.dispute_id} ({reason}): WEAK evidence (score={score:.2f}). "
            f"High escalation risk. Coach merchant on: "
            + (missing_critical[0][:60] if missing_critical else missing_beneficial[0][:60] if missing_beneficial else "evidence documentation")
            + "."
        )
    else:
        paypal_signal = (
            f"Dispute {pkg.dispute_id} ({reason}): {tier.value} evidence "
            f"(score={score:.2f}, win_prob={win_prob:.0%}). "
            f"Standard review expected."
        )

    return EvidenceScorerReport(
        dispute_id=pkg.dispute_id,
        reason_code=pkg.reason_code,
        overall_score=score,
        strength_tier=tier,
        items=items,
        missing_critical=missing_critical,
        missing_beneficial=missing_beneficial,
        estimated_win_probability=win_prob,
        submit_ready=submit_ready,
        paypal_signal=paypal_signal,
        recommendation=recommendation,
    )
