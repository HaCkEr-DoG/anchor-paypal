"""
Seller Protection Checker — determine PayPal Seller Protection eligibility.

PayPal Seller Protection (SP) shields merchants from liability for certain
dispute types when the merchant meets specific requirements. SP is NOT
automatic — the merchant must actively claim it and meet all criteria.

SP Coverage:
  - Item Not Received (INR): Covered if shipped to confirmed address with
    proof of shipment/delivery + signature for ≥$750
  - Unauthorized Transaction: Covered if order placed with verified PayPal
    account to confirmed address
  - SNAD: NOT covered by Seller Protection

SP Requirements (all must be met):
  1. Item shipped to confirmed shipping address on the transaction
  2. Proof of shipment or delivery (tracking number)
  3. For tangible goods:
     - Signature confirmation required for orders ≥ $750
  4. Single physical item shipped (not digital goods for UNAUTHORIZED claims)
  5. Transaction date within 180 days of claim
  6. Merchant account in good standing
  7. No prior chargebacks for same transaction

SP Gaps (reasons SP might not apply):
  - Shipped to unconfirmed address
  - No tracking / no proof of shipment
  - Signature missing for ≥$750 orders
  - Digital goods dispute (UNAUTHORIZED)
  - Item is in excluded categories:
    * Vehicles, real estate, gold, platinum, precious metals
    * Custom items (made-to-order)
    * Industrial machinery / items
    * Payments for professional services
    * License key / in-game currency

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SPEligibility(str, Enum):
    ELIGIBLE         = "ELIGIBLE"      # Fully covered
    LIKELY_ELIGIBLE  = "LIKELY_ELIGIBLE"  # Meets most criteria, minor gaps
    INELIGIBLE       = "INELIGIBLE"    # Fails hard requirements
    NOT_APPLICABLE   = "NOT_APPLICABLE"  # Claim type not covered (SNAD)
    PARTIAL          = "PARTIAL"       # Some conditions unmet; partial protection


class SPGapSeverity(str, Enum):
    HARD_FAIL = "HARD_FAIL"      # Disqualifies SP entirely
    SOFT_GAP  = "SOFT_GAP"       # Weakens case but doesn't disqualify
    WARNING   = "WARNING"        # Should be fixed for future orders


class ExcludedCategory(str, Enum):
    VEHICLE           = "VEHICLE"
    REAL_ESTATE       = "REAL_ESTATE"
    PRECIOUS_METALS   = "PRECIOUS_METALS"
    CUSTOM_MADE       = "CUSTOM_MADE"
    INDUSTRIAL        = "INDUSTRIAL"
    SERVICES          = "SERVICES"
    DIGITAL_LICENSE   = "DIGITAL_LICENSE"
    IN_GAME_CURRENCY  = "IN_GAME_CURRENCY"
    NONE              = "NONE"


@dataclass
class SPInput:
    """Input for Seller Protection eligibility check."""
    dispute_id: str
    claim_type: str               # INR / UNAUTHORIZED / SNAD

    # Order facts
    order_amount: float
    is_physical_item: bool = True
    item_category: ExcludedCategory = ExcludedCategory.NONE

    # Shipping / delivery
    shipped_to_confirmed_address: bool = False
    has_tracking_number: bool = False
    tracking_shows_delivered: bool = False
    has_signature_confirmation: bool = False

    # Account standing
    merchant_account_in_good_standing: bool = True
    transaction_within_180_days: bool = True

    # Prior disputes
    prior_chargeback_same_transaction: bool = False

    # For UNAUTHORIZED
    buyer_has_verified_paypal_account: bool = True


@dataclass
class SPGap:
    """One gap in Seller Protection eligibility."""
    requirement: str
    severity: SPGapSeverity
    description: str
    fix: str              # What merchant can do (retroactive or for future)


@dataclass
class SPReport:
    """
    Seller Protection eligibility assessment.
    """
    dispute_id: str
    claim_type: str
    eligibility: SPEligibility

    # Score (0-100)
    sp_score: float

    # Gaps
    gaps: list[SPGap]
    hard_fail_count: int
    soft_gap_count: int

    # Specific checks
    signature_required: bool          # Is signature required for this order?
    signature_missing: bool
    address_confirmed: bool
    tracking_present: bool

    # Guidance
    sp_text_to_cite: str              # Text to include in dispute response
    action_items: list[str]
    summary: str


def _signature_required(amount: float) -> bool:
    return amount >= 750.0


def _check_gaps(inp: SPInput) -> list[SPGap]:
    gaps: list[SPGap] = []

    # Hard fail: SNAD
    if inp.claim_type == "SNAD":
        gaps.append(SPGap(
            requirement="Claim type coverage",
            severity=SPGapSeverity.HARD_FAIL,
            description="Seller Protection does NOT cover Significantly Not As Described claims",
            fix="Cannot claim SP for SNAD — focus on product accuracy evidence instead",
        ))
        return gaps  # No point checking further

    # Hard fail: excluded category
    if inp.item_category != ExcludedCategory.NONE:
        gaps.append(SPGap(
            requirement="Item category",
            severity=SPGapSeverity.HARD_FAIL,
            description=f"Item category {inp.item_category.value} is excluded from Seller Protection",
            fix="Consult PayPal's excluded categories list for future transactions",
        ))

    # Hard fail: prior chargeback same transaction
    if inp.prior_chargeback_same_transaction:
        gaps.append(SPGap(
            requirement="No prior chargeback",
            severity=SPGapSeverity.HARD_FAIL,
            description="Prior chargeback already filed for this transaction",
            fix="Cannot claim SP if a chargeback already exists for this transaction",
        ))

    # Hard fail: transaction age
    if not inp.transaction_within_180_days:
        gaps.append(SPGap(
            requirement="Transaction within 180 days",
            severity=SPGapSeverity.HARD_FAIL,
            description="Transaction is older than 180 days — SP window expired",
            fix="Expedite dispute responses; SP expires 180 days from transaction",
        ))

    # Hard fail: digital goods for UNAUTHORIZED
    if not inp.is_physical_item and inp.claim_type == "UNAUTHORIZED":
        gaps.append(SPGap(
            requirement="Physical item",
            severity=SPGapSeverity.HARD_FAIL,
            description="Seller Protection for UNAUTHORIZED does not cover digital goods",
            fix="Collect device/IP evidence for digital goods disputes — SP does not apply",
        ))

    # Soft gap: unconfirmed address
    if not inp.shipped_to_confirmed_address:
        gaps.append(SPGap(
            requirement="Confirmed shipping address",
            severity=SPGapSeverity.SOFT_GAP,
            description="Item not shipped to PayPal-confirmed address on file",
            fix="Always ship to the confirmed address shown on the PayPal transaction",
        ))

    # Soft gap: no tracking
    if not inp.has_tracking_number:
        gaps.append(SPGap(
            requirement="Proof of shipment",
            severity=SPGapSeverity.SOFT_GAP,
            description="No tracking number — cannot prove shipment",
            fix="Always use a trackable shipping method and retain the tracking number",
        ))

    # Signature required but missing
    sig_required = _signature_required(inp.order_amount)
    if sig_required and not inp.has_signature_confirmation:
        gaps.append(SPGap(
            requirement="Signature confirmation (≥$750)",
            severity=SPGapSeverity.SOFT_GAP,
            description=f"Order is ${inp.order_amount:.2f} — signature confirmation required for Seller Protection",
            fix="Retroactively request signature confirmation from carrier; require for all future ≥$750 orders",
        ))

    return gaps


def check_seller_protection(inp: SPInput) -> SPReport:
    """
    Assess Seller Protection eligibility for a dispute.
    """
    gaps = _check_gaps(inp)
    hard_fails = [g for g in gaps if g.severity == SPGapSeverity.HARD_FAIL]
    soft_gaps = [g for g in gaps if g.severity == SPGapSeverity.SOFT_GAP]

    # Eligibility
    if inp.claim_type == "SNAD":
        eligibility = SPEligibility.NOT_APPLICABLE
    elif hard_fails:
        eligibility = SPEligibility.INELIGIBLE
    elif soft_gaps:
        # Partial if some soft gaps, likely eligible if minor ones
        critical_soft = [g for g in soft_gaps if "tracking" in g.requirement.lower() or "signature" in g.requirement.lower()]
        eligibility = SPEligibility.PARTIAL if critical_soft else SPEligibility.LIKELY_ELIGIBLE
    else:
        eligibility = SPEligibility.ELIGIBLE

    # Score
    score = 100.0
    score -= len(hard_fails) * 40
    score -= len(soft_gaps) * 15
    score = max(0.0, min(100.0, score))

    sig_required = _signature_required(inp.order_amount)
    sig_missing = sig_required and not inp.has_signature_confirmation

    # SP text
    if eligibility == SPEligibility.ELIGIBLE:
        sp_text = (
            f"This transaction qualifies for PayPal Seller Protection as an "
            f"{'Item Not Received' if inp.claim_type == 'INR' else 'Unauthorized Transaction'} claim. "
            f"The item was shipped to the confirmed address with tracking "
            f"{'and signature confirmation ' if inp.has_signature_confirmation else ''}"
            f"as required by PayPal's Seller Protection policy."
        )
    elif eligibility == SPEligibility.LIKELY_ELIGIBLE:
        sp_text = (
            f"This transaction is likely covered by PayPal Seller Protection. "
            f"Shipment was made to the confirmed address with tracking number provided."
        )
    elif eligibility == SPEligibility.NOT_APPLICABLE:
        sp_text = "Seller Protection does not apply to Significantly Not As Described claims."
    else:
        sp_text = ""

    # Action items
    actions: list[str] = []
    if sig_missing and eligibility in (SPEligibility.PARTIAL, SPEligibility.LIKELY_ELIGIBLE):
        actions.append(f"Contact carrier to obtain retrospective delivery confirmation for ${inp.order_amount:.2f} order")
    if not inp.shipped_to_confirmed_address:
        actions.append("For future orders: always ship to the PayPal-confirmed address")
    if not inp.has_tracking_number:
        actions.append("For future orders: always use a trackable carrier and save the tracking number")
    if eligibility == SPEligibility.ELIGIBLE:
        actions.append("Include SP eligibility text in your dispute response to PayPal")

    summary = (
        f"SP {inp.dispute_id}: {inp.claim_type} | eligibility={eligibility.value} | "
        f"score={score:.0f} | hard_fails={len(hard_fails)} | soft_gaps={len(soft_gaps)} | "
        f"sig_required={sig_required} sig_missing={sig_missing}"
    )

    return SPReport(
        dispute_id=inp.dispute_id,
        claim_type=inp.claim_type,
        eligibility=eligibility,
        sp_score=round(score, 1),
        gaps=gaps,
        hard_fail_count=len(hard_fails),
        soft_gap_count=len(soft_gaps),
        signature_required=sig_required,
        signature_missing=sig_missing,
        address_confirmed=inp.shipped_to_confirmed_address,
        tracking_present=inp.has_tracking_number,
        sp_text_to_cite=sp_text,
        action_items=actions,
        summary=summary,
    )
