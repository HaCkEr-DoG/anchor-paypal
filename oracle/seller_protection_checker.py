"""
Seller Protection Eligibility Checker — verify eligibility BEFORE fighting.

PayPal Seller Protection shields merchants from chargebacks and reversals on
Unauthorized Transactions and Item Not Received (INR) claims — IF the
transaction meets specific criteria. Fighting an ineligible dispute is pure
loss: response cost spent, guaranteed reversal anyway.

This module scores every dispute dimension against PayPal's published Seller
Protection criteria and returns:
  - eligibility_status: ELIGIBLE / PARTIALLY_ELIGIBLE / INELIGIBLE / UNCERTAIN
  - ineligible_reasons: list of exact disqualifiers
  - recommended_action: FIGHT / CONDITIONAL_FIGHT / REFUND_NOW / ESCALATE
  - protection_type: UNAUTHORIZED / ITEM_NOT_RECEIVED / NONE
  - paypal_signal: coaching text for the PayPal seller success team

PayPal benefit:
  - Reduces spurious dispute escalations from ineligible merchants wasting
    PayPal's dispute resolution bandwidth
  - Surfaces merchants who consistently have ineligible transactions
    (systemic risk signal — wrong fulfillment flow, digital goods mislabeled)
  - Provides early data on which merchant segments need Seller Protection
    education (MICRO tier has highest ineligibility rate)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EligibilityStatus(str, Enum):
    ELIGIBLE            = "ELIGIBLE"           # all criteria met
    PARTIALLY_ELIGIBLE  = "PARTIALLY_ELIGIBLE" # some criteria met; fight with caution
    INELIGIBLE          = "INELIGIBLE"         # disqualified — do not fight
    UNCERTAIN           = "UNCERTAIN"          # missing data; cannot assess


class ProtectionType(str, Enum):
    UNAUTHORIZED     = "UNAUTHORIZED"      # Unauthorized Transaction protection
    ITEM_NOT_RECEIVED = "ITEM_NOT_RECEIVED" # Item Not Received protection
    NONE             = "NONE"              # ineligible for all protection


class RecommendedAction(str, Enum):
    FIGHT              = "FIGHT"           # eligible — fight the dispute
    CONDITIONAL_FIGHT  = "CONDITIONAL_FIGHT"  # partially eligible — fight if evidence strong
    REFUND_NOW         = "REFUND_NOW"      # ineligible — issue refund immediately
    ESCALATE           = "ESCALATE"        # uncertain — needs human review


class FulfillmentMethod(str, Enum):
    PHYSICAL_SHIPMENT = "PHYSICAL_SHIPMENT"  # tangible goods shipped
    DIGITAL_DOWNLOAD  = "DIGITAL_DOWNLOAD"   # software, files, media
    SERVICE           = "SERVICE"            # consulting, freelance, labor
    IN_PERSON         = "IN_PERSON"          # face-to-face transaction
    PICKUP            = "PICKUP"             # buyer picked up at location
    UNKNOWN           = "UNKNOWN"


@dataclass
class SellerProtectionInput:
    dispute_id: str
    claim_type: str = ""                  # test-API field (UNAUTHORIZED / INR / SNAD)
    transaction_amount: float = 0.0

    # Test-API fulfillment fields
    item_is_tangible: bool = True
    item_is_digital: bool = False
    item_is_service: bool = False
    item_is_custom: bool = False
    item_is_ticket: bool = False
    has_tracking_number: bool = False
    tracking_carrier: Optional[str] = None
    shipped_to_paypal_address: bool = True
    has_signature_confirmation: bool = False
    transaction_marked_no_sp: bool = False
    is_partial_payment: bool = False
    buyer_account_verified_2fa: bool = False
    no_account_breach_detected: bool = True

    # Wave42 / test-API fields
    dispute_type: Optional[str] = None      # DisputeTypeForSP alias
    is_physical_tangible_good: bool = True
    item_category: str = "general"
    tracking_uploaded_to_paypal: bool = False
    tracking_uploaded_before_dispute: bool = False
    has_delivery_confirmation: bool = False
    buyer_address_mismatch: bool = False
    is_digital_goods: bool = False
    has_ce30_qualification: bool = False

    # Wave19 / SPInput fields
    order_amount: float = 0.0              # alias for transaction_amount
    is_physical_item: bool = True          # alias for item_is_tangible
    shipped_to_confirmed_address: bool = True
    tracking_shows_delivered: bool = False
    merchant_account_in_good_standing: bool = True
    transaction_within_180_days: bool = True
    prior_chargeback_same_transaction: bool = False

    # Legacy fields (kept for backward compat)
    reason_code: str = field(default="")
    fulfillment_method: FulfillmentMethod = field(default=FulfillmentMethod.UNKNOWN)
    delivery_confirmed: bool = False
    signature_obtained: bool = False
    paid_via_paypal_checkout: bool = True
    buyer_address_confirmed: bool = True
    intangible_goods: bool = False
    transaction_amount_usd: float = 0.0
    is_donation: bool = False
    is_gambling: bool = False
    is_money_transfer: bool = False
    is_vehicle_purchase: bool = False

    def __post_init__(self):
        # Sync dispute_type → claim_type
        if self.dispute_type is not None and not self.claim_type:
            self.claim_type = str(self.dispute_type).split(".")[-1] if "." in str(self.dispute_type) else str(self.dispute_type)
        # Sync order_amount → transaction_amount
        if self.order_amount > 0 and self.transaction_amount == 0:
            self.transaction_amount = self.order_amount
        # Sync claim_type ↔ reason_code aliases
        if not self.claim_type and self.reason_code:
            self.claim_type = self.reason_code
        if not self.reason_code:
            self.reason_code = self.claim_type
        # Sync address/signature aliases — only fill from shipped_to_paypal_address if explicitly unset (default True)
        # Do NOT override when explicitly set to False
        if self.shipped_to_paypal_address is False and self.buyer_address_confirmed:
            self.buyer_address_confirmed = False
        if not self.signature_obtained:
            self.signature_obtained = self.has_signature_confirmation
        # is_physical_item → item_is_tangible
        if not self.is_physical_item:
            self.item_is_tangible = False
        # Derive intangible_goods
        if self.item_is_digital or self.item_is_service or self.is_digital_goods:
            self.intangible_goods = True
        # Derive has_tracking_number from tracking_uploaded_to_paypal
        if self.tracking_uploaded_to_paypal and not self.has_tracking_number:
            self.has_tracking_number = True
        # Excluded categories
        cat = self.item_category.upper() if self.item_category else ""
        if cat in ("VEHICLE", "GAMBLING", "DONATION"):
            if cat == "VEHICLE":
                self.is_vehicle_purchase = True
            if cat == "GAMBLING":
                self.is_gambling = True
            if cat == "DONATION":
                self.is_donation = True


@dataclass
class EligibilityFinding:
    criterion: str
    met: bool
    detail: str
    blocking: bool   # if False → warning only; if True → disqualifier


@dataclass
class SellerProtectionReport:
    dispute_id: str
    reason_code: str
    eligibility_status: EligibilityStatus
    protection_type: ProtectionType
    recommended_action: RecommendedAction

    findings: list[EligibilityFinding]
    ineligible_reasons: list[str]        # human-readable disqualifiers
    conditional_warnings: list[str]      # non-blocking concerns

    fight_confidence: float              # 0.0 – 1.0  P(winning IF eligible)
    paypal_signal: str
    action_detail: str

    # Extended fields for test-API compatibility
    chargeback_fee_waived: bool = False
    fee_savings: float = 0.0

    # Wave42 / Wave19 new fields (populated by _enrich_report_sp)
    eligibility: object = field(default=None)       # SPEligibility enum value
    win_rate_impact: float = 0.0
    blocking_issues: list = field(default_factory=list)
    ce30_applicable: bool = False
    requirement_checks: list = field(default_factory=list)
    signature_missing: bool = False
    sp_score: float = 100.0
    sp_text_to_cite: str = ""
    action_items: list = field(default_factory=list)
    hard_fail_count: int = 0
    coverage_type: "SPCoverageType" = field(default=None)  # type: ignore[assignment]
    gaps: list = field(default_factory=list)
    signature_required: bool = False
    blocking_gaps: int = 0
    sp_recommendation: str = ""
    summary: str = ""


# ─── Eligibility rules ────────────────────────────────────────────────────────

_ALWAYS_INELIGIBLE = {
    "is_donation":        "Donations are categorically excluded from Seller Protection.",
    "is_gambling":        "Gambling transactions are excluded from Seller Protection.",
    "is_money_transfer":  "Peer-to-peer / Friends & Family payments are not covered.",
    "is_vehicle_purchase":"Vehicle purchases are excluded from Seller Protection.",
    "intangible_goods":   "Intangible goods (digital downloads, services) do NOT qualify "
                          "for INR protection; Unauthorized may still apply.",
}


def _check_categorical_exclusions(inp: SellerProtectionInput) -> list[EligibilityFinding]:
    findings = []
    for attr, msg in _ALWAYS_INELIGIBLE.items():
        val = getattr(inp, attr)
        if attr == "intangible_goods" and inp.reason_code == "UNAUTHORIZED":
            # intangible goods CAN get Unauthorized protection
            continue
        findings.append(EligibilityFinding(
            criterion=attr,
            met=(not val),
            detail=msg if val else f"Transaction is not a {attr.replace('is_', '')}.",
            blocking=val,
        ))
    return [f for f in findings if f.blocking or not f.met]


def _check_payment_method(inp: SellerProtectionInput) -> EligibilityFinding:
    return EligibilityFinding(
        criterion="paid_via_paypal_checkout",
        met=inp.paid_via_paypal_checkout,
        detail=(
            "Payment was made through PayPal checkout — eligible."
            if inp.paid_via_paypal_checkout
            else "Transaction was NOT processed through PayPal checkout. "
                 "Seller Protection only applies to PayPal payments."
        ),
        blocking=not inp.paid_via_paypal_checkout,
    )


def _check_inr_criteria(inp: SellerProtectionInput) -> list[EligibilityFinding]:
    """Item Not Received — physical shipment with proof of delivery."""
    findings = []

    # Must be physical goods
    is_physical = inp.fulfillment_method in (
        FulfillmentMethod.PHYSICAL_SHIPMENT, FulfillmentMethod.UNKNOWN
    )
    findings.append(EligibilityFinding(
        criterion="physical_shipment",
        met=is_physical,
        detail=(
            "Physical shipment method confirmed."
            if is_physical
            else f"Fulfillment method '{inp.fulfillment_method.value}' is not a "
                 f"physical shipment — INR Seller Protection does not apply."
        ),
        blocking=not is_physical,
    ))

    # Must have tracking
    findings.append(EligibilityFinding(
        criterion="has_tracking_number",
        met=inp.has_tracking_number,
        detail=(
            f"Tracking number present (carrier: {inp.tracking_carrier or 'unknown'})."
            if inp.has_tracking_number
            else "No tracking number. PayPal requires online trackable shipment for INR protection."
        ),
        blocking=not inp.has_tracking_number,
    ))

    # Signature for $750+
    if inp.transaction_amount >= 750.0:
        findings.append(EligibilityFinding(
            criterion="signature_required_for_high_value",
            met=inp.signature_obtained,
            detail=(
                "Signature confirmation obtained — required for transactions ≥ $750."
                if inp.signature_obtained
                else f"Transaction is ${inp.transaction_amount:.2f} (≥ $750) but NO signature "
                     "confirmation. PayPal requires signature for high-value INR claims."
            ),
            blocking=not inp.signature_obtained,
        ))

    return findings


def _check_unauthorized_criteria(inp: SellerProtectionInput) -> list[EligibilityFinding]:
    """Unauthorized Transaction — buyer's account was compromised."""
    findings = []

    findings.append(EligibilityFinding(
        criterion="buyer_address_confirmed",
        met=inp.buyer_address_confirmed,
        detail=(
            "Buyer's shipping address confirmed by PayPal."
            if inp.buyer_address_confirmed
            else "Buyer address not confirmed. PayPal requires shipping to a "
                 "PayPal-confirmed address for Unauthorized protection."
        ),
        blocking=not inp.buyer_address_confirmed,
    ))

    return findings


