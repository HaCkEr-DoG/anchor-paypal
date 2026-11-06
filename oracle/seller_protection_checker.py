"""
Seller Protection Checker — determine whether a transaction qualifies
for PayPal Seller Protection and what conditions apply.

PayPal Seller Protection rules:
  - Covers INR (Item Not Received) and Unauthorized transactions
  - Does NOT cover SNAD (Significantly Not As Described)
  - Requires item to be a physical, tangible good
  - Digital / intangible goods: not covered (with narrow exceptions)
  - Transaction must be:
    * Paid via PayPal checkout (not PayPal.Me / personal links)
    * Shipped to the address on the PayPal transaction details
    * Tracking number uploaded to PayPal before dispute filed
  - Signature required for ≥$750 (Significant Seller Protection threshold)
  - Chargeback eligible transactions must have buyer's confirmed address
  - Item category exclusions: real estate, vehicles, custom-made items,
    negotiable instruments, gold bullion, digital currency

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SPEligibility(str, Enum):
    ELIGIBLE            = "ELIGIBLE"
    ELIGIBLE_WITH_GAPS  = "ELIGIBLE_WITH_GAPS"  # Eligible but missing best practice
    INELIGIBLE          = "INELIGIBLE"
    PARTIAL             = "PARTIAL"             # Some conditions met, review needed


class SPRequirement(str, Enum):
    MET       = "MET"
    NOT_MET   = "NOT_MET"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class DisputeTypeForSP(str, Enum):
    INR           = "INR"
    UNAUTHORIZED  = "UNAUTHORIZED"
    SNAD          = "SNAD"           # Never covered
    SUBSCRIPTION  = "SUBSCRIPTION"   # Never covered
    OTHER         = "OTHER"


# Excluded item categories
_EXCLUDED_CATEGORIES = {
    "real_estate", "vehicle", "aircraft", "boat",
    "custom_made", "negotiable_instrument",
    "gold_bullion", "precious_metals",
    "digital_currency", "cryptocurrency",
    "gambling_winnings",
}

_SIGNATURE_THRESHOLD = 750.0


@dataclass
class SPRequirementCheck:
    requirement: str
    status: SPRequirement
    detail: str


@dataclass
class SellerProtectionInput:
    """Input for Seller Protection eligibility check."""
    dispute_id: str
    dispute_type: DisputeTypeForSP
    transaction_amount: float

    # Payment method
    paid_via_paypal_checkout: bool = True      # Not via PayPal.Me or personal link
    is_business_account: bool = True

    # Item type
    is_physical_tangible_good: bool = True
    item_category: str = "general"             # From _EXCLUDED_CATEGORIES

    # Shipping / fulfillment
    shipped_to_paypal_address: bool = True     # Must match transaction detail address
    tracking_uploaded_to_paypal: bool = False
    tracking_uploaded_before_dispute: bool = False
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False   # Required for ≥$750

    # Buyer address
    buyer_address_confirmed: bool = True       # PayPal confirmed address
    buyer_address_mismatch: bool = False       # Shipped to different address

    # Additional context
    is_digital_goods: bool = False
    has_ce30_qualification: bool = False       # CE3.0 — changes unauthorized handling


@dataclass
class SellerProtectionReport:
    """Seller Protection eligibility report."""
    dispute_id: str
    dispute_type: DisputeTypeForSP
    eligibility: SPEligibility

    requirement_checks: list[SPRequirementCheck]
    blocking_issues: list[str]
    warnings: list[str]
    recommendations: list[str]

    signature_required: bool
    signature_present: bool
    ce30_applicable: bool

    win_rate_impact: float   # Additive if covered, negative if not
    summary: str


def check_seller_protection(inp: SellerProtectionInput) -> SellerProtectionReport:
    """Check whether a transaction qualifies for PayPal Seller Protection."""
    checks: list[SPRequirementCheck] = []
    blocking: list[str] = []
    warnings: list[str] = []
    recs: list[str] = []

    # ── Dispute type gate ────────────────────────────────────────
    if inp.dispute_type == DisputeTypeForSP.SNAD:
        checks.append(SPRequirementCheck(
            "Dispute type",
            SPRequirement.NOT_MET,
            "SNAD disputes are explicitly excluded from Seller Protection",
        ))
        blocking.append("SNAD disputes are never covered by Seller Protection")
        return SellerProtectionReport(
            dispute_id=inp.dispute_id,
            dispute_type=inp.dispute_type,
            eligibility=SPEligibility.INELIGIBLE,
            requirement_checks=checks,
            blocking_issues=blocking,
            warnings=warnings,
            recommendations=["Accept dispute or negotiate with buyer — no SP coverage"],
            signature_required=False,
            signature_present=False,
            ce30_applicable=False,
            win_rate_impact=-0.10,
            summary=f"SP {inp.dispute_id}: INELIGIBLE — SNAD not covered",
        )

    if inp.dispute_type == DisputeTypeForSP.SUBSCRIPTION:
        checks.append(SPRequirementCheck(
            "Dispute type",
            SPRequirement.NOT_MET,
            "Subscription/recurring billing disputes not covered by Seller Protection",
        ))
        blocking.append("Subscription disputes are not covered by Seller Protection")
        return SellerProtectionReport(
            dispute_id=inp.dispute_id,
            dispute_type=inp.dispute_type,
            eligibility=SPEligibility.INELIGIBLE,
            requirement_checks=checks,
            blocking_issues=blocking,
            warnings=warnings,
            recommendations=["Provide cancellation policy and subscription terms"],
            signature_required=False,
            signature_present=False,
            ce30_applicable=False,
            win_rate_impact=-0.05,
            summary=f"SP {inp.dispute_id}: INELIGIBLE — subscription not covered",
        )

    checks.append(SPRequirementCheck(
        "Dispute type",
        SPRequirement.MET,
        f"{inp.dispute_type.value} is covered by Seller Protection",
    ))

    # ── Payment method ──────────────────────────────────────────
    if inp.paid_via_paypal_checkout:
        checks.append(SPRequirementCheck(
            "PayPal checkout payment",
            SPRequirement.MET,
            "Transaction made through PayPal checkout",
        ))
    else:
        checks.append(SPRequirementCheck(
            "PayPal checkout payment",
            SPRequirement.NOT_MET,
            "Must be paid via PayPal checkout — personal links excluded",
        ))
        blocking.append("Payment not made through PayPal checkout")

    # ── Item type ────────────────────────────────────────────────
    if inp.is_digital_goods:
        checks.append(SPRequirementCheck(
            "Physical tangible good",
            SPRequirement.NOT_MET,
            "Digital goods are not covered by standard Seller Protection",
        ))
        blocking.append("Digital goods not covered by Seller Protection")
    elif inp.item_category.lower() in _EXCLUDED_CATEGORIES:
        checks.append(SPRequirementCheck(
            "Item category",
            SPRequirement.NOT_MET,
            f"Item category '{inp.item_category}' is excluded",
        ))
        blocking.append(f"Item category '{inp.item_category}' excluded from SP")
    else:
        checks.append(SPRequirementCheck(
            "Physical tangible good",
            SPRequirement.MET,
            "Item qualifies as physical tangible good",
        ))

    # ── Shipped to PayPal address ────────────────────────────────
    if inp.shipped_to_paypal_address and not inp.buyer_address_mismatch:
        checks.append(SPRequirementCheck(
            "Shipped to PayPal address",
            SPRequirement.MET,
            "Shipped to address on PayPal transaction detail",
        ))
    else:
        checks.append(SPRequirementCheck(
            "Shipped to PayPal address",
            SPRequirement.NOT_MET,
            "Must ship to the address shown on the PayPal transaction",
        ))
        blocking.append("Package not shipped to PayPal transaction address")

    # ── Tracking ─────────────────────────────────────────────────
    if inp.tracking_uploaded_to_paypal:
        if inp.tracking_uploaded_before_dispute:
            checks.append(SPRequirementCheck(
                "Tracking uploaded to PayPal",
                SPRequirement.MET,
                "Tracking uploaded before dispute was filed",
            ))
        else:
            checks.append(SPRequirementCheck(
                "Tracking uploaded before dispute",
                SPRequirement.NOT_MET,
                "Tracking must be uploaded before the dispute was filed",
            ))
            warnings.append("Tracking uploaded after dispute — may not qualify")
            recs.append("Always upload tracking to PayPal immediately after shipment")
    else:
        checks.append(SPRequirementCheck(
            "Tracking uploaded to PayPal",
            SPRequirement.NOT_MET,
            "Tracking number not uploaded to PayPal transaction",
        ))
        blocking.append("Tracking number not uploaded to PayPal")
        recs.append("Upload tracking number to the PayPal transaction immediately")

    # ── Buyer confirmed address ──────────────────────────────────
    if inp.buyer_address_confirmed:
        checks.append(SPRequirementCheck(
            "Buyer confirmed address",
            SPRequirement.MET,
            "Buyer's PayPal address is confirmed",
        ))
    else:
        checks.append(SPRequirementCheck(
            "Buyer confirmed address",
            SPRequirement.NOT_MET,
            "Buyer address is unconfirmed — Seller Protection may not apply",
        ))
        warnings.append("Unconfirmed buyer address reduces SP coverage")

    # ── Signature (≥$750) ────────────────────────────────────────
    sig_required = inp.transaction_amount >= _SIGNATURE_THRESHOLD
    if sig_required:
        if inp.has_signature_confirmation:
            checks.append(SPRequirementCheck(
                "Signature confirmation",
                SPRequirement.MET,
                f"Signature obtained for ${inp.transaction_amount:.2f} transaction",
            ))
        else:
            checks.append(SPRequirementCheck(
                "Signature confirmation",
                SPRequirement.NOT_MET,
                f"Signature required for transactions ≥${_SIGNATURE_THRESHOLD:.0f}",
            ))
            blocking.append(f"Missing signature confirmation (required ≥${_SIGNATURE_THRESHOLD:.0f})")
            recs.append("Obtain signed delivery confirmation for high-value shipments")
    else:
        checks.append(SPRequirementCheck(
            "Signature confirmation",
            SPRequirement.NOT_APPLICABLE,
            f"Not required for transactions below ${_SIGNATURE_THRESHOLD:.0f}",
        ))

    # ── CE3.0 applicability (unauthorized only) ──────────────────
    ce30 = (
        inp.dispute_type == DisputeTypeForSP.UNAUTHORIZED
        and inp.has_ce30_qualification
    )

    # ── Eligibility determination ────────────────────────────────
    met_count      = sum(1 for c in checks if c.status == SPRequirement.MET)
    not_met_count  = sum(1 for c in checks if c.status == SPRequirement.NOT_MET)

    # Hard blocks on item type or payment method are always INELIGIBLE
    _hard_block_keywords = ("digital goods", "category", "not made through paypal")
    has_hard_block = any(
        any(kw in b.lower() for kw in _hard_block_keywords)
        for b in blocking
    )

    if not blocking:
        if warnings:
            eligibility = SPEligibility.ELIGIBLE_WITH_GAPS
        else:
            eligibility = SPEligibility.ELIGIBLE
    elif has_hard_block:
        eligibility = SPEligibility.INELIGIBLE
    elif not_met_count <= 1 and len(blocking) == 1:
        eligibility = SPEligibility.PARTIAL
    else:
        eligibility = SPEligibility.INELIGIBLE

    impact_map = {
        SPEligibility.ELIGIBLE:           0.15,
        SPEligibility.ELIGIBLE_WITH_GAPS: 0.08,
        SPEligibility.PARTIAL:            0.02,
        SPEligibility.INELIGIBLE:        -0.05,
    }
    win_impact = impact_map[eligibility]
    if ce30:
        win_impact += 0.18

    if ce30:
        recs.insert(0, "CE3.0 eligible — reference prior non-disputed transactions in response")

    summary = (
        f"SP {inp.dispute_id}: "
        f"{eligibility.value} | blocking={len(blocking)} | "
        f"warnings={len(warnings)} | ce30={ce30}"
    )

    return SellerProtectionReport(
        dispute_id=inp.dispute_id,
        dispute_type=inp.dispute_type,
        eligibility=eligibility,
        requirement_checks=checks,
        blocking_issues=blocking,
        warnings=warnings,
        recommendations=recs,
        signature_required=sig_required,
        signature_present=inp.has_signature_confirmation,
        ce30_applicable=ce30,
        win_rate_impact=win_impact,
        summary=summary,
    )
