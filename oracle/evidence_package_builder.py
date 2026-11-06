"""
Evidence Package Builder — assemble and score the complete evidence
package for a dispute response, checking completeness, strength, and
network-specific requirements.

For each dispute the builder:
  1. Inventories what evidence is present
  2. Checks required fields per reason code
  3. Scores completeness (0–100)
  4. Rates overall evidence strength
  5. Lists gaps with remediation advice

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvidenceStrength(str, Enum):
    COMPELLING  = "COMPELLING"   # Will almost certainly win
    STRONG      = "STRONG"       # Good case — above average win odds
    ADEQUATE    = "ADEQUATE"     # Meets minimum threshold
    WEAK        = "WEAK"         # Missing key pieces
    INSUFFICIENT = "INSUFFICIENT" # Cannot file a credible response


class EvidenceCategory(str, Enum):
    FULFILLMENT      = "FULFILLMENT"      # Tracking, delivery confirmation
    IDENTITY         = "IDENTITY"         # Buyer ID, IP logs, device fingerprint
    PRODUCT          = "PRODUCT"          # Description, photos, digital delivery
    POLICY           = "POLICY"           # Refund policy, TOS, subscription terms
    COMMUNICATION    = "COMMUNICATION"    # Emails, chat logs with buyer
    AUTHORIZATION    = "AUTHORIZATION"    # 3DS, CVV, AVS match
    FINANCIAL        = "FINANCIAL"        # Invoice, receipt, partial refund proof


@dataclass
class EvidenceItem:
    """A single piece of evidence in the package."""
    item_id: str
    category: EvidenceCategory
    description: str
    is_present: bool
    is_required: bool       # Required by network for this reason code
    quality_score: float    # 0–1 (how strong this piece is)
    notes: str = ""


@dataclass
class EvidencePackageInput:
    """Input describing what evidence is available."""
    dispute_id: str
    reason_code: str            # INR / SNAD / UNAUTHORIZED / 10.4 / 13.1 etc.
    network: str                # PAYPAL / VISA / MASTERCARD / AMEX / DISCOVER
    transaction_amount: float

    # Fulfillment evidence
    has_tracking_number: bool = False
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    has_shipment_date_proof: bool = False
    tracking_shows_delivered: bool = False

    # Identity / authorization evidence
    has_ip_log: bool = False
    has_device_fingerprint: bool = False
    has_order_history: bool = False
    has_3ds_authentication: bool = False
    has_avs_match: bool = False
    has_cvv_match: bool = False

    # Product / service evidence
    has_product_listing: bool = False
    has_product_photos: bool = False
    has_digital_delivery_proof: bool = False
    product_matches_listing: bool = True

    # Policy evidence
    has_refund_policy_at_checkout: bool = False
    has_tos_acceptance: bool = False
    has_cancellation_policy: bool = False
    has_subscription_terms: bool = False

    # Communication evidence
    has_buyer_communication: bool = False
    communication_supports_merchant: bool = False

    # Financial evidence
    has_invoice: bool = False
    has_partial_refund_proof: bool = False


# Required evidence per reason code
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "INR":           ["has_tracking_number", "has_shipment_date_proof"],
    "SNAD":          ["has_product_listing", "has_refund_policy_at_checkout"],
    "UNAUTHORIZED":  ["has_ip_log", "has_order_history"],
    "SUBSCRIPTION":  ["has_subscription_terms", "has_cancellation_policy"],
    "10.4":          ["has_ip_log", "has_device_fingerprint", "has_order_history"],
    "13.1":          ["has_tracking_number", "has_delivery_confirmation"],
    "13.3":          ["has_product_listing", "has_product_photos"],
    "12.6":          ["has_invoice"],
    "12.7":          ["has_invoice"],
    "4853":          ["has_product_listing", "has_delivery_confirmation"],
    "4863":          ["has_ip_log", "has_order_history"],
    "4834":          ["has_invoice"],
    "4807":          [],
    "C02":           ["has_partial_refund_proof"],
    "C08":           ["has_tracking_number", "has_delivery_confirmation"],
    "C14":           ["has_cancellation_policy", "has_subscription_terms"],
    "C31":           ["has_product_listing"],
    "FR2":           ["has_ip_log", "has_device_fingerprint"],
    "FR6":           ["has_partial_refund_proof"],
    "AP":            ["has_3ds_authentication"],
    "NF":            ["has_tracking_number"],
    "RG":            ["has_delivery_confirmation"],
    "RM":            ["has_product_listing"],
}

# Bonus fields — not required but add strength
_BONUS_FIELDS: dict[str, list[str]] = {
    "INR":          ["has_delivery_confirmation", "has_signature_confirmation",
                     "has_buyer_communication"],
    "SNAD":         ["has_product_photos", "has_buyer_communication",
                     "has_tos_acceptance"],
    "UNAUTHORIZED": ["has_3ds_authentication", "has_avs_match", "has_cvv_match"],
    "10.4":         ["has_3ds_authentication", "has_avs_match"],
    "13.1":         ["has_signature_confirmation", "has_buyer_communication"],
    "13.3":         ["has_buyer_communication", "has_refund_policy_at_checkout"],
}


def _field_to_category(fname: str) -> EvidenceCategory:
    if "tracking" in fname or "delivery" in fname or "signature" in fname or "shipment" in fname:
        return EvidenceCategory.FULFILLMENT
    if "ip_log" in fname or "device" in fname or "order_history" in fname:
        return EvidenceCategory.IDENTITY
    if "product" in fname or "digital" in fname:
        return EvidenceCategory.PRODUCT
    if "refund_policy" in fname or "tos" in fname or "cancel" in fname or "subscription" in fname:
        return EvidenceCategory.POLICY
    if "communication" in fname or "buyer_comm" in fname:
        return EvidenceCategory.COMMUNICATION
    if "3ds" in fname or "avs" in fname or "cvv" in fname:
        return EvidenceCategory.AUTHORIZATION
    if "invoice" in fname or "partial_refund" in fname:
        return EvidenceCategory.FINANCIAL
    return EvidenceCategory.PRODUCT


@dataclass
class EvidenceGap:
    field_name: str
    category: EvidenceCategory
    is_blocking: bool   # Required and missing → can't file
    remediation: str


@dataclass
class EvidencePackageReport:
    """Complete evidence package assessment."""
    dispute_id: str
    reason_code: str
    network: str

    items: list[EvidenceItem]
    gaps: list[EvidenceGap]
    missing_required: list[str]

    completeness_score: float    # 0–100 (required + bonus)
    evidence_strength: EvidenceStrength
    can_file: bool               # All required fields present
    win_rate_boost: float        # Additive modifier from evidence quality

    recommendations: list[str]
    summary: str


def build_evidence_package(inp: EvidencePackageInput) -> EvidencePackageReport:
    """Assess the evidence package for a dispute."""
    required = _REQUIRED_FIELDS.get(inp.reason_code, [])
    bonus    = _BONUS_FIELDS.get(inp.reason_code, [])
    all_relevant = list(dict.fromkeys(required + bonus))

    items: list[EvidenceItem] = []
    gaps: list[EvidenceGap] = []
    missing_required: list[str] = []

    required_present = 0
    bonus_present = 0

    inp_dict = inp.__dict__

    for fname in all_relevant:
        is_req = fname in required
        present = bool(inp_dict.get(fname, False))
        quality = 0.8 if present else 0.0
        # Upgrade quality for extra strong evidence
        if fname == "has_delivery_confirmation" and inp.tracking_shows_delivered:
            quality = 1.0
        if fname == "has_signature_confirmation" and present:
            quality = 1.0

        items.append(EvidenceItem(
            item_id=fname,
            category=_field_to_category(fname),
            description=fname.replace("has_", "").replace("_", " ").title(),
            is_present=present,
            is_required=is_req,
            quality_score=quality,
        ))

        if is_req:
            if present:
                required_present += 1
            else:
                missing_required.append(fname)
                gaps.append(EvidenceGap(
                    field_name=fname,
                    category=_field_to_category(fname),
                    is_blocking=True,
                    remediation=f"Obtain {fname.replace('has_', '').replace('_', ' ')} before filing",
                ))
        elif present:
            bonus_present += 1

    # Completeness score
    req_weight = 0.70
    bon_weight = 0.30
    req_score = (required_present / len(required) * 100) if required else 100.0
    bon_score = (bonus_present / len(bonus) * 100) if bonus else 100.0
    completeness = req_score * req_weight + bon_score * bon_weight

    can_file = len(missing_required) == 0

    # Signature for high-value
    if inp.transaction_amount >= 750 and not inp.has_signature_confirmation:
        gaps.append(EvidenceGap(
            field_name="has_signature_confirmation",
            category=EvidenceCategory.FULFILLMENT,
            is_blocking=False,
            remediation="Obtain signature confirmation — required for transactions ≥$750",
        ))

    # Communication bonus
    if inp.has_buyer_communication and inp.communication_supports_merchant:
        completeness = min(100.0, completeness + 5.0)

    # Strength determination
    if not can_file:
        strength = EvidenceStrength.INSUFFICIENT
    elif completeness >= 85 and req_score == 100:
        strength = EvidenceStrength.COMPELLING
    elif completeness >= 70:
        strength = EvidenceStrength.STRONG
    elif completeness >= 50:
        strength = EvidenceStrength.ADEQUATE
    else:
        strength = EvidenceStrength.WEAK

    # Win rate boost
    boost_map = {
        EvidenceStrength.COMPELLING:   0.18,
        EvidenceStrength.STRONG:       0.10,
        EvidenceStrength.ADEQUATE:     0.02,
        EvidenceStrength.WEAK:        -0.08,
        EvidenceStrength.INSUFFICIENT:-0.20,
    }
    win_boost = boost_map[strength]

    # Recommendations
    recs: list[str] = []
    if missing_required:
        recs.append(f"Blocking gap: obtain {', '.join(missing_required[:3])}")
    if inp.transaction_amount >= 750 and not inp.has_signature_confirmation:
        recs.append("Collect signature confirmation for high-value transaction")
    if not inp.has_buyer_communication:
        recs.append("Include buyer communication history to corroborate dispute")
    if inp.reason_code in ("UNAUTHORIZED", "10.4") and not inp.has_3ds_authentication:
        recs.append("Obtain 3DS/SCA authentication record if available")

    summary = (
        f"Evidence {inp.dispute_id}: "
        f"can_file={can_file} | strength={strength.value} | "
        f"completeness={completeness:.0f}% | gaps={len(gaps)}"
    )

    return EvidencePackageReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        network=inp.network,
        items=items,
        gaps=gaps,
        missing_required=missing_required,
        completeness_score=round(completeness, 1),
        evidence_strength=strength,
        can_file=can_file,
        win_rate_boost=win_boost,
        recommendations=recs,
        summary=summary,
    )