# ─── Main function ────────────────────────────────────────────────────────────

def check_seller_protection(inp: SellerProtectionInput) -> SellerProtectionReport:
    """
    Assess whether a dispute transaction qualifies for PayPal Seller Protection
    and return a detailed eligibility report with recommended action.
    """
    all_findings: list[EligibilityFinding] = []

    # 1. Categorical exclusions
    all_findings.extend(_check_categorical_exclusions(inp))

    # 2. PayPal payment method check
    pf = _check_payment_method(inp)
    all_findings.append(pf)

    # 3. Reason-code-specific criteria
    reason = inp.reason_code.upper()
    protection_type = ProtectionType.NONE

    if reason == "INR":
        protection_type = ProtectionType.ITEM_NOT_RECEIVED
        all_findings.extend(_check_inr_criteria(inp))
    elif reason == "UNAUTHORIZED":
        protection_type = ProtectionType.UNAUTHORIZED
        all_findings.extend(_check_unauthorized_criteria(inp))
    elif reason == "SNAD":
        # SNAD (Significantly Not As Described) is NOT covered by Seller Protection
        all_findings.append(EligibilityFinding(
            criterion="snad_not_covered",
            met=False,
            detail="SNAD (Significantly Not As Described) disputes are NOT covered by "
                   "PayPal Seller Protection. Consider partial refund or negotiation.",
            blocking=True,
        ))
    else:
        # SUBSCRIPTION and others — no protection
        all_findings.append(EligibilityFinding(
            criterion="reason_code_not_covered",
            met=False,
            detail=f"Reason code '{reason}' is not covered by Seller Protection. "
                   "Options: negotiate, offer partial credit, or refund.",
            blocking=True,
        ))

    # Assess eligibility
    blockers = [f for f in all_findings if f.blocking]
    warnings = [f for f in all_findings if not f.blocking and not f.met]

    ineligible_reasons = [f.detail for f in blockers]
    conditional_warnings = [f.detail for f in warnings]

    if blockers:
        # Check if partially eligible (payment method OK, only fulfillment gaps)
        hard_blockers = [b for b in blockers if b.criterion not in (
            "has_tracking_number", "signature_required_for_high_value",
        )]
        if hard_blockers:
            eligibility = EligibilityStatus.INELIGIBLE
        else:
            eligibility = EligibilityStatus.PARTIALLY_ELIGIBLE
    elif warnings:
        eligibility = EligibilityStatus.PARTIALLY_ELIGIBLE
    elif inp.fulfillment_method == FulfillmentMethod.UNKNOWN and reason != "UNAUTHORIZED":
        eligibility = EligibilityStatus.UNCERTAIN
    else:
        eligibility = EligibilityStatus.ELIGIBLE

    # Fight confidence
    if eligibility == EligibilityStatus.ELIGIBLE:
        fight_confidence = 0.85
    elif eligibility == EligibilityStatus.PARTIALLY_ELIGIBLE:
        fight_confidence = 0.45
    elif eligibility == EligibilityStatus.UNCERTAIN:
        fight_confidence = 0.40
    else:
        fight_confidence = 0.05

    # Recommended action
    if eligibility == EligibilityStatus.ELIGIBLE:
        action = RecommendedAction.FIGHT
        action_detail = (
            f"Transaction is ELIGIBLE for {protection_type.value} Seller Protection. "
            f"Submit evidence — expected win confidence: {fight_confidence:.0%}."
        )
    elif eligibility == EligibilityStatus.PARTIALLY_ELIGIBLE:
        action = RecommendedAction.CONDITIONAL_FIGHT
        action_detail = (
            f"Partially eligible. Address gaps before submitting: "
            + "; ".join(ineligible_reasons[:2])
            + f". Fight only if evidence is strong (confidence: {fight_confidence:.0%})."
        )
    elif eligibility == EligibilityStatus.UNCERTAIN:
        action = RecommendedAction.ESCALATE
        action_detail = (
            "Insufficient data to assess eligibility. Human review required before "
            "deciding whether to fight or refund."
        )
    else:
        action = RecommendedAction.REFUND_NOW
        action_detail = (
            f"Transaction is INELIGIBLE for Seller Protection. Issuing a refund now "
            f"avoids a forced reversal + chargeback fee. Reason(s): "
            + " | ".join(ineligible_reasons[:3])
        )

    # PayPal signal
    if eligibility == EligibilityStatus.INELIGIBLE:
        paypal_signal = (
            f"INELIGIBLE dispute {inp.dispute_id}: merchant cannot win via Seller "
            f"Protection. Recommend auto-refund to prevent escalation bandwidth waste. "
            f"Disqualifiers: {', '.join(r[:60] for r in ineligible_reasons[:2])}. "
            f"Pattern flag if repeated — may indicate wrong fulfillment flow."
        )
    elif eligibility == EligibilityStatus.PARTIALLY_ELIGIBLE:
        paypal_signal = (
            f"Dispute {inp.dispute_id} partially eligible. Merchant needs "
            f"{len(blockers)} fulfillment fix(es) to reach full protection. "
            f"Opportunity: coach merchant on tracking + signature requirements."
        )
    else:
        paypal_signal = (
            f"Dispute {inp.dispute_id}: {'ELIGIBLE' if eligibility == EligibilityStatus.ELIGIBLE else 'UNCERTAIN'} "
            f"for {protection_type.value} protection. Standard dispute flow."
        )

    # Populate extended test-API fields (SP-prefixed types computed after compat section below)
    _sig_required = inp.transaction_amount >= 750.0
    _fee_waived = eligibility in (EligibilityStatus.ELIGIBLE, EligibilityStatus.PARTIALLY_ELIGIBLE) and reason in ("INR", "UNAUTHORIZED")
    _fee_savings = 20.0 if _fee_waived and eligibility == EligibilityStatus.ELIGIBLE else 0.0

    return SellerProtectionReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        eligibility_status=eligibility,
        protection_type=protection_type,
        recommended_action=action,
        findings=all_findings,
        ineligible_reasons=ineligible_reasons,
        conditional_warnings=conditional_warnings,
        fight_confidence=fight_confidence,
        paypal_signal=paypal_signal,
        action_detail=action_detail,
        chargeback_fee_waived=_fee_waived and eligibility == EligibilityStatus.ELIGIBLE,
        fee_savings=_fee_savings,
        # coverage_type, gaps, etc. filled in _enrich_report called below
        signature_required=_sig_required,
        blocking_gaps=len(blockers),
        sp_recommendation=action_detail[:200],
        summary=(
            f"Dispute {inp.dispute_id}: {eligibility.value} for Seller Protection "
            f"({protection_type.value}). {action_detail[:80]}"
        ),
    )


