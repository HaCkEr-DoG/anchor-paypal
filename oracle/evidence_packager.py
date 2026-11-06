"""
Evidence Packager — assembles the optimal PayPal evidence submission package.

Every oracle module decides WHETHER to fight. This module decides HOW.

The difference between winning and losing is often not whether you have
evidence, but whether you present it in PayPal's preferred format, in the
right order, with the right framing for the claim type.

What PayPal reviewers (and their AI) look for by claim type:
  - INR: Tracking number → delivery confirmation → buyer's confirmed address
  - SNAD: Clear description of what was described vs what was received
  - UNAUTHORIZED: IP/device match → transaction authorization → account history

What kills otherwise-winnable disputes:
  1. Submitting evidence WITHOUT a narrative — raw tracking numbers don't win
  2. SNAD responses that just say "item was as described" with no proof
  3. Missing signature confirmation for high-value orders
  4. Blaming the carrier without documenting delivery chain custody

This module:
  1. Takes available evidence items
  2. Selects the optimal subset for the claim type
  3. Writes the narrative connecting them
  4. Specifies exact field values for PayPal's dispute form
  5. Flags missing evidence that would materially improve win probability
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvidenceType(str, Enum):
    TRACKING_NUMBER           = "TRACKING_NUMBER"
    DELIVERY_CONFIRMATION     = "DELIVERY_CONFIRMATION"
    SIGNATURE_CONFIRMATION    = "SIGNATURE_CONFIRMATION"
    BUYER_ADDRESS_CONFIRMATION = "BUYER_ADDRESS_CONFIRMATION"
    ITEM_DESCRIPTION          = "ITEM_DESCRIPTION"
    ITEM_PHOTOS               = "ITEM_PHOTOS"
    PROOF_OF_AUTHENTICITY     = "PROOF_OF_AUTHENTICITY"
    COMMUNICATION_HISTORY     = "COMMUNICATION_HISTORY"
    IP_ADDRESS_MATCH          = "IP_ADDRESS_MATCH"
    ACCOUNT_LOGIN_HISTORY     = "ACCOUNT_LOGIN_HISTORY"
    INVOICE_RECEIPT           = "INVOICE_RECEIPT"
    SHIPPING_LABEL            = "SHIPPING_LABEL"
    CUSTOMS_DOCUMENTATION     = "CUSTOMS_DOCUMENTATION"
    RETURN_MERCHANDISE_AUTH   = "RETURN_MERCHANDISE_AUTH"
    CARRIER_INVESTIGATION     = "CARRIER_INVESTIGATION"
    NETWORK_FRAUD_DATA        = "NETWORK_FRAUD_DATA"


@dataclass
class EvidenceItem:
    """One piece of available evidence."""
    evidence_type: EvidenceType
    value: str                   # The actual value (tracking #, URL, description, etc.)
    document_name: Optional[str] = None  # File name if it's a document
    win_rate_impact: float = 0.0  # How much this evidence improves win rate


@dataclass
class EvidenceField:
    """One field in the PayPal dispute response form."""
    field_name: str
    field_value: str
    char_count: int
    is_required: bool
    paypal_char_limit: Optional[int] = None


@dataclass
class EvidencePackage:
    """
    Complete, submission-ready evidence package for one dispute.
    Maps directly to PayPal's dispute response form fields.
    """
    dispute_id: str
    claim_type: str

    # PayPal form fields (ready to paste)
    merchant_response_statement: str    # Main narrative (2000 chars)
    supplemental_evidence_description: str  # Additional context (1000 chars)
    tracking_information: Optional[str]
    delivery_confirmation: Optional[str]

    # Document checklist
    documents_to_attach: list[str]      # Ordered list of documents to upload
    documents_missing_critical: list[str]  # Critical missing docs
    documents_missing_helpful: list[str]   # Would help but not critical

    # Package quality
    package_strength: str               # "STRONG" / "MODERATE" / "WEAK"
    estimated_win_rate_with_package: float
    missing_evidence_win_rate_boost: float  # Potential gain from gathering missing docs

    # Field values
    form_fields: list[EvidenceField]

    reasoning: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# INR-specific evidence weights
_INR_EVIDENCE_WEIGHTS = {
    EvidenceType.DELIVERY_CONFIRMATION: 0.18,
    EvidenceType.TRACKING_NUMBER:       0.12,
    EvidenceType.SIGNATURE_CONFIRMATION: 0.15,
    EvidenceType.BUYER_ADDRESS_CONFIRMATION: 0.08,
    EvidenceType.SHIPPING_LABEL:        0.05,
    EvidenceType.COMMUNICATION_HISTORY: 0.06,
    EvidenceType.NETWORK_FRAUD_DATA:    0.08,
}

# SNAD-specific evidence weights
_SNAD_EVIDENCE_WEIGHTS = {
    EvidenceType.ITEM_DESCRIPTION:      0.15,
    EvidenceType.ITEM_PHOTOS:           0.18,
    EvidenceType.PROOF_OF_AUTHENTICITY: 0.14,
    EvidenceType.COMMUNICATION_HISTORY: 0.10,
    EvidenceType.RETURN_MERCHANDISE_AUTH: 0.08,
    EvidenceType.INVOICE_RECEIPT:       0.06,
}

# UNAUTHORIZED evidence weights
_UNAUTHORIZED_EVIDENCE_WEIGHTS = {
    EvidenceType.IP_ADDRESS_MATCH:     0.20,
    EvidenceType.ACCOUNT_LOGIN_HISTORY: 0.18,
    EvidenceType.DELIVERY_CONFIRMATION: 0.12,
    EvidenceType.TRACKING_NUMBER:       0.08,
    EvidenceType.NETWORK_FRAUD_DATA:    0.10,
}


def build_evidence_package(
    dispute_id: str,
    claim_type: str,
    dispute_amount: float,
    available_evidence: list[EvidenceItem],
    buyer_name: str = "the buyer",
    item_name: str = "the item",
    order_date: Optional[str] = None,
    base_win_rate: float = 0.65,
) -> EvidencePackage:
    """
    Assemble optimal PayPal evidence submission package.

    Takes available evidence items and builds a submission-ready package
    with narrative text, form field values, and document checklist.
    """
    reasoning: list[str] = []
    warnings: list[str] = []

    # Select evidence weights for this claim type
    if claim_type == "ITEM_NOT_RECEIVED":
        weights = _INR_EVIDENCE_WEIGHTS
    elif claim_type == "SIGNIFICANTLY_NOT_AS_DESCRIBED":
        weights = _SNAD_EVIDENCE_WEIGHTS
    elif claim_type == "UNAUTHORIZED":
        weights = _UNAUTHORIZED_EVIDENCE_WEIGHTS
    else:
        weights = _INR_EVIDENCE_WEIGHTS

    # Index available evidence
    evidence_map = {e.evidence_type: e for e in available_evidence}

    # Compute win rate from available evidence
    present_boost = sum(
        weights.get(e.evidence_type, 0.0)
        for e in available_evidence
    )
    missing_boost = sum(
        v for k, v in weights.items()
        if k not in evidence_map
    )

    estimated_win_rate = min(0.95, base_win_rate + present_boost)

    # Determine package strength
    if estimated_win_rate >= 0.80:
        strength = "STRONG"
    elif estimated_win_rate >= 0.65:
        strength = "MODERATE"
    else:
        strength = "WEAK"

    # Build narrative
    if claim_type == "ITEM_NOT_RECEIVED":
        narrative = _build_inr_narrative(
            dispute_id, buyer_name, item_name, order_date, dispute_amount,
            evidence_map, reasoning, warnings,
        )
    elif claim_type == "SIGNIFICANTLY_NOT_AS_DESCRIBED":
        narrative = _build_snad_narrative(
            dispute_id, buyer_name, item_name, order_date, dispute_amount,
            evidence_map, reasoning, warnings,
        )
    elif claim_type == "UNAUTHORIZED":
        narrative = _build_unauthorized_narrative(
            dispute_id, buyer_name, item_name, order_date, dispute_amount,
            evidence_map, reasoning, warnings,
        )
    else:
        narrative = _build_inr_narrative(
            dispute_id, buyer_name, item_name, order_date, dispute_amount,
            evidence_map, reasoning, warnings,
        )

    # Supplemental evidence description
    supplemental = _build_supplemental(claim_type, evidence_map, available_evidence)

    # Build form fields
    form_fields: list[EvidenceField] = [
        EvidenceField(
            field_name="Merchant Response Statement",
            field_value=narrative,
            char_count=len(narrative),
            is_required=True,
            paypal_char_limit=2000,
        ),
        EvidenceField(
            field_name="Supplemental Evidence",
            field_value=supplemental,
            char_count=len(supplemental),
            is_required=False,
            paypal_char_limit=1000,
        ),
    ]

    # Tracking
    tracking = None
    if EvidenceType.TRACKING_NUMBER in evidence_map:
        tracking = evidence_map[EvidenceType.TRACKING_NUMBER].value
        form_fields.append(EvidenceField(
            field_name="Tracking Number",
            field_value=tracking,
            char_count=len(tracking),
            is_required=False,
        ))

    # Delivery confirmation
    delivery_conf = None
    if EvidenceType.DELIVERY_CONFIRMATION in evidence_map:
        delivery_conf = evidence_map[EvidenceType.DELIVERY_CONFIRMATION].value
        form_fields.append(EvidenceField(
            field_name="Proof of Delivery",
            field_value=delivery_conf,
            char_count=len(delivery_conf),
            is_required=False,
        ))

    # Documents to attach
    docs_to_attach = [
        e.document_name for e in available_evidence
        if e.document_name
    ]

    # Critical missing documents
    critical_missing: list[str] = []
    helpful_missing: list[str] = []

    if claim_type == "ITEM_NOT_RECEIVED":
        if EvidenceType.DELIVERY_CONFIRMATION not in evidence_map:
            critical_missing.append("Delivery confirmation screenshot from carrier website")
        if EvidenceType.TRACKING_NUMBER not in evidence_map:
            critical_missing.append("Shipment tracking number and carrier name")
        if EvidenceType.BUYER_ADDRESS_CONFIRMATION not in evidence_map:
            helpful_missing.append("Confirmation that shipping address matches PayPal-confirmed address")
    elif claim_type == "SIGNIFICANTLY_NOT_AS_DESCRIBED":
        if EvidenceType.ITEM_PHOTOS not in evidence_map:
            critical_missing.append("Photos of the item showing it matches original listing")
        if EvidenceType.ITEM_DESCRIPTION not in evidence_map:
            critical_missing.append("Original listing/description that the item was sold against")
    elif claim_type == "UNAUTHORIZED":
        if EvidenceType.IP_ADDRESS_MATCH not in evidence_map:
            critical_missing.append("IP address match between transaction and account owner's known IP")
        if EvidenceType.ACCOUNT_LOGIN_HISTORY not in evidence_map:
            helpful_missing.append("Account login history showing account was active from buyer's device")

    if critical_missing:
        warnings.append(f"MISSING CRITICAL EVIDENCE: {'; '.join(critical_missing)}")

    return EvidencePackage(
        dispute_id=dispute_id,
        claim_type=claim_type,
        merchant_response_statement=narrative,
        supplemental_evidence_description=supplemental,
        tracking_information=tracking,
        delivery_confirmation=delivery_conf,
        documents_to_attach=docs_to_attach,
        documents_missing_critical=critical_missing,
        documents_missing_helpful=helpful_missing,
        package_strength=strength,
        estimated_win_rate_with_package=round(estimated_win_rate, 3),
        missing_evidence_win_rate_boost=round(missing_boost, 3),
        form_fields=form_fields,
        reasoning=reasoning,
        warnings=warnings,
    )


def _build_inr_narrative(
    dispute_id: str,
    buyer_name: str,
    item_name: str,
    order_date: Optional[str],
    amount: float,
    evidence: dict,
    reasoning: list,
    warnings: list,
) -> str:
    tracking = evidence.get(EvidenceType.TRACKING_NUMBER)
    delivery = evidence.get(EvidenceType.DELIVERY_CONFIRMATION)
    signature = evidence.get(EvidenceType.SIGNATURE_CONFIRMATION)
    address = evidence.get(EvidenceType.BUYER_ADDRESS_CONFIRMATION)

    parts: list[str] = []

    # Opening
    order_ref = f"on {order_date}" if order_date else ""
    parts.append(
        f"We are disputing this Item Not Received claim. "
        f"The order for {item_name} (${amount:.2f}) was placed {order_ref} "
        f"and shipped to the buyer's PayPal-confirmed address."
    )

    if tracking:
        parts.append(
            f"The shipment was sent via {tracking.value if hasattr(tracking, 'value') else tracking} "
            f"with full tracking."
        )
        reasoning.append("Tracking number included in narrative")

    if delivery:
        parts.append(
            f"Our records confirm delivery: {delivery.value if hasattr(delivery, 'value') else delivery}. "
            f"The package was delivered to the confirmed address."
        )
        reasoning.append("Delivery confirmation included")

    if signature:
        parts.append(
            f"Delivery required a signature: {signature.value if hasattr(signature, 'value') else signature}. "
            f"This confirms the item was physically received at the destination."
        )
        reasoning.append("Signature confirmation — strongest possible INR evidence")

    if address:
        parts.append(
            f"The delivery address matches the PayPal-confirmed address provided at checkout: "
            f"{address.value if hasattr(address, 'value') else address}."
        )

    parts.append(
        "We have fulfilled our seller obligations under PayPal Seller Protection. "
        "The item was shipped to the buyer's confirmed address with valid tracking. "
        "We respectfully request that this dispute be decided in our favor."
    )

    return " ".join(parts)


def _build_snad_narrative(
    dispute_id: str,
    buyer_name: str,
    item_name: str,
    order_date: Optional[str],
    amount: float,
    evidence: dict,
    reasoning: list,
    warnings: list,
) -> str:
    photos = evidence.get(EvidenceType.ITEM_PHOTOS)
    description = evidence.get(EvidenceType.ITEM_DESCRIPTION)
    authenticity = evidence.get(EvidenceType.PROOF_OF_AUTHENTICITY)
    comms = evidence.get(EvidenceType.COMMUNICATION_HISTORY)

    parts = [
        f"We are disputing this Significantly Not As Described claim for {item_name} (${amount:.2f}). "
        f"The item was accurately described in our listing and matches the description provided at the time of sale."
    ]

    if description:
        desc_val = description.value if hasattr(description, 'value') else description
        parts.append(f"Our original listing stated: {desc_val}. The item sold exactly matched this description.")

    if photos:
        parts.append(
            "We have attached photos of the item confirming it matches the listing. "
            "The item shows no damage, missing parts, or discrepancies from the described condition."
        )
        reasoning.append("Item photos attached — strong SNAD defense")

    if authenticity:
        auth_val = authenticity.value if hasattr(authenticity, 'value') else authenticity
        parts.append(f"Authenticity documentation: {auth_val}. The item is genuine as described.")

    if comms:
        parts.append(
            "Our communication history with the buyer (attached) shows no complaints about item condition "
            "prior to this dispute, which is inconsistent with a genuinely not-as-described item."
        )

    parts.append(
        "SNAD claims require proof that the item materially differs from the listing description. "
        "No such difference exists here. We request this dispute be decided in our favor."
    )
    return " ".join(parts)


def _build_unauthorized_narrative(
    dispute_id: str,
    buyer_name: str,
    item_name: str,
    order_date: Optional[str],
    amount: float,
    evidence: dict,
    reasoning: list,
    warnings: list,
) -> str:
    ip_match = evidence.get(EvidenceType.IP_ADDRESS_MATCH)
    login_history = evidence.get(EvidenceType.ACCOUNT_LOGIN_HISTORY)
    delivery = evidence.get(EvidenceType.DELIVERY_CONFIRMATION)

    parts = [
        f"We are disputing this Unauthorized Transaction claim for {item_name} (${amount:.2f}). "
        f"Transaction records indicate this purchase was authorized by the account holder."
    ]

    if ip_match:
        ip_val = ip_match.value if hasattr(ip_match, 'value') else ip_match
        parts.append(
            f"The transaction IP address matches the account holder's known location: {ip_val}. "
            f"This is consistent with an authorized transaction."
        )

    if login_history:
        lh_val = login_history.value if hasattr(login_history, 'value') else login_history
        parts.append(
            f"Account login history shows regular activity from this device/location: {lh_val}. "
            f"No unusual access patterns were observed around the transaction date."
        )

    if delivery:
        d_val = delivery.value if hasattr(delivery, 'value') else delivery
        parts.append(
            f"Delivery confirmation: {d_val}. The order was shipped to the account's confirmed address. "
            f"An unauthorized user would not have access to redirect delivery."
        )

    parts.append(
        "The transaction evidence is consistent with an authorized purchase by the account holder. "
        "We respectfully request this unauthorized transaction claim be denied."
    )
    return " ".join(parts)


def _build_supplemental(
    claim_type: str,
    evidence: dict,
    all_evidence: list[EvidenceItem],
) -> str:
    items = []
    for ev in all_evidence:
        if ev.document_name:
            items.append(f"[Document: {ev.document_name}]")
        elif ev.evidence_type == EvidenceType.NETWORK_FRAUD_DATA:
            items.append(f"Cross-merchant fraud signal: {ev.value}")
        elif ev.evidence_type == EvidenceType.CARRIER_INVESTIGATION:
            items.append(f"Carrier investigation result: {ev.value}")
    if not items:
        return f"All relevant documentation for this {claim_type} dispute is included above."
    return "Supporting evidence: " + "; ".join(items)
