"""
Seller Protection Eligibility Checker — determine whether a PayPal dispute
qualifies for Seller Protection (SP) and what the coverage implications are.

PayPal Seller Protection program:
  - Covers merchants against chargebacks and reversals in qualifying disputes
  - Waives the $20 chargeback fee when SP applies
  - Covers INR (Item Not Received) and Unauthorized transactions
  - NEVER covers SNAD (Significantly Not As Described) disputes

SP eligibility requirements:
  1. Transaction on PayPal (not card network-initiated)
  2. Tangible item (not digital goods, services, or intangible items)
  3. Shipped to the address on the PayPal transaction
  4. Proof of shipment provided (tracking number)
  5. For amounts ≥ $750: signature confirmation required
  6. Dispute type is INR or Unauthorized (NOT SNAD)
  7. Transaction not marked as "No Seller Protection" at time of sale
  8. Single transaction (not partial payment)

Enhanced Seller Protection (for UNAUTHORIZED only):
  - Additional requirements: 2FA verification, no account breach

SP limitations:
  - Does not apply to custom or made-to-order items in SNAD context
  - Does not cover digital downloads
  - Does not cover services (coaching, consulting, etc.)
  - Ticket re-sellers: additional requirements apply

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SPEligibilityStatus(str, Enum):
    FULLY_ELIGIBLE     = "FULLY_ELIGIBLE"     # All requirements met
    CONDITIONALLY_ELIGIBLE = "CONDITIONALLY_ELIGIBLE"  # Met with caveats
    INELIGIBLE         = "INELIGIBLE"         # Does not qualify
    UNKNOWN            = "UNKNOWN"            # Cannot determine without more info


class SPCoverageType(str, Enum):
    INR_COVERAGE          = "INR_COVERAGE"           # Item Not Received
    UNAUTHORIZED_COVERAGE = "UNAUTHORIZED_COVERAGE"  # Unauthorized transaction
    NO_COVERAGE           = "NO_COVERAGE"            # SNAD or ineligible type
    ENHANCED_COVERAGE     = "ENHANCED_COVERAGE"      # Unauthorized + 2FA


class SPEligibilityGapType(str, Enum):
    WRONG_DISPUTE_TYPE        = "WRONG_DISPUTE_TYPE"
    MISSING_TRACKING          = "MISSING_TRACKING"
    MISSING_SIGNATURE         = "MISSING_SIGNATURE"      # Required for ≥ $750
    WRONG_SHIP_ADDRESS        = "WRONG_SHIP_ADDRESS"
    DIGITAL_GOODS             = "DIGITAL_GOODS"
    INTANGIBLE_ITEM           = "INTANGIBLE_ITEM"
    NO_SP_FLAG_AT_SALE        = "NO_SP_FLAG_AT_SALE"
    PARTIAL_PAYMENT           = "PARTIAL_PAYMENT"
    TICKET_RESELLER_RULES     = "TICKET_RESELLER_RULES"
    CUSTOM_ITEM               = "CUSTOM_ITEM"


@dataclass
class SPEligibilityGap:
    """One gap in Seller Protection eligibility."""
    gap_type: SPEligibilityGapType
    is_blocking: bool           # True = disqualifies SP outright
    description: str
    remediation: str            # What could fix this (if anything)


@dataclass
class SellerProtectionInput:
    """Input for Seller Protection eligibility check."""
    dispute_id: str
    claim_type: str              # INR / SNAD / UNAUTHORIZED / DUPLICATE

    # Transaction characteristics
    transaction_amount: float
    item_is_tangible: bool = True
    item_is_digital: bool = False
    item_is_service: bool = False
    item_is_custom: bool = False
    item_is_ticket: bool = False      # Event ticket (special rules)

    # Shipping evidence
    has_tracking_number: bool = False
    tracking_carrier: str = ""
    shipped_to_paypal_address: bool = True  # Delivery address matches PayPal transaction

    # Signature confirmation (required ≥ $750)
    has_signature_confirmation: bool = False

    # Transaction flags
    transaction_marked_no_sp: bool = False   # "No Seller Protection" at sale time
    is_partial_payment: bool = False         # Part of a split/installment payment

    # Enhanced SP (unauthorized only)
    buyer_account_verified_2fa: bool = False  # Buyer used 2FA at time of transaction
    no_account_breach_detected: bool = True   # No sign of account takeover

    # Miscellaneous
    is_physical_goods: Optional[bool] = None  # Override for edge cases


@dataclass
class SellerProtectionReport:
    """Seller Protection eligibility report."""
    dispute_id: str

    # Core determination
    eligibility_status: SPEligibilityStatus
    coverage_type: SPCoverageType

    # Detail
    gaps: list[SPEligibilityGap]
    blocking_gaps: int
    fixable_gaps: int              # Gaps that could be remediated

    # Financial impact
    chargeback_fee_waived: bool    # True if SP means $20 fee is waived
    fee_savings: float             # Amount saved by SP

    # Guidance
    sp_recommendation: str
    missing_evidence: list[str]    # Specific evidence items to gather
    signature_required: bool       # True if ≥ $750 → signature required

    summary: str


def _check_dispute_type(inp: SellerProtectionInput) -> Optional[SPEligibilityGap]:
    ct = inp.claim_type.upper()
    if ct == "SNAD":
        return SPEligibilityGap(
            gap_type=SPEligibilityGapType.WRONG_DISPUTE_TYPE,
            is_blocking=True,
            description="SNAD disputes are NEVER covered by Seller Protection",
            remediation="No remediation — SNAD is excluded from SP by policy",
        )
    if ct not in ("INR", "UNAUTHORIZED", "DUPLICATE"):
        return SPEligibilityGap(
            gap_type=SPEligibilityGapType.WRONG_DISPUTE_TYPE,
            is_blocking=True,
            description=f"Claim type '{inp.claim_type}' is not covered by Seller Protection",
            remediation="Verify claim type classification with PayPal",
        )
    return None


def _check_item_type(inp: SellerProtectionInput) -> list[SPEligibilityGap]:
    gaps = []
    if inp.item_is_digital:
        gaps.append(SPEligibilityGap(
            gap_type=SPEligibilityGapType.DIGITAL_GOODS,
            is_blocking=True,
            description="Digital goods are excluded from Seller Protection",
            remediation="No remediation — digital goods policy exclusion",
        ))
    if inp.item_is_service:
        gaps.append(SPEligibilityGap(
            gap_type=SPEligibilityGapType.INTANGIBLE_ITEM,
            is_blocking=True,
            description="Services (intangible) are excluded from Seller Protection",
            remediation="No remediation — service transactions not eligible",
        ))
    if inp.item_is_ticket:
        gaps.append(SPEligibilityGap(
            gap_type=SPEligibilityGapType.TICKET_RESELLER_RULES,
            is_blocking=False,
            description="Event ticket transactions have additional SP requirements",
            remediation="Provide proof of ticket transfer and buyer authorization",
        ))
    return gaps


def _check_shipping(inp: SellerProtectionInput) -> list[SPEligibilityGap]:
    gaps = []
    ct = inp.claim_type.upper()

    # Tracking is required for INR disputes
    if ct == "INR" and not inp.has_tracking_number:
        gaps.append(SPEligibilityGap(
            gap_type=SPEligibilityGapType.MISSING_TRACKING,
            is_blocking=True,
            description="INR dispute requires proof of shipment (tracking number)",
            remediation="Obtain and upload carrier tracking confirmation",
        ))

    # Delivery must be to PayPal address
    if not inp.shipped_to_paypal_address:
        gaps.append(SPEligibilityGap(
            gap_type=SPEligibilityGapType.WRONG_SHIP_ADDRESS,
            is_blocking=True,
            description="Item was not shipped to the address on the PayPal transaction",
            remediation="No remediation — address mismatch disqualifies SP",
        ))

    # Signature required for ≥ $750
    if inp.transaction_amount >= 750.0 and not inp.has_signature_confirmation:
        gaps.append(SPEligibilityGap(
            gap_type=SPEligibilityGapType.MISSING_SIGNATURE,
            is_blocking=True,
            description=(f"Transaction ${inp.transaction_amount:.2f} ≥ $750 requires "
                         "signature confirmation for SP coverage"),
            remediation="Obtain signature confirmation from carrier records",
        ))

    return gaps


def _check_sale_flags(inp: SellerProtectionInput) -> list[SPEligibilityGap]:
    gaps = []
    if inp.transaction_marked_no_sp:
        gaps.append(SPEligibilityGap(
            gap_type=SPEligibilityGapType.NO_SP_FLAG_AT_SALE,
            is_blocking=True,
            description="Transaction was flagged 'No Seller Protection' at time of sale",
            remediation="No remediation — flag was set by PayPal at transaction time",
        ))
    if inp.is_partial_payment:
        gaps.append(SPEligibilityGap(
            gap_type=SPEligibilityGapType.PARTIAL_PAYMENT,
            is_blocking=True,
            description="Partial/installment payments are excluded from Seller Protection",
            remediation="Confirm with PayPal whether full transaction is eligible",
        ))
    return gaps


def check_seller_protection(inp: SellerProtectionInput) -> SellerProtectionReport:
    """
    Determine Seller Protection eligibility for a dispute.
    """
    all_gaps: list[SPEligibilityGap] = []

    # 1. Dispute type check
    type_gap = _check_dispute_type(inp)
    if type_gap:
        all_gaps.append(type_gap)

    # 2. Item type checks
    all_gaps.extend(_check_item_type(inp))

    # 3. Shipping/delivery checks (only relevant if not already disqualified by type)
    ct = inp.claim_type.upper()
    if ct != "SNAD":
        all_gaps.extend(_check_shipping(inp))

    # 4. Sale-time flags
    all_gaps.extend(_check_sale_flags(inp))

    blocking = sum(1 for g in all_gaps if g.is_blocking)
    fixable  = sum(1 for g in all_gaps
                   if not g.is_blocking and "No remediation" not in g.remediation)

    signature_required = inp.transaction_amount >= 750.0

    # Determine eligibility status
    if blocking > 0:
        status = SPEligibilityStatus.INELIGIBLE
    elif len(all_gaps) > 0:
        status = SPEligibilityStatus.CONDITIONALLY_ELIGIBLE
    else:
        status = SPEligibilityStatus.FULLY_ELIGIBLE

    # Determine coverage type
    if status == SPEligibilityStatus.INELIGIBLE:
        coverage = SPCoverageType.NO_COVERAGE
    elif ct == "UNAUTHORIZED":
        if inp.buyer_account_verified_2fa and inp.no_account_breach_detected:
            coverage = SPCoverageType.ENHANCED_COVERAGE
        else:
            coverage = SPCoverageType.UNAUTHORIZED_COVERAGE
    else:
        coverage = SPCoverageType.INR_COVERAGE

    # Fee impact
    cb_waived = status in (SPEligibilityStatus.FULLY_ELIGIBLE,
                            SPEligibilityStatus.CONDITIONALLY_ELIGIBLE)
    fee_savings = 20.0 if cb_waived else 0.0

    # Build missing evidence list
    missing_evidence: list[str] = []
    for g in all_gaps:
        if "No remediation" not in g.remediation:
            missing_evidence.append(g.remediation)

    if status == SPEligibilityStatus.FULLY_ELIGIBLE:
        recommendation = "SP coverage confirmed — $20 chargeback fee will be waived if merchant loses"
    elif status == SPEligibilityStatus.CONDITIONALLY_ELIGIBLE:
        recommendation = "SP likely applies — resolve minor gaps to ensure coverage"
    else:
        reason = all_gaps[0].description if all_gaps else "Unknown disqualification"
        recommendation = f"SP does not apply — {reason}"

    summary = (
        f"SP {inp.dispute_id}: "
        f"status={status.value} | coverage={coverage.value} | "
        f"blocking_gaps={blocking} | fee_savings=${fee_savings:.0f} | "
        f"sig_required={signature_required}"
    )

    return SellerProtectionReport(
        dispute_id=inp.dispute_id,
        eligibility_status=status,
        coverage_type=coverage,
        gaps=all_gaps,
        blocking_gaps=blocking,
        fixable_gaps=fixable,
        chargeback_fee_waived=cb_waived,
        fee_savings=fee_savings,
        sp_recommendation=recommendation,
        missing_evidence=missing_evidence,
        signature_required=signature_required,
        summary=summary,
    )