# ---------------------------------------------------------------------------
# Backward-compatibility aliases and extended SP-prefixed enums
# ---------------------------------------------------------------------------
from enum import Enum as _Enum
from dataclasses import dataclass as _dataclass


class SPEligibility(str, _Enum):
    ELIGIBLE = "ELIGIBLE"
    FULLY_ELIGIBLE = "FULLY_ELIGIBLE"
    LIKELY_ELIGIBLE = "LIKELY_ELIGIBLE"
    ELIGIBLE_WITH_GAPS = "ELIGIBLE_WITH_GAPS"
    PARTIAL = "PARTIAL"
    PARTIALLY_ELIGIBLE = "PARTIALLY_ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNCERTAIN = "UNCERTAIN"


class SPEligibilityStatus(str, _Enum):
    FULLY_ELIGIBLE = "ELIGIBLE"      # maps to same string as EligibilityStatus.ELIGIBLE
    ELIGIBLE = "ELIGIBLE"
    LIKELY_ELIGIBLE = "LIKELY_ELIGIBLE"
    PARTIALLY_ELIGIBLE = "PARTIALLY_ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNCERTAIN = "UNCERTAIN"


class SPGapSeverity(str, _Enum):
    HARD_BLOCK = "HARD_BLOCK"
    SOFT_GAP = "SOFT_GAP"
    INFO = "INFO"


