"""
Seller Protection Advisor — pre-order eligibility check for PayPal Seller Protection.

PayPal Seller Protection (SP) shields merchants from chargebacks and disputes
on eligible transactions. NOT checking eligibility BEFORE shipping is one of
the most common and costly mistakes: a merchant fights a dispute thinking SP
covers them, only to discover post-dispute that SP was ineligible all along.

This module answers at order-time: "If this order results in an INR or SNAD
dispute, will Seller Protection apply?"

PayPal Seller Protection Rules (as of 2024):
  Covered claim types: Unauthorized Transaction, Item Not Received (INR)
  NOT covered: Significantly Not As Described (SNAD)

  To be eligible for FULL protection, ALL of the following must be true:
    1. Shipped to the address on the PayPal transaction (no rerouting)
    2. Order fulfillment is documented (tracking number)
    3. Tangible item — NOT digital goods, services, vehicles, real estate,
       custom-built items, or items that violate PayPal AUP
    4. Single payment — not partial payment or installments
    5. PayPal payment confirmed (not eCheck pending)
    6. For orders ≥ $750: signature confirmation required
    7. Buyer not on SP ineligible list (previous abuse claims)

  PARTIAL protection exists for:
    - INR claims where delivery is confirmed but no signature (< $750)
    - Payments made through third-party checkout (not PayPal direct)

  NO protection:
    - SNAD claims (always excluded from SP)
    - Virtual/digital goods
    - Services rendered
    - Custom-built or made-to-order items
    - Intangible items (software licenses, in-game items)

Output:
  - sp_eligible: True / False
  - protection_level: FULL / PARTIAL / NONE
  - missing_requirements: what's needed to get to FULL
  - risk_flags: specific risks not covered
  - recommendation: what to do at shipping time
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ProtectionLevel(str, Enum):
    FULL    = "FULL"     # All requirements met
    PARTIAL = "PARTIAL"  # Some requirements met; limited coverage
    NONE    = "NONE"     # Not eligible for Seller Protection


class ItemCategory(str, Enum):
    TANGIBLE_GOODS       = "TANGIBLE_GOODS"       # Fully eligible
    DIGITAL_GOODS        = "DIGITAL_GOODS"         # NOT eligible
    SERVICES             = "SERVICES"              # NOT eligible
    VEHICLES             = "VEHICLES"              # NOT eligible
    REAL_ESTATE          = "REAL_ESTATE"           # NOT eligible
    CUSTOM_BUILT         = "CUSTOM_BUILT"          # NOT eligible
    GIFT_CARD            = "GIFT_CARD"             # NOT eligible
    CRYPTOCURRENCY       = "CRYPTOCURRENCY"        # NOT eligible
    IN_GAME_ITEMS        = "IN_GAME_ITEMS"         # NOT eligible
    SOFTWARE_LICENSE     = "SOFTWARE_LICENSE"      # NOT eligible
    EVENT_TICKETS        = "EVENT_TICKETS"         # Borderline — no physical delivery
    GENERAL              = "GENERAL"               # Assumed tangible


# Categories where SP does NOT apply regardless of other conditions
_INELIGIBLE_CATEGORIES = {
    ItemCategory.DIGITAL_GOODS,
    ItemCategory.SERVICES,
    ItemCategory.VEHICLES,
    ItemCategory.REAL_ESTATE,
    ItemCategory.CUSTOM_BUILT,
    ItemCategory.GIFT_CARD,
    ItemCategory.CRYPTOCURRENCY,
    ItemCategory.IN_GAME_ITEMS,
    ItemCategory.SOFTWARE_LICENSE,
}

# Categories where SP is borderline / case-by-case
_BORDERLINE_CATEGORIES = {
    ItemCategory.EVENT_TICKETS,
}


@dataclass
class SPAdvisoryInput:
    """
    State of an order at placement / fulfillment time.
    All inputs should be known before the item ships.
    """
    order_id: str
    order_amount: float

    # Payment signals
    payment_confirmed: bool = True        # PayPal payment cleared (not eCheck)
    payment_method: str = "PAYPAL_DIRECT" # PAYPAL_DIRECT / THIRD_PARTY_CHECKOUT / ECHEQUE
    is_partial_payment: bool = False      # Installments or split payment

    # Item signals
    item_category: ItemCategory = ItemCategory.TANGIBLE_GOODS
    is_custom_built: bool = False         # Made-to-order
    is_digital_delivery: bool = False     # Even if physical item, digital delivery

    # Shipping / address signals
    has_tracking_number: bool = True      # Tracking number obtained
    shipped_to_paypal_address: bool = True  # Address matches PayPal transaction
    has_signature_confirmation: bool = False  # Signature obtained on delivery
    carrier_provides_tracking: bool = True   # Carrier has online tracking
    delivery_confirmed: bool = False         # Delivery confirmed by carrier

    # Buyer signals
    buyer_on_ineligible_list: bool = False   # Previously abused SP claims
    buyer_flagged_multiple_claims: bool = False

    # Claim type expected risk (advisory, not enforced)
    expect_inr_risk: bool = True         # Is INR (Item Not Received) a risk?
    expect_snad_risk: bool = False       # Is SNAD (Not As Described) a risk?


@dataclass
class SPRequirement:
    """One SP eligibility requirement and whether it is met."""
    name: str
    met: bool
    critical: bool      # If not met, drops to NONE (not just PARTIAL)
    description: str
    remediation: Optional[str] = None


@dataclass
class SPAdvisoryReport:
    """
    Seller Protection eligibility assessment for one order.
    Generated at order-time, before shipping.
    """
    order_id: str
    order_amount: float
    protection_level: ProtectionLevel
    sp_eligible: bool   # True for FULL or PARTIAL

    requirements: list[SPRequirement]
    missing_requirements: list[str]      # What's needed for FULL
    risk_flags: list[str]               # Specific uncovered risks

    recommendation: str                  # What to do at shipping time
    shipping_checklist: list[str]        # Concrete actions before/at shipping
    snad_note: str                       # Reminder: SNAD is always excluded

    summary: str


def _check_requirements(inp: SPAdvisoryInput) -> list[SPRequirement]:
    reqs: list[SPRequirement] = []

    # 1. Item category eligibility
    category_ok = (
        inp.item_category not in _INELIGIBLE_CATEGORIES
        and not inp.is_custom_built
        and not inp.is_digital_delivery
    )
    category_desc = f"Item category {inp.item_category.value}"
    if inp.item_category in _INELIGIBLE_CATEGORIES:
        category_desc += " — EXCLUDED from Seller Protection"
        cat_rem = None
    elif inp.is_custom_built:
        category_desc += " (custom-built items excluded)"
        cat_rem = "Consider listing as a standard catalog item if possible"
    elif inp.is_digital_delivery:
        category_desc += " (digital delivery excluded)"
        cat_rem = "Ship physical proof or use an SP-eligible delivery method"
    else:
        cat_rem = None
    reqs.append(SPRequirement(
        "Item eligibility", category_ok, critical=True,
        description=category_desc, remediation=cat_rem,
    ))

    # 2. Payment confirmed (not eCheck)
    payment_ok = inp.payment_confirmed and inp.payment_method != "ECHEQUE"
    reqs.append(SPRequirement(
        "Payment confirmed", payment_ok, critical=True,
        description="PayPal payment cleared (not pending eCheck)",
        remediation="Wait for eCheck to clear before shipping" if not payment_ok else None,
    ))

    # 3. Not partial/installment payment
    not_partial = not inp.is_partial_payment
    reqs.append(SPRequirement(
        "Single payment", not_partial, critical=True,
        description="Full single payment (not installments or split)",
        remediation="Collect full payment in one PayPal transaction before shipping",
    ))

    # 4. Shipped to PayPal address
    address_ok = inp.shipped_to_paypal_address
    reqs.append(SPRequirement(
        "Shipped to PayPal address", address_ok, critical=True,
        description="Item shipped to address on PayPal transaction",
        remediation="Always ship to the exact address shown in the PayPal payment — never reroute",
    ))

    # 5. Tracking number
    tracking_ok = inp.has_tracking_number and inp.carrier_provides_tracking
    reqs.append(SPRequirement(
        "Tracking number", tracking_ok, critical=False,
        description="Shipment has an online-trackable tracking number",
        remediation="Use a carrier with online tracking (USPS, FedEx, UPS, DHL)",
    ))

    # 6. Signature confirmation for orders ≥ $750
    sig_required = inp.order_amount >= 750.0
    if sig_required:
        sig_ok = inp.has_signature_confirmation
    else:
        sig_ok = True  # Not required for < $750
    reqs.append(SPRequirement(
        "Signature confirmation",
        sig_ok,
        critical=False,
        description=(
            f"Signature required for orders ≥ $750 (this order: ${inp.order_amount:.0f})"
            if sig_required else
            f"Signature not required for orders < $750 (this order: ${inp.order_amount:.0f})"
        ),
        remediation="Request signature service from carrier at shipping time" if sig_required and not sig_ok else None,
    ))

    # 7. Buyer not on ineligible list
    buyer_ok = not inp.buyer_on_ineligible_list and not inp.buyer_flagged_multiple_claims
    reqs.append(SPRequirement(
        "Buyer not flagged",
        buyer_ok,
        critical=True,
        description="Buyer not on PayPal Seller Protection ineligible list",
        remediation="Consider refusing or refunding orders from flagged buyers — SP will not cover",
    ))

    return reqs


def _build_risk_flags(inp: SPAdvisoryInput, reqs: list[SPRequirement]) -> list[str]:
    flags: list[str] = []

    if inp.expect_snad_risk:
        flags.append("SNAD_NEVER_COVERED: Significantly Not As Described claims are NEVER covered by Seller Protection")

    if inp.item_category in _BORDERLINE_CATEGORIES:
        flags.append(f"BORDERLINE_CATEGORY: {inp.item_category.value} — PayPal reviews SP eligibility case-by-case")

    if inp.payment_method == "THIRD_PARTY_CHECKOUT":
        flags.append("THIRD_PARTY_CHECKOUT: Payments via third-party checkout may receive only partial protection")

    for r in reqs:
        if not r.met and r.critical:
            flags.append(f"CRITICAL_GAP: {r.name} not met — SP denied entirely")

    return flags


def _build_recommendation(
    level: ProtectionLevel,
    inp: SPAdvisoryInput,
    reqs: list[SPRequirement],
) -> str:
    if level == ProtectionLevel.NONE:
        ineligible_reason = next((r.name for r in reqs if not r.met and r.critical), "ineligible item or payment")
        return (
            f"Seller Protection does NOT apply to this order ({ineligible_reason}). "
            "Before shipping: weigh the dispute risk carefully — you have no SP safety net. "
            "Consider requiring payment by another method or refusing the order."
        )
    elif level == ProtectionLevel.PARTIAL:
        gaps = [r.name for r in reqs if not r.met and not r.critical]
        return (
            f"Seller Protection applies PARTIALLY. Missing for full coverage: {', '.join(gaps)}. "
            "Ship with tracking and retain all evidence. SNAD claims remain uncovered."
        )
    else:
        if inp.order_amount >= 750:
            return (
                "Seller Protection: FULL coverage available. "
                "REQUIRED at shipment: signature confirmation (order ≥ $750). "
                "Retain tracking and delivery confirmation. "
                "Note: SNAD claims are NEVER covered."
            )
        return (
            "Seller Protection: FULL coverage available. "
            "Ship with online tracking to the PayPal address. "
            "Retain delivery confirmation. "
            "Note: SNAD claims are NEVER covered."
        )


def _build_checklist(inp: SPAdvisoryInput, level: ProtectionLevel) -> list[str]:
    checklist: list[str] = []
    if level == ProtectionLevel.NONE:
        checklist.append("⚠️ SP does not apply — ship at your own risk or decline the order")
        return checklist

    checklist.append("✓ Confirm PayPal payment has cleared before packing")
    checklist.append("✓ Ship ONLY to the address shown on the PayPal transaction")
    checklist.append("✓ Use a carrier with online tracking (USPS, FedEx, UPS, DHL)")
    checklist.append("✓ Save the tracking number — you will need it for any dispute")

    if inp.order_amount >= 750:
        checklist.append(f"✓ REQUEST SIGNATURE CONFIRMATION — order is ${inp.order_amount:.0f} (≥ $750 threshold)")

    checklist.append("✓ Save all order documentation: description, photos, messages with buyer")
    checklist.append("✓ Remember: SNAD claims are never covered by SP regardless of evidence")

    return checklist


def assess_seller_protection(inp: SPAdvisoryInput) -> SPAdvisoryReport:
    """
    Determine Seller Protection eligibility for an order at fulfillment time.

    Call this before shipping, not after a dispute is filed.
    """
    reqs = _check_requirements(inp)

    # Critical failures → NONE
    critical_failures = [r for r in reqs if not r.met and r.critical]
    non_critical_failures = [r for r in reqs if not r.met and not r.critical]

    if critical_failures:
        level = ProtectionLevel.NONE
    elif non_critical_failures:
        level = ProtectionLevel.PARTIAL
    else:
        level = ProtectionLevel.FULL

    sp_eligible = level != ProtectionLevel.NONE

    missing = [r.name for r in reqs if not r.met]
    risk_flags = _build_risk_flags(inp, reqs)
    recommendation = _build_recommendation(level, inp, reqs)
    checklist = _build_checklist(inp, level)

    snad_note = (
        "SNAD (Significantly Not As Described) claims are ALWAYS excluded from PayPal Seller Protection, "
        "regardless of how well the transaction meets all other eligibility criteria. "
        "To defend SNAD disputes, you must rely on evidence quality, not SP coverage."
    )

    summary = (
        f"Order {inp.order_id} — ${inp.order_amount:.0f} — "
        f"SP: {level.value} | "
        f"Category: {inp.item_category.value} | "
        f"Missing: {', '.join(missing) if missing else 'none'} | "
        f"Flags: {len(risk_flags)}"
    )

    return SPAdvisoryReport(
        order_id=inp.order_id,
        order_amount=inp.order_amount,
        protection_level=level,
        sp_eligible=sp_eligible,
        requirements=reqs,
        missing_requirements=missing,
        risk_flags=risk_flags,
        recommendation=recommendation,
        shipping_checklist=checklist,
        snad_note=snad_note,
        summary=summary,
    )


def batch_sp_check(orders: list[SPAdvisoryInput]) -> list[SPAdvisoryReport]:
    """Check SP eligibility for a batch of orders, sorted by risk (NONE first)."""
    reports = [assess_seller_protection(o) for o in orders]
    level_order = {ProtectionLevel.NONE: 0, ProtectionLevel.PARTIAL: 1, ProtectionLevel.FULL: 2}
    reports.sort(key=lambda r: level_order[r.protection_level])
    return reports
