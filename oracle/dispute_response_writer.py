"""
Dispute Response Writer — generate structured, evidence-backed dispute
response text ready to submit to PayPal via the Disputes API.

This is the "final mile" oracle: given all oracle decisions and available
evidence, write the actual dispute response the merchant submits.

Previous modules decide WHAT to do (fight, refund, escalate) and score
HOW STRONG the evidence is. This module writes the WORDS — the response
narrative that PayPal's dispute team reads.

Key principles:
  1. Facts first — specific order IDs, tracking numbers, dates
  2. Evidence references — cite each document in the package
  3. Buyer protection language — address buyer's exact claim
  4. Concise and professional — avoid emotional language
  5. Reason-code-specific templates — INR, SNAD, UNAUTHORIZED, SUBSCRIPTION
     all require different arguments

Five response sections:
  HEADER:  one-sentence summary (what happened, outcome)
  FACTS:   order timeline with specific dates and amounts
  EVIDENCE: itemized evidence list with document references
  REBUTTAL: direct counter to buyer's claim
  REQUEST:  explicit ask (close in merchant's favor / provide partial refund)

PayPal benefit:
  Well-structured responses reduce PayPal's dispute resolution time
  (fewer follow-up requests for clarification) and improve PayPal's
  Buyer Protection accuracy by ensuring evidence is clearly linked to claims.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResponseTone(str, Enum):
    ASSERTIVE   = "ASSERTIVE"   # strong evidence, fight to win
    COOPERATIVE = "COOPERATIVE" # partial evidence, offer resolution
    CONCILIATORY = "CONCILIATORY" # weak evidence, acknowledge buyer concern


class EvidenceItem(str, Enum):
    TRACKING_NUMBER         = "TRACKING_NUMBER"
    PROOF_OF_DELIVERY       = "PROOF_OF_DELIVERY"
    SIGNED_DELIVERY         = "SIGNED_DELIVERY"
    INVOICE                 = "INVOICE"
    ORDER_CONFIRMATION      = "ORDER_CONFIRMATION"
    BUYER_COMMUNICATION     = "BUYER_COMMUNICATION"
    PRODUCT_PHOTO           = "PRODUCT_PHOTO"
    POLICY_DOCUMENT         = "POLICY_DOCUMENT"
    SUBSCRIPTION_AGREEMENT  = "SUBSCRIPTION_AGREEMENT"
    CANCELLATION_RECORD     = "CANCELLATION_RECORD"
    IP_ADDRESS_LOG          = "IP_ADDRESS_LOG"
    DEVICE_FINGERPRINT      = "DEVICE_FINGERPRINT"
    TRANSACTION_RECORD      = "TRANSACTION_RECORD"
    FULFILLMENT_RECORD      = "FULFILLMENT_RECORD"
    CARRIER_CLAIM           = "CARRIER_CLAIM"


@dataclass
class ResponseContext:
    """All context needed to write a dispute response."""
    dispute_id: str
    merchant_id: str
    merchant_name: str
    reason_code: str            # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION

    # Order facts
    order_id: str
    order_date: str             # YYYY-MM-DD
    order_amount: float
    currency: str = "USD"
    item_description: str = ""
    buyer_name: str = ""

    # Fulfillment facts
    ship_date: str = ""
    carrier: str = ""
    tracking_number: str = ""
    delivery_date: str = ""     # empty if not yet delivered
    delivery_status: str = ""   # DELIVERED / IN_TRANSIT / EXCEPTION

    # Subscription specific
    subscription_start: str = ""
    last_billing_date: str = ""
    cancellation_requested: bool = False
    cancellation_date: str = ""

    # Evidence available
    evidence_items: list[EvidenceItem] = field(default_factory=list)

    # Decision context
    win_probability: float = 0.5
    recommended_action: str = ""  # FIGHT / REFUND / PARTIAL_REFUND

    # Optional: prior contact with buyer
    prior_contact_summary: str = ""   # e.g., "Buyer emailed 2026-11-01 requesting status"


@dataclass
class ResponseSection:
    section: str    # HEADER / FACTS / EVIDENCE / REBUTTAL / REQUEST
    content: str


@dataclass
class DisputeResponseDraft:
    dispute_id: str
    merchant_id: str
    reason_code: str
    tone: ResponseTone

    sections: list[ResponseSection]
    full_response: str           # All sections joined, ready for API submission

    evidence_count: int
    evidence_items_referenced: list[EvidenceItem]

    word_count: int
    is_submission_ready: bool    # True when essential evidence present

    submission_note: str         # e.g., "Upload tracking screenshot before submitting"
    summary: str


# ─── Template builders per reason code ────────────────────────────────────────

def _build_inr_response(ctx: ResponseContext, tone: ResponseTone) -> list[ResponseSection]:
    has_tracking = bool(ctx.tracking_number)
    has_delivery = EvidenceItem.PROOF_OF_DELIVERY in ctx.evidence_items
    has_signed = EvidenceItem.SIGNED_DELIVERY in ctx.evidence_items

    header = (
        f"Merchant {ctx.merchant_name} fulfilled Order #{ctx.order_id} on {ctx.ship_date} "
        f"and the item was {'delivered' if ctx.delivery_status == 'DELIVERED' else 'shipped'} "
        f"as confirmed by {ctx.carrier or 'the carrier'}."
        if has_tracking else
        f"Merchant {ctx.merchant_name} fulfilled Order #{ctx.order_id} on {ctx.ship_date}. "
        f"We are investigating the delivery status with the carrier."
    )

    facts_lines = [
        f"Order #{ctx.order_id} was placed on {ctx.order_date} for ${ctx.order_amount:.2f} {ctx.currency}.",
    ]
    if ctx.item_description:
        facts_lines.append(f"Item: {ctx.item_description}.")
    if ctx.ship_date:
        facts_lines.append(f"Order shipped on {ctx.ship_date} via {ctx.carrier or 'carrier'}.")
    if ctx.tracking_number:
        facts_lines.append(f"Tracking number: {ctx.tracking_number}.")
    if ctx.delivery_date:
        facts_lines.append(f"Carrier records confirm delivery on {ctx.delivery_date}.")
    if ctx.delivery_status == "EXCEPTION":
        facts_lines.append("Carrier has reported a delivery exception. A carrier trace has been initiated.")

    evidence_lines = ["Evidence attached to this response:"]
    for ev in ctx.evidence_items:
        evidence_lines.append(f"  - {ev.value.replace('_', ' ').title()}")

    rebuttal_lines = []
    if has_delivery or has_signed:
        rebuttal_lines.append(
            "Carrier records and/or signed delivery confirmation demonstrate that the "
            "shipment was delivered to the address provided by the buyer at checkout. "
            "The item was delivered as ordered."
        )
    elif has_tracking:
        rebuttal_lines.append(
            f"Tracking number {ctx.tracking_number} shows the shipment was dispatched "
            f"on {ctx.ship_date} and is in transit. Delivery is pending carrier completion."
        )
        if ctx.delivery_status == "EXCEPTION":
            rebuttal_lines.append(
                "We have filed a carrier claim and are working to resolve the delivery "
                "exception. We ask that PayPal hold the dispute pending carrier resolution."
            )
    else:
        rebuttal_lines.append(
            "We are actively working with the carrier to obtain delivery confirmation. "
            "We ask that PayPal allow additional time for carrier verification."
        )

    request = (
        "We respectfully request that PayPal close this dispute in the merchant's favor, "
        "as the item was shipped and delivered as confirmed by carrier records."
        if (has_delivery or has_signed) else
        "We respectfully request that PayPal hold this dispute pending carrier verification "
        "and delivery confirmation. We will provide updated carrier records within 48 hours."
        if has_tracking else
        "We ask for 72 hours to obtain carrier delivery confirmation and will update this response."
    )

    return [
        ResponseSection("HEADER", header),
        ResponseSection("FACTS", " ".join(facts_lines)),
        ResponseSection("EVIDENCE", "\n".join(evidence_lines)),
        ResponseSection("REBUTTAL", " ".join(rebuttal_lines)),
        ResponseSection("REQUEST", request),
    ]


def _build_snad_response(ctx: ResponseContext, tone: ResponseTone) -> list[ResponseSection]:
    has_photo = EvidenceItem.PRODUCT_PHOTO in ctx.evidence_items
    has_policy = EvidenceItem.POLICY_DOCUMENT in ctx.evidence_items
    has_invoice = EvidenceItem.INVOICE in ctx.evidence_items

    header = (
        f"Merchant {ctx.merchant_name} fulfilled Order #{ctx.order_id} exactly as listed: "
        f"{'product photos, invoice, and return policy are attached as evidence' if has_photo and has_invoice else 'evidence is attached'}."
    )

    facts_lines = [
        f"Order #{ctx.order_id} placed on {ctx.order_date} for ${ctx.order_amount:.2f} {ctx.currency}.",
    ]
    if ctx.item_description:
        facts_lines.append(f"Listed item: {ctx.item_description}.")
    if ctx.ship_date:
        facts_lines.append(f"Item shipped on {ctx.ship_date} via {ctx.carrier or 'carrier'}.")
    if ctx.delivery_date:
        facts_lines.append(f"Delivered on {ctx.delivery_date}.")

    evidence_lines = ["Evidence attached:"]
    for ev in ctx.evidence_items:
        evidence_lines.append(f"  - {ev.value.replace('_', ' ').title()}")

    rebuttal_lines = []
    if has_photo:
        rebuttal_lines.append(
            "Product photos attached confirm the item shipped matches the listing exactly, "
            "including condition, size, color, and all features described."
        )
    if has_invoice:
        rebuttal_lines.append(
            "The invoice confirms the item ordered and fulfilled matches the listing description."
        )
    if has_policy:
        rebuttal_lines.append(
            "Our return policy (attached) allows returns within 30 days for items not matching "
            "their description. The buyer did not use our return process prior to escalating."
        )
    if not rebuttal_lines:
        rebuttal_lines.append(
            "The item was fulfilled exactly as described in the listing. "
            "We stand behind the accuracy of our product description."
        )

    request = (
        "We respectfully request that PayPal close this dispute in the merchant's favor. "
        "The item was fulfilled as described and evidence of this is attached."
        if tone == ResponseTone.ASSERTIVE else
        "We are willing to accept a return for a full refund if the buyer returns the item "
        "in original condition. We request that PayPal facilitate a return before issuing any refund."
    )

    return [
        ResponseSection("HEADER", header),
        ResponseSection("FACTS", " ".join(facts_lines)),
        ResponseSection("EVIDENCE", "\n".join(evidence_lines)),
        ResponseSection("REBUTTAL", " ".join(rebuttal_lines)),
        ResponseSection("REQUEST", request),
    ]


def _build_unauthorized_response(ctx: ResponseContext, tone: ResponseTone) -> list[ResponseSection]:
    has_ip = EvidenceItem.IP_ADDRESS_LOG in ctx.evidence_items
    has_device = EvidenceItem.DEVICE_FINGERPRINT in ctx.evidence_items
    has_transaction = EvidenceItem.TRANSACTION_RECORD in ctx.evidence_items

    header = (
        f"Order #{ctx.order_id} was placed and fulfilled with matching authorization signals; "
        f"the transaction passed standard fraud screening at the time of purchase."
    )

    facts_lines = [
        f"Order #{ctx.order_id} placed on {ctx.order_date} for ${ctx.order_amount:.2f} {ctx.currency}.",
        "Transaction was authorized by the card network at time of purchase.",
    ]
    if ctx.ship_date:
        facts_lines.append(f"Order fulfilled and shipped on {ctx.ship_date}.")
    if ctx.delivery_date:
        facts_lines.append(f"Delivered {ctx.delivery_date}.")
    if ctx.prior_contact_summary:
        facts_lines.append(f"Buyer contact: {ctx.prior_contact_summary}")

    evidence_lines = ["Evidence attached:"]
    for ev in ctx.evidence_items:
        evidence_lines.append(f"  - {ev.value.replace('_', ' ').title()}")

    rebuttal_lines = []
    if has_ip:
        rebuttal_lines.append(
            "IP address log confirms the order was placed from an IP address consistent with "
            "the buyer's billing address region, with no VPN or proxy indicators."
        )
    if has_device:
        rebuttal_lines.append(
            "Device fingerprint matches prior authenticated sessions from this buyer account."
        )
    if has_transaction:
        rebuttal_lines.append(
            "Transaction record confirms AVS/CVV match and standard authorization signals, "
            "with no fraud indicators at time of processing."
        )
    if not rebuttal_lines:
        rebuttal_lines.append(
            "The transaction passed standard authorization checks. "
            "The order was fulfilled to the shipping address provided at checkout."
        )

    request = (
        "We respectfully request that PayPal close this unauthorized dispute in the merchant's favor. "
        "Authorization records confirm a legitimate transaction with matching fraud signals."
    )

    return [
        ResponseSection("HEADER", header),
        ResponseSection("FACTS", " ".join(facts_lines)),
        ResponseSection("EVIDENCE", "\n".join(evidence_lines)),
        ResponseSection("REBUTTAL", " ".join(rebuttal_lines)),
        ResponseSection("REQUEST", request),
    ]


def _build_subscription_response(ctx: ResponseContext, tone: ResponseTone) -> list[ResponseSection]:
    has_agreement = EvidenceItem.SUBSCRIPTION_AGREEMENT in ctx.evidence_items
    has_cancel = EvidenceItem.CANCELLATION_RECORD in ctx.evidence_items

    header = (
        f"The charge for Order #{ctx.order_id} on {ctx.order_date} was a recurring subscription "
        f"billing that the buyer agreed to at the time of enrollment."
        if not ctx.cancellation_requested else
        f"The buyer's cancellation request for Order #{ctx.order_id} was processed on {ctx.cancellation_date}; "
        f"the disputed charge was billed before the cancellation was received."
    )

    facts_lines = [
        f"Subscription charge of ${ctx.order_amount:.2f} {ctx.currency} billed on {ctx.order_date}.",
    ]
    if ctx.subscription_start:
        facts_lines.append(f"Subscription started: {ctx.subscription_start}.")
    if ctx.last_billing_date:
        facts_lines.append(f"Last billing date: {ctx.last_billing_date}.")
    if ctx.cancellation_requested and ctx.cancellation_date:
        facts_lines.append(f"Cancellation processed: {ctx.cancellation_date}.")

    evidence_lines = ["Evidence attached:"]
    for ev in ctx.evidence_items:
        evidence_lines.append(f"  - {ev.value.replace('_', ' ').title()}")

    rebuttal_lines = []
    if has_agreement:
        rebuttal_lines.append(
            "The attached subscription agreement confirms the buyer acknowledged and agreed to "
            "recurring billing terms at the time of enrollment, including billing frequency and amount."
        )
    if has_cancel and ctx.cancellation_requested:
        rebuttal_lines.append(
            f"The cancellation was processed on {ctx.cancellation_date}. "
            "The disputed charge was billed prior to the cancellation request being received. "
            "No future charges will be billed."
        )
    if not rebuttal_lines:
        rebuttal_lines.append(
            "This was an authorized recurring charge per the buyer's subscription agreement. "
            "The service was available and provided during the billing period."
        )

    request = (
        "We respectfully request that PayPal close this dispute in the merchant's favor. "
        "The charge was an authorized recurring subscription billing as agreed by the buyer."
        if not ctx.cancellation_requested else
        "We acknowledge the cancellation and will issue a prorated refund for the unused billing period. "
        "We ask that PayPal close this dispute as resolved."
    )

    return [
        ResponseSection("HEADER", header),
        ResponseSection("FACTS", " ".join(facts_lines)),
        ResponseSection("EVIDENCE", "\n".join(evidence_lines)),
        ResponseSection("REBUTTAL", " ".join(rebuttal_lines)),
        ResponseSection("REQUEST", request),
    ]


# ─── Tone selection ────────────────────────────────────────────────────────────

def _select_tone(ctx: ResponseContext) -> ResponseTone:
    if ctx.win_probability >= 0.70:
        return ResponseTone.ASSERTIVE
    if ctx.win_probability >= 0.40:
        return ResponseTone.COOPERATIVE
    return ResponseTone.CONCILIATORY


def _submission_check(ctx: ResponseContext, sections: list[ResponseSection]) -> tuple[bool, str]:
    essential = {
        "INR": [EvidenceItem.TRACKING_NUMBER, EvidenceItem.PROOF_OF_DELIVERY],
        "SNAD": [EvidenceItem.INVOICE, EvidenceItem.PRODUCT_PHOTO],
        "UNAUTHORIZED": [EvidenceItem.TRANSACTION_RECORD],
        "SUBSCRIPTION": [EvidenceItem.SUBSCRIPTION_AGREEMENT],
    }
    required = essential.get(ctx.reason_code, [])
    missing = [e for e in required if e not in ctx.evidence_items]
    if missing:
        note = "Before submitting, upload: " + ", ".join(
            m.value.replace("_", " ").title() for m in missing
        )
        return False, note
    return True, "Response is ready to submit."


# ─── Main function ─────────────────────────────────────────────────────────────

def write_dispute_response(ctx: ResponseContext) -> DisputeResponseDraft:
    """
    Generate a structured, evidence-backed dispute response ready for
    submission to PayPal's Disputes API.
    """
    tone = _select_tone(ctx)

    if ctx.reason_code == "INR":
        sections = _build_inr_response(ctx, tone)
    elif ctx.reason_code == "SNAD":
        sections = _build_snad_response(ctx, tone)
    elif ctx.reason_code == "UNAUTHORIZED":
        sections = _build_unauthorized_response(ctx, tone)
    elif ctx.reason_code == "SUBSCRIPTION":
        sections = _build_subscription_response(ctx, tone)
    else:
        sections = _build_inr_response(ctx, tone)

    full_response = "\n\n".join(
        f"[{s.section}]\n{s.content}" for s in sections
    )
    word_count = len(full_response.split())
    ready, note = _submission_check(ctx, sections)

    summary = (
        f"Response {ctx.dispute_id} [{ctx.reason_code}] "
        f"tone={tone.value} words={word_count} "
        f"evidence={len(ctx.evidence_items)} ready={ready}"
    )

    return DisputeResponseDraft(
        dispute_id=ctx.dispute_id,
        merchant_id=ctx.merchant_id,
        reason_code=ctx.reason_code,
        tone=tone,
        sections=sections,
        full_response=full_response,
        evidence_count=len(ctx.evidence_items),
        evidence_items_referenced=list(ctx.evidence_items),
        word_count=word_count,
        is_submission_ready=ready,
        submission_note=note,
        summary=summary,
    )