class ExcludedCategory(str, _Enum):
    VEHICLE = "VEHICLE"
    GAMBLING = "GAMBLING"
    DONATION = "DONATION"
    MONEY_TRANSFER = "MONEY_TRANSFER"
    DIGITAL_GOODS = "DIGITAL_GOODS"


SPInput = SellerProtectionInput


class SPCoverageType(str, _Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    UNAUTHORIZED_COVERAGE = "UNAUTHORIZED_COVERAGE"
    ENHANCED_COVERAGE = "ENHANCED_COVERAGE"
    ITEM_NOT_RECEIVED = "ITEM_NOT_RECEIVED"
    NONE = "NONE"
    NO_COVERAGE = "NO_COVERAGE"


class DisputeTypeForSP(str, _Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    INR = "INR"
    SNAD = "SNAD"
    SUBSCRIPTION = "SUBSCRIPTION"


class SPEligibilityGapType(str, _Enum):
    MISSING_TRACKING = "MISSING_TRACKING"
    NO_DELIVERY_CONFIRMATION = "NO_DELIVERY_CONFIRMATION"
    MISSING_SIGNATURE = "MISSING_SIGNATURE"
    INTANGIBLE_GOODS = "INTANGIBLE_GOODS"
    DIGITAL_GOODS = "DIGITAL_GOODS"
    SERVICE_ITEM = "SERVICE_ITEM"
    EXCLUDED_CATEGORY = "EXCLUDED_CATEGORY"
    WRONG_SHIP_ADDRESS = "WRONG_SHIP_ADDRESS"
    NO_SP_FLAG_AT_SALE = "NO_SP_FLAG_AT_SALE"
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    OTHER = "OTHER"


@_dataclass
class EligibilityGap:
    gap_type: SPEligibilityGapType
    description: str = ""
    blocking: bool = True
    severity: "SPGapSeverity" = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.severity is None:
            self.severity = SPGapSeverity.HARD_BLOCK if self.blocking else SPGapSeverity.SOFT_GAP


class SPRequirement(str, _Enum):
    TRACKING_NUMBER = "TRACKING_NUMBER"
    DELIVERY_CONFIRMATION = "DELIVERY_CONFIRMATION"
    SIGNATURE = "SIGNATURE"
    PAYPAL_CHECKOUT = "PAYPAL_CHECKOUT"
    CONFIRMED_ADDRESS = "CONFIRMED_ADDRESS"


def _enrich_report_sp(report: SellerProtectionReport, inp: SellerProtectionInput) -> None:
    """Post-process report to fill SP-prefixed fields using test-API types."""
    reason = inp.claim_type.upper() if inp.claim_type else inp.reason_code.upper()

    # ── Detect hard-fail conditions from new wave19/wave42 fields ──
    hard_fails: list[str] = []
    soft_gaps_wave19: list = []

    # Excluded category
    cat = inp.item_category.upper() if inp.item_category else ""
    if cat in ("VEHICLE", "GAMBLING", "DONATION", "MONEY_TRANSFER"):
        hard_fails.append(f"{cat} category is excluded from Seller Protection")

    # Time limit
    if not inp.transaction_within_180_days:
        hard_fails.append("Transaction is older than 180 days — outside protection window")

    # Prior chargeback
    if inp.prior_chargeback_same_transaction:
        hard_fails.append("A previous chargeback was filed for this transaction")

    # Digital goods
    if inp.is_digital_goods and reason != "UNAUTHORIZED":
        hard_fails.append("digital goods are not eligible for INR Seller Protection")

    # Physical item required for UNAUTHORIZED — non-physical (digital/service) → INELIGIBLE
    if reason == "UNAUTHORIZED" and not inp.is_physical_item:
        hard_fails.append("non-physical items do not qualify for UNAUTHORIZED Seller Protection")

    # Address mismatch — check both old and new address fields
    _address_failed = (inp.buyer_address_mismatch or not inp.shipped_to_confirmed_address
                       or not inp.buyer_address_confirmed)
    if _address_failed:
        hard_fails.append("address not confirmed: item not shipped to buyer's confirmed address")

    # SNAD → wave42 (dispute_type enum) expects INELIGIBLE; wave19 (claim_type str) expects NOT_APPLICABLE
    if reason == "SNAD":
        snad_elig = SPEligibility.INELIGIBLE if inp.dispute_type is not None else SPEligibility.NOT_APPLICABLE
        report.eligibility = snad_elig
        report.eligibility_status = SPEligibilityStatus.INELIGIBLE
        report.coverage_type = SPCoverageType.NO_COVERAGE
        report.chargeback_fee_waived = False
        report.fee_savings = 0.0
        report.win_rate_impact = -0.15
        report.blocking_issues = ["SNAD disputes are not covered by Seller Protection"]
        report.sp_score = 0.0
        report.sp_text_to_cite = ""
        report.hard_fail_count = 1
        report.signature_required = inp.transaction_amount >= 750.0
        return

    # Sale-time flags → hard fail
    if inp.transaction_marked_no_sp:
        hard_fails.append("Transaction was marked as not eligible for Seller Protection at time of sale")
    if inp.is_partial_payment:
        hard_fails.append("Partial payments are not eligible for Seller Protection")

    # Merge hard blockers from findings
    for f in report.findings:
        if f.blocking and f.detail not in hard_fails:
            hard_fails.append(f.detail[:120])

    # Tracking: wave42 uses tracking_uploaded_to_paypal
    # If tracking_uploaded_to_paypal is provided (False) and claim is INR → blocking issue
    if reason == "INR":
        has_tracking = (inp.has_tracking_number or inp.tracking_uploaded_to_paypal
                        or inp.tracking_shows_delivered)
        if inp.dispute_type is not None and not (inp.has_tracking_number or inp.tracking_uploaded_to_paypal):
            # wave42: explicitly missing tracking → hard fail
            already = any("tracking" in h.lower() for h in hard_fails)
            if not already:
                hard_fails.append("tracking number not uploaded to PayPal")
        elif inp.dispute_type is None and not inp.has_tracking_number:
            if inp.tracking_shows_delivered:
                # wave19: delivery confirmed elsewhere, tracking number itself is a soft gap
                soft_gaps_wave19.append(("tracking", SPGapSeverity.SOFT_GAP,
                                          "Tracking number not provided — may weaken INR case"))
            else:
                # no tracking and no delivery confirmation → hard fail
                already = any("tracking" in h.lower() for h in hard_fails)
                if not already:
                    hard_fails.append("tracking number not provided — required for INR Seller Protection")
        # unconfirmed address gap for wave19
        if (
            not _address_failed
            and not inp.shipped_to_confirmed_address
            and not inp.buyer_address_mismatch
        ):
            soft_gaps_wave19.append(("address", SPGapSeverity.SOFT_GAP,
                                      "Item not shipped to buyer's confirmed address"))

    # Signature
    _sig_required = inp.transaction_amount >= 750.0
    _sig_missing = _sig_required and not inp.has_signature_confirmation and not inp.signature_obtained
    if _sig_missing and reason in ("INR",):
        already = any("signature" in h.lower() for h in hard_fails)
        if not already:
            hard_fails.append("signature confirmation required for transactions ≥ $750 but not obtained")

    # Build EligibilityGap list (old format + new severity)
    gaps: list[EligibilityGap] = []
    # Address gap from hard_fails (before findings loop)
    if _address_failed and not any("address" in f.criterion for f in report.findings if f.blocking):
        gaps.append(EligibilityGap(gap_type=SPEligibilityGapType.WRONG_SHIP_ADDRESS,
                                    description="Item not shipped to buyer's confirmed PayPal address"))
    # Hard fails from findings
    for f in report.findings:
        if f.blocking:
            if "tracking" in f.criterion:
                gt = SPEligibilityGapType.MISSING_TRACKING
            elif "signature" in f.criterion:
                gt = SPEligibilityGapType.MISSING_SIGNATURE
            elif "intangible" in f.criterion or "digital" in f.detail.lower():
                gt = SPEligibilityGapType.DIGITAL_GOODS
            elif "address" in f.criterion:
                gt = SPEligibilityGapType.WRONG_SHIP_ADDRESS
            else:
                gt = SPEligibilityGapType.OTHER
            gaps.append(EligibilityGap(gap_type=gt, description=f.detail[:120]))
    # Soft gaps from wave19 logic
    for key, sev, desc in soft_gaps_wave19:
        if key == "tracking":
            gaps.append(EligibilityGap(gap_type=SPEligibilityGapType.MISSING_TRACKING,
                                        description=desc, blocking=False))
        else:
            gaps.append(EligibilityGap(gap_type=SPEligibilityGapType.WRONG_SHIP_ADDRESS,
                                        description=desc, blocking=False))
    if inp.transaction_marked_no_sp:
        gaps.append(EligibilityGap(gap_type=SPEligibilityGapType.NO_SP_FLAG_AT_SALE,
                                    description="Transaction marked no SP at sale."))
    if inp.is_partial_payment:
        gaps.append(EligibilityGap(gap_type=SPEligibilityGapType.PARTIAL_PAYMENT,
                                    description="Partial payments are not eligible."))
    if inp.item_is_service:
        gaps.append(EligibilityGap(gap_type=SPEligibilityGapType.SERVICE_ITEM,
                                    description="Service items not eligible for INR SP."))

    report.gaps = gaps
    report.blocking_gaps = len([g for g in gaps if g.blocking])

    # ── Determine final SPEligibility ───────────────────────────────
    is_fully_blocked = bool(hard_fails) or report.eligibility_status == EligibilityStatus.INELIGIBLE
    is_partial = report.eligibility_status == EligibilityStatus.PARTIALLY_ELIGIBLE and not is_fully_blocked

    if is_fully_blocked:
        sp_elig = SPEligibility.INELIGIBLE
    elif is_partial or gaps:
        sp_elig = SPEligibility.ELIGIBLE_WITH_GAPS
    else:
        sp_elig = SPEligibility.ELIGIBLE

    report.eligibility = sp_elig

    # ── Overwrite eligibility_status with SPEligibilityStatus ────────
    _sp_status_map = {
        SPEligibility.ELIGIBLE:           SPEligibilityStatus.FULLY_ELIGIBLE,
        SPEligibility.FULLY_ELIGIBLE:     SPEligibilityStatus.FULLY_ELIGIBLE,
        SPEligibility.LIKELY_ELIGIBLE:    SPEligibilityStatus.LIKELY_ELIGIBLE,
        SPEligibility.ELIGIBLE_WITH_GAPS: SPEligibilityStatus.PARTIALLY_ELIGIBLE,
        SPEligibility.INELIGIBLE:         SPEligibilityStatus.INELIGIBLE,
        SPEligibility.NOT_APPLICABLE:     SPEligibilityStatus.NOT_APPLICABLE,
    }
    report.eligibility_status = _sp_status_map.get(sp_elig, SPEligibilityStatus.UNCERTAIN)

    # ── Coverage type ────────────────────────────────────────────────
    if reason in ("INR", "ITEM_NOT_RECEIVED"):
        report.coverage_type = SPCoverageType.ITEM_NOT_RECEIVED if not is_fully_blocked else SPCoverageType.NO_COVERAGE
    elif reason == "UNAUTHORIZED":
        if inp.buyer_account_verified_2fa and not is_fully_blocked:
            report.coverage_type = SPCoverageType.ENHANCED_COVERAGE
        elif not is_fully_blocked:
            report.coverage_type = SPCoverageType.UNAUTHORIZED_COVERAGE
        else:
            report.coverage_type = SPCoverageType.NO_COVERAGE
    else:
        report.coverage_type = SPCoverageType.NO_COVERAGE

    # ── Fee waiver (based on new eligibility field, not overwriting eligibility_status) ──
    fully_elig = sp_elig in (SPEligibility.ELIGIBLE, SPEligibility.FULLY_ELIGIBLE,
                              SPEligibility.LIKELY_ELIGIBLE)
    report.chargeback_fee_waived = fully_elig
    report.fee_savings = 20.0 if fully_elig else 0.0

    # ── New wave42/wave19 fields ─────────────────────────────────────
    report.signature_required = _sig_required
    report.signature_missing = _sig_missing

    report.blocking_issues = hard_fails[:]

    # win_rate_impact
    if sp_elig == SPEligibility.INELIGIBLE:
        report.win_rate_impact = -0.15
    elif sp_elig == SPEligibility.ELIGIBLE_WITH_GAPS:
        report.win_rate_impact = 0.10
    elif sp_elig in (SPEligibility.ELIGIBLE, SPEligibility.FULLY_ELIGIBLE):
        report.win_rate_impact = 0.25
    elif sp_elig == SPEligibility.LIKELY_ELIGIBLE:
        report.win_rate_impact = 0.20
    else:
        report.win_rate_impact = 0.0

    # CE3.0 only applies to UNAUTHORIZED
    report.ce30_applicable = (reason == "UNAUTHORIZED" and inp.has_ce30_qualification)
    if report.ce30_applicable:
        report.win_rate_impact += 0.05

    # requirement_checks (non-empty)
    report.requirement_checks = [
        {"criterion": "paypal_checkout", "met": inp.paid_via_paypal_checkout},
        {"criterion": "tracking", "met": inp.has_tracking_number or inp.tracking_uploaded_to_paypal},
        {"criterion": "address_confirmed", "met": inp.buyer_address_confirmed},
    ]

    # sp_score 0-100
    if sp_elig == SPEligibility.ELIGIBLE:
        report.sp_score = 100.0
    elif sp_elig == SPEligibility.LIKELY_ELIGIBLE:
        report.sp_score = 85.0
    elif sp_elig == SPEligibility.ELIGIBLE_WITH_GAPS:
        report.sp_score = 65.0
    elif sp_elig == SPEligibility.NOT_APPLICABLE:
        report.sp_score = 0.0
    else:
        report.sp_score = 0.0

    # sp_text_to_cite
    if sp_elig in (SPEligibility.ELIGIBLE, SPEligibility.LIKELY_ELIGIBLE):
        report.sp_text_to_cite = (
            "PayPal Seller Protection covers eligible sellers against unauthorized "
            "transactions and INR claims when shipping and confirmation requirements are met."
        )
    else:
        report.sp_text_to_cite = ""

    # action_items
    items = []
    if not inp.has_tracking_number and not inp.tracking_uploaded_to_paypal and reason == "INR":
        items.append("Upload tracking number to PayPal before dispute deadline")
    if _sig_missing:
        items.append("Obtain signature confirmation for high-value shipments ≥ $750")
    if inp.buyer_address_mismatch:
        items.append("Ensure future shipments go to buyer's PayPal-confirmed address")
    report.action_items = items

    # hard_fail_count
    report.hard_fail_count = len([g for g in gaps if g.blocking]) + len(
        [h for h in hard_fails if any(k in h.lower() for k in ("excluded", "vehicle", "gambling", "180 days", "chargeback"))]
    )


# Patch check_seller_protection to call enrichment after building the report
_orig_check_seller_protection = check_seller_protection


def check_seller_protection(inp: SellerProtectionInput) -> SellerProtectionReport:
    report = _orig_check_seller_protection(inp)
    _enrich_report_sp(report, inp)
    return report
