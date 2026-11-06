"""
Dispute Response Composer — generate a structured dispute response
outline tailored to the reason code, evidence, and network requirements.

The composer produces:
  - Opening statement (factual assertion)
  - Evidence summary (what we're submitting and why)
  - Point-by-point rebuttal of common buyer claims
  - Closing statement
  - Required evidence attachments checklist
  - Response tone guidance

This is a pure oracle module — no I/O, no persistence.
Actual prose templates are concise and professional but not legally reviewed;
merchants should adapt them before filing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResponseTone(str, Enum):
    ASSERTIVE    = "ASSERTIVE"    # Strong evidence, clear win
    PROFESSIONAL = "PROFESSIONAL" # Standard tone
    CONCILIATORY = "CONCILIATORY" # Borderline — acknowledge buyer concern
    FACTUAL      = "FACTUAL"      # Let evidence speak, minimal argument


class ResponseSection(str, Enum):
    OPENING         = "OPENING"
    EVIDENCE_SUMMARY = "EVIDENCE_SUMMARY"
    REBUTTAL        = "REBUTTAL"
    CLOSING         = "CLOSING"


@dataclass
class ResponseParagraph:
    section: ResponseSection
    heading: str
    body: str


@dataclass
class AttachmentItem:
    name: str
    is_required: bool
    description: str
    has_it: bool    # Whether the merchant already has this


@dataclass
class DisputeResponseInput:
    """Input for response composition."""
    dispute_id: str
    reason_code: str
    network: str
    transaction_amount: float
    buyer_claim: str    # The buyer's stated reason for dispute

    # Evidence available
    has_tracking_number: bool = False
    tracking_number: str = ""
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    has_product_listing: bool = False
    has_buyer_communication: bool = False
    has_refund_policy: bool = False
    has_ip_log: bool = False
    has_order_history: bool = False
    has_device_fingerprint: bool = False

    # Context
    seller_protection_eligible: bool = False
    ce30_applicable: bool = False
    win_probability: float = 0.50

    # Merchant info
    merchant_name: str = "Merchant"
    order_date: str = ""    # ISO date string, optional
    ship_date: str = ""


# ── Templates by reason code ─────────────────────────────────────

_OPENING_TEMPLATES: dict[str, str] = {
    "INR": (
        "We are respectfully submitting our response to dispute {dispute_id}. "
        "We fulfilled this order in full and have proof of shipment and delivery. "
        "The item was shipped to the buyer's confirmed address within our stated "
        "handling window, and tracking confirms the package was delivered."
    ),
    "SNAD": (
        "We are respectfully submitting our response to dispute {dispute_id}. "
        "The item delivered precisely matches the product listing at time of purchase. "
        "Our refund policy, which was visible at checkout, provides the appropriate "
        "remedy for any dissatisfaction with the product."
    ),
    "UNAUTHORIZED": (
        "We are respectfully submitting our response to dispute {dispute_id}. "
        "The order was placed from the buyer's verified account, using the confirmed "
        "address on file, and all authentication signals indicate a legitimate purchase. "
        "We have attached IP logs and order history confirming this."
    ),
    "SUBSCRIPTION": (
        "We are respectfully submitting our response to dispute {dispute_id}. "
        "The charge in question is a recurring subscription the buyer explicitly enrolled in. "
        "Cancellation terms were clearly disclosed at sign-up and remain available "
        "through self-service on our platform."
    ),
}

_REBUTTAL_TEMPLATES: dict[str, list[str]] = {
    "INR": [
        "Buyer claims non-receipt: Our tracking record (#{tracking_number}) shows the package "
        "was scanned as DELIVERED on the expected delivery date. If the buyer cannot locate "
        "the package, we recommend they check with neighbors or their local carrier branch.",
        "Delivery address: The package was shipped to the exact address provided on the "
        "PayPal transaction and confirmed by the buyer's account.",
    ],
    "SNAD": [
        "Buyer claims item differs from description: Our product listing, archived at time of "
        "sale, accurately describes the item. We submit photos of the product alongside "
        "the original listing for direct comparison.",
        "Return policy: Our return policy was displayed at checkout and allows returns within "
        "the stated window. The buyer did not contact us to initiate a return before filing this dispute.",
    ],
    "UNAUTHORIZED": [
        "Buyer claims unauthorized use: Our server logs confirm the order was placed "
        "from an IP address consistent with prior purchases by this account.",
        "Account verification: The order used the buyer's saved payment method and was "
        "shipped to their confirmed address — patterns inconsistent with unauthorized use.",
    ],
    "SUBSCRIPTION": [
        "Buyer claims they cancelled: We have no record of a cancellation request "
        "matching this account prior to the charge date.",
        "Terms disclosure: Our subscription terms, including renewal charge details and "
        "cancellation instructions, were provided at sign-up and are attached.",
    ],
}

_CLOSING_TEMPLATES: dict[str, str] = {
    "INR": (
        "We respectfully request that this dispute be resolved in our favor. "
        "We have fulfilled our obligations as a seller: the item was shipped promptly, "
        "delivered to the buyer's address, and all evidence is attached for review."
    ),
    "SNAD": (
        "We respectfully request that this dispute be resolved in our favor. "
        "The item delivered matches the listing in all material respects, and our "
        "refund policy provides a fair remedy for the buyer should they remain dissatisfied."
    ),
    "UNAUTHORIZED": (
        "We respectfully request that this dispute be resolved in our favor. "
        "All available signals indicate the transaction was authorized by the account holder. "
        "We have attached our complete order and authentication records for review."
    ),
    "SUBSCRIPTION": (
        "We respectfully request that this dispute be resolved in our favor. "
        "This was a legitimate recurring charge that the buyer enrolled in with full "
        "disclosure of terms. We are happy to process a cancellation going forward."
    ),
}

_REQUIRED_ATTACHMENTS: dict[str, list[tuple[str, str]]] = {
    "INR": [
        ("Proof of shipment", "Shipping label or carrier receipt with ship date"),
        ("Tracking record", "Full tracking history showing delivery scan"),
        ("Order details", "Order confirmation showing buyer's address and items"),
    ],
    "SNAD": [
        ("Product listing", "Screenshot of listing at time of sale"),
        ("Refund policy", "Screenshot of policy as shown at checkout"),
        ("Product photos", "Photos of item matching listing description"),
    ],
    "UNAUTHORIZED": [
        ("IP log", "Server log showing IP address at time of order placement"),
        ("Order history", "Account order history showing consistent purchase pattern"),
        ("Shipping confirmation", "Confirmation shipped to buyer's confirmed address"),
    ],
    "SUBSCRIPTION": [
        ("Subscription terms", "Terms disclosed at sign-up"),
        ("Cancellation policy", "Cancellation instructions as shown to buyer"),
        ("Account enrollment", "Record of buyer enrolling in subscription"),
    ],
}

_BONUS_ATTACHMENTS: dict[str, list[tuple[str, str]]] = {
    "INR": [
        ("Delivery confirmation", "Signed delivery receipt if available"),
        ("Buyer communication", "Any messages where buyer confirmed receiving"),
    ],
    "UNAUTHORIZED": [
        ("3DS authentication", "3D Secure authentication record"),
        ("Device fingerprint", "Device / browser fingerprint matching buyer's prior orders"),
    ],
}


@dataclass
class DisputeResponseReport:
    """Structured dispute response package."""
    dispute_id: str
    reason_code: str
    network: str

    paragraphs: list[ResponseParagraph]
    attachments: list[AttachmentItem]
    tone: ResponseTone

    missing_critical_attachments: list[str]
    can_file_complete: bool
    word_count_estimate: int
    summary: str


def compose_dispute_response(inp: DisputeResponseInput) -> DisputeResponseReport:
    """Compose a structured dispute response."""
    # Fallback templates for unknown codes
    code = inp.reason_code
    if code not in _OPENING_TEMPLATES:
        code = "INR"   # Fallback to INR template

    opening_tmpl = _OPENING_TEMPLATES[code]
    closing_tmpl = _CLOSING_TEMPLATES[code]
    rebuttal_items = _REBUTTAL_TEMPLATES.get(code, [])

    tracking_num = inp.tracking_number if inp.tracking_number else "see attached"

    opening_body = opening_tmpl.format(
        dispute_id=inp.dispute_id,
        merchant=inp.merchant_name,
        amount=inp.transaction_amount,
    )

    # Evidence summary
    evidence_lines: list[str] = []
    if inp.has_tracking_number:
        evidence_lines.append(f"Tracking number: {tracking_num}")
    if inp.has_delivery_confirmation:
        evidence_lines.append("Delivery confirmation from carrier")
    if inp.has_signature_confirmation:
        evidence_lines.append("Signed delivery confirmation")
    if inp.has_product_listing:
        evidence_lines.append("Original product listing (archived)")
    if inp.has_buyer_communication:
        evidence_lines.append("Buyer communication history")
    if inp.has_refund_policy:
        evidence_lines.append("Refund policy as shown at checkout")
    if inp.has_ip_log:
        evidence_lines.append("Server IP log at time of order")
    if inp.has_order_history:
        evidence_lines.append("Account order history")
    if inp.ce30_applicable:
        evidence_lines.append("CE3.0: prior non-disputed transactions (attached)")

    ev_body = "We are submitting the following supporting evidence:\n"
    if evidence_lines:
        ev_body += "\n".join(f"  • {e}" for e in evidence_lines)
    else:
        ev_body += "  (Evidence pending — please attach before filing)"

    # Rebuttal
    rebuttal_body = "\n".join(
        item.format(tracking_number=tracking_num)
        for item in rebuttal_items
    )
    if not rebuttal_body:
        rebuttal_body = (
            "We dispute the buyer's characterization of this transaction. "
            "Our records demonstrate that all seller obligations were fulfilled."
        )

    paragraphs = [
        ResponseParagraph(ResponseSection.OPENING, "Statement of Position", opening_body),
        ResponseParagraph(ResponseSection.EVIDENCE_SUMMARY, "Supporting Evidence", ev_body),
        ResponseParagraph(ResponseSection.REBUTTAL, "Rebuttal to Buyer's Claim", rebuttal_body),
        ResponseParagraph(ResponseSection.CLOSING, "Closing Statement", closing_tmpl),
    ]

    # Attachments
    required = _REQUIRED_ATTACHMENTS.get(inp.reason_code, _REQUIRED_ATTACHMENTS.get(code, []))
    bonus    = _BONUS_ATTACHMENTS.get(inp.reason_code, [])

    # Map evidence fields to attachment names
    evidence_flags = {
        "Tracking record":          inp.has_tracking_number,
        "Proof of shipment":        inp.has_tracking_number,
        "Delivery confirmation":    inp.has_delivery_confirmation,
        "Signed delivery confirmation": inp.has_signature_confirmation,
        "Product listing":          inp.has_product_listing,
        "Refund policy":            inp.has_refund_policy,
        "Product photos":           inp.has_product_listing,
        "IP log":                   inp.has_ip_log,
        "Order history":            inp.has_order_history,
        "Device fingerprint":       inp.has_device_fingerprint,
        "Buyer communication":      inp.has_buyer_communication,
        "Order details":            True,
        "Shipping confirmation":    inp.has_tracking_number,
        "Account enrollment":       inp.has_order_history,
        "Subscription terms":       inp.has_refund_policy,
        "Cancellation policy":      inp.has_refund_policy,
        "3DS authentication":       False,
    }

    attachments: list[AttachmentItem] = []
    missing_critical: list[str] = []

    for name, desc in required:
        has_it = evidence_flags.get(name, False)
        attachments.append(AttachmentItem(name=name, is_required=True,
                                           description=desc, has_it=has_it))
        if not has_it:
            missing_critical.append(name)

    for name, desc in bonus:
        has_it = evidence_flags.get(name, False)
        attachments.append(AttachmentItem(name=name, is_required=False,
                                           description=desc, has_it=has_it))

    can_file = len(missing_critical) == 0

    # Tone
    if inp.win_probability >= 0.70 and can_file:
        tone = ResponseTone.ASSERTIVE
    elif inp.win_probability < 0.45:
        tone = ResponseTone.CONCILIATORY
    elif can_file:
        tone = ResponseTone.PROFESSIONAL
    else:
        tone = ResponseTone.FACTUAL

    total_words = sum(len(p.body.split()) for p in paragraphs)

    summary = (
        f"Response {inp.dispute_id}: "
        f"can_file={can_file} | tone={tone.value} | "
        f"missing={len(missing_critical)} | ~{total_words}w"
    )

    return DisputeResponseReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        network=inp.network,
        paragraphs=paragraphs,
        attachments=attachments,
        tone=tone,
        missing_critical_attachments=missing_critical,
        can_file_complete=can_file,
        word_count_estimate=total_words,
        summary=summary,
    )
