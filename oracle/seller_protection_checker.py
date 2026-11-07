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
    reason_code: str                      # UNAUTHORIZED / INR / SNAD / SUBSCRIPTION
    transaction_amount: float

    # Fulfillment data
    fulfillment_method: FulfillmentMethod = FulfillmentMethod.UNKNOWN
    has_tracking_number: bool = False
    tracking_carrier: Optional[str] = None  # "USPS" / "UPS" / "FedEx" / etc.
    delivery_confirmed: bool = False         # carrier confirmed delivery
    signature_obtained: bool = False         # sig. confirmation for $750+

    # Payment data
    paid_via_paypal_checkout: bool = True    # was PayPal the payment method?
    buyer_address_confirmed: bool = True     # PayPal address verified
    intangible_goods: bool = False           # digital goods, services, real estate

    # Transaction metadata
    transaction_amount_usd: float = 0.0     # for signature threshold check
    is_donation: bool = False
    is_gambling: bool = False
    is_money_transfer: bool = False         # peer-to-peer (Friends & Family)
    is_vehicle_purchase: bool = False


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
    )
