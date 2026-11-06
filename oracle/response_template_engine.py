"""
Response Template Engine — generate PayPal dispute response text.

PayPal's dispute resolution team reads merchant responses. The quality and
structure of the response text directly affects win rate. This module generates:
  1. Formal dispute response text (the submission to PayPal)
  2. Buyer-facing resolution message (via PayPal messaging)
  3. Internal case summary (for merchant records)

Best practices for PayPal response text:
  - Lead with the strongest evidence
  - Use specific dates, tracking numbers, amounts
  - Mirror PayPal's own language ("Proof of Delivery", "Seller Protection")
  - Keep responses factual and professional — no emotional language
  - For INR: emphasize tracking + signature confirmation
  - For SNAD: emphasize listing accuracy + return policy
  - For UNAUTHORIZED: emphasize device/IP match + prior successful orders
  - For chargebacks: cite CE3.0 if eligible (Visa unauthorized)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResponseTone(str, Enum):
    FORMAL       = "FORMAL"       # Standard PayPal dispute response
    CONCILIATORY = "CONCILIATORY" # Buyer-friendly, settlement-oriented
    ASSERTIVE    = "ASSERTIVE"    # Strong evidence, confident tone
    TECHNICAL    = "TECHNICAL"    # Chargeback with legal/policy citations


class ResponseSection(str, Enum):
    HEADER           = "HEADER"
    TRANSACTION_FACTS = "TRANSACTION_FACTS"
    DELIVERY_PROOF   = "DELIVERY_PROOF"
    PRODUCT_EVIDENCE = "PRODUCT_EVIDENCE"
    TRANSACTION_LEGITIMACY = "TRANSACTION_LEGITIMACY"
    POLICY_COMPLIANCE = "POLICY_COMPLIANCE"
    BUYER_COMMUNICATION = "BUYER_COMMUNICATION"
    CONCLUSION       = "CONCLUSION"


@dataclass
class DisputeContext:
    """Context for generating a dispute response."""
    dispute_id: str
    claim_type: str              # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION / DUPLICATE
    dispute_amount: float
    order_id: str
    order_date: str              # ISO date string

    # Delivery facts
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    delivery_date: Optional[str] = None
    delivery_address: str = ""
    has_signature: bool = False
    has_delivery_photo: bool = False

    # Product facts
    item_description: str = ""
    listing_url: Optional[str] = None

    # Transaction legitimacy
    buyer_ip: Optional[str] = None
    shipping_ip_match: bool = False
    prior_order_count: int = 0

    # Policy
    return_policy_url: Optional[str] = None
    tos_accepted_date: Optional[str] = None
    cancellation_policy_url: Optional[str] = None

    # Comms
    last_message_date: Optional[str] = None
    resolution_offered: bool = False
    resolution_description: str = ""

    # CE3.0 eligibility (Visa unauthorized)
    ce30_eligible: bool = False
    ce30_prior_transactions: int = 0

    # Tone
    tone: ResponseTone = ResponseTone.FORMAL


@dataclass
class ResponseBlock:
    """One section of a dispute response."""
    section: ResponseSection
    heading: str
    body: str
    evidence_references: list[str] = field(default_factory=list)


@dataclass
class DisputeResponseReport:
    """
    Generated dispute response text package.
    """
    dispute_id: str
    claim_type: str
    tone: ResponseTone

    # Generated text
    paypal_response_text: str    # Full text to submit to PayPal
    buyer_message_text: str      # Message to send buyer via PayPal messaging
    internal_summary: str        # For merchant records

    # Structured blocks
    blocks: list[ResponseBlock]

    # Guidance
    submission_checklist: list[str]   # Items to verify before submitting
    estimated_strength: str          # STRONG / MODERATE / WEAK based on context
    summary: str


def _inr_response(ctx: DisputeContext) -> list[ResponseBlock]:
    blocks: list[ResponseBlock] = []

    # Header
    blocks.append(ResponseBlock(
        section=ResponseSection.HEADER,
        heading="Dispute Response — Item Not Received",
        body=(
            f"I am responding to dispute {ctx.dispute_id} regarding order {ctx.order_id} "
            f"placed on {ctx.order_date} for ${ctx.dispute_amount:.2f}. "
            f"I respectfully dispute this claim and provide the following evidence of delivery."
        ),
    ))

    # Transaction facts
    blocks.append(ResponseBlock(
        section=ResponseSection.TRANSACTION_FACTS,
        heading="Transaction Details",
        body=(
            f"Order ID: {ctx.order_id}\n"
            f"Order Date: {ctx.order_date}\n"
            f"Amount: ${ctx.dispute_amount:.2f}\n"
            f"Ship-to Address: {ctx.delivery_address}"
        ),
    ))

    # Delivery proof
    delivery_parts = []
    evidence_refs = []
    if ctx.tracking_number and ctx.carrier:
        delivery_parts.append(
            f"The order was shipped via {ctx.carrier} with tracking number {ctx.tracking_number}."
        )
        evidence_refs.append(f"Tracking: {ctx.tracking_number}")
    if ctx.delivery_date:
        delivery_parts.append(
            f"Tracking confirms the package was delivered on {ctx.delivery_date} to the address on file."
        )
    if ctx.has_signature:
        delivery_parts.append(
            "Delivery was completed with signature confirmation, meeting PayPal's Seller Protection requirements."
        )
        evidence_refs.append("Signature confirmation record attached")
    if ctx.has_delivery_photo:
        delivery_parts.append(
            "A delivery photo was captured at time of delivery. Photo is attached as evidence."
        )
        evidence_refs.append("Delivery photo attached")
    if not delivery_parts:
        delivery_parts.append("Shipping documentation is attached as evidence.")

    blocks.append(ResponseBlock(
        section=ResponseSection.DELIVERY_PROOF,
        heading="Proof of Delivery",
        body=" ".join(delivery_parts),
        evidence_references=evidence_refs,
    ))

    # Buyer comms
    if ctx.last_message_date or ctx.resolution_offered:
        comms_parts = []
        if ctx.last_message_date:
            comms_parts.append(f"I last communicated with the buyer on {ctx.last_message_date}.")
        if ctx.resolution_offered:
            comms_parts.append(f"I offered a resolution: {ctx.resolution_description}.")
        blocks.append(ResponseBlock(
            section=ResponseSection.BUYER_COMMUNICATION,
            heading="Buyer Communication",
            body=" ".join(comms_parts),
        ))

    # Conclusion
    blocks.append(ResponseBlock(
        section=ResponseSection.CONCLUSION,
        heading="Conclusion",
        body=(
            f"Based on the tracking and delivery evidence provided, the order was delivered as promised. "
            f"This transaction qualifies for PayPal Seller Protection as an Item Not Received claim. "
            f"I respectfully request that this dispute be resolved in my favor."
        ),
    ))

    return blocks


def _snad_response(ctx: DisputeContext) -> list[ResponseBlock]:
    blocks: list[ResponseBlock] = []

    blocks.append(ResponseBlock(
        section=ResponseSection.HEADER,
        heading="Dispute Response — Significantly Not As Described",
        body=(
            f"I am responding to dispute {ctx.dispute_id} regarding order {ctx.order_id}. "
            f"I believe the item was accurately described and the buyer's claim is not valid."
        ),
    ))

    blocks.append(ResponseBlock(
        section=ResponseSection.TRANSACTION_FACTS,
        heading="Transaction Details",
        body=(
            f"Order ID: {ctx.order_id} | Date: {ctx.order_date} | Amount: ${ctx.dispute_amount:.2f}"
        ),
    ))

    product_parts = [f"The item was listed as: {ctx.item_description}."]
    evidence_refs = []
    if ctx.listing_url:
        product_parts.append(
            f"Screenshots of the original listing (at the time of purchase) are attached, "
            f"showing all specifications and conditions accurately described."
        )
        evidence_refs.append("Listing screenshots attached")
    product_parts.append(
        "The item shipped matches the listed specifications. "
        "Photos of the item before shipment are attached."
    )

    blocks.append(ResponseBlock(
        section=ResponseSection.PRODUCT_EVIDENCE,
        heading="Product Description Evidence",
        body=" ".join(product_parts),
        evidence_references=evidence_refs,
    ))

    if ctx.return_policy_url:
        blocks.append(ResponseBlock(
            section=ResponseSection.POLICY_COMPLIANCE,
            heading="Return Policy",
            body=(
                f"My return policy, displayed at the time of purchase, allows returns "
                f"for items not as described. The buyer did not initiate a return before filing this dispute. "
                f"I remain willing to accept a return under my posted policy."
            ),
        ))

    if ctx.last_message_date:
        blocks.append(ResponseBlock(
            section=ResponseSection.BUYER_COMMUNICATION,
            heading="Buyer Communication",
            body=f"I attempted to resolve this directly with the buyer on {ctx.last_message_date} but did not reach agreement.",
        ))

    blocks.append(ResponseBlock(
        section=ResponseSection.CONCLUSION,
        heading="Conclusion",
        body=(
            "The item was accurately described in the listing and the buyer received what was advertised. "
            "I request that this dispute be resolved in my favor."
        ),
    ))

    return blocks


def _unauthorized_response(ctx: DisputeContext) -> list[ResponseBlock]:
    blocks: list[ResponseBlock] = []

    ce30_note = ""
    if ctx.ce30_eligible and ctx.ce30_prior_transactions >= 2:
        ce30_note = (
            f" This transaction is also eligible for Visa's Compelling Evidence 3.0 program, "
            f"as the buyer has {ctx.ce30_prior_transactions} prior non-disputed transactions "
            f"from the same device and IP address within the past 120 days."
        )

    blocks.append(ResponseBlock(
        section=ResponseSection.HEADER,
        heading="Dispute Response — Unauthorized Transaction",
        body=(
            f"I am responding to dispute {ctx.dispute_id} for order {ctx.order_id}. "
            f"I have strong evidence this transaction was authorized by the account holder.{ce30_note}"
        ),
    ))

    blocks.append(ResponseBlock(
        section=ResponseSection.TRANSACTION_FACTS,
        heading="Transaction Details",
        body=(
            f"Order ID: {ctx.order_id} | Date: {ctx.order_date} | Amount: ${ctx.dispute_amount:.2f}\n"
            f"Ship-to: {ctx.delivery_address}"
        ),
    ))

    legitimacy_parts = []
    evidence_refs = []
    if ctx.buyer_ip:
        legitimacy_parts.append(
            f"The transaction was placed from IP address {ctx.buyer_ip}, "
            f"which matches the buyer's registered account IP."
        )
        evidence_refs.append("Transaction IP log attached")
    if ctx.shipping_ip_match:
        legitimacy_parts.append("The shipping address matches the buyer's account history.")
    if ctx.prior_order_count > 0:
        legitimacy_parts.append(
            f"The buyer has {ctx.prior_order_count} prior successful orders from the same account "
            f"with no previous disputes, indicating an established relationship."
        )
        evidence_refs.append(f"{ctx.prior_order_count} prior orders documented")

    if not legitimacy_parts:
        legitimacy_parts.append("Transaction legitimacy documentation is attached.")

    blocks.append(ResponseBlock(
        section=ResponseSection.TRANSACTION_LEGITIMACY,
        heading="Transaction Legitimacy Evidence",
        body=" ".join(legitimacy_parts),
        evidence_references=evidence_refs,
    ))

    if ctx.tracking_number and ctx.delivery_date:
        blocks.append(ResponseBlock(
            section=ResponseSection.DELIVERY_PROOF,
            heading="Delivery Confirmation",
            body=(
                f"The order was delivered on {ctx.delivery_date} via {ctx.carrier or 'carrier'} "
                f"(tracking: {ctx.tracking_number}) to the buyer's registered address."
            ),
        ))

    blocks.append(ResponseBlock(
        section=ResponseSection.CONCLUSION,
        heading="Conclusion",
        body=(
            "Based on the transaction legitimacy evidence provided, this order was authorized "
            "by the account holder. I request that this dispute be resolved in my favor."
        ),
    ))

    return blocks


def _subscription_response(ctx: DisputeContext) -> list[ResponseBlock]:
    blocks: list[ResponseBlock] = []

    blocks.append(ResponseBlock(
        section=ResponseSection.HEADER,
        heading="Dispute Response — Subscription / Service",
        body=(
            f"I am responding to dispute {ctx.dispute_id} for order {ctx.order_id}. "
            f"The buyer accepted the subscription terms and did not cancel before this charge."
        ),
    ))

    blocks.append(ResponseBlock(
        section=ResponseSection.TRANSACTION_FACTS,
        heading="Transaction Details",
        body=f"Order ID: {ctx.order_id} | Date: {ctx.order_date} | Amount: ${ctx.dispute_amount:.2f}",
    ))

    policy_parts = []
    if ctx.tos_accepted_date:
        policy_parts.append(
            f"The buyer accepted the Terms of Service on {ctx.tos_accepted_date}. "
            f"The ToS clearly states the subscription terms, billing cycle, and cancellation policy."
        )
    if ctx.cancellation_policy_url:
        policy_parts.append(
            "The cancellation policy allows cancellation at any time before the next billing date. "
            "No cancellation was submitted before this charge."
        )

    if policy_parts:
        blocks.append(ResponseBlock(
            section=ResponseSection.POLICY_COMPLIANCE,
            heading="Subscription Terms & Cancellation Policy",
            body=" ".join(policy_parts),
        ))

    if ctx.last_message_date:
        blocks.append(ResponseBlock(
            section=ResponseSection.BUYER_COMMUNICATION,
            heading="Buyer Communication",
            body=f"Communication with the buyer on {ctx.last_message_date} is documented.",
        ))

    blocks.append(ResponseBlock(
        section=ResponseSection.CONCLUSION,
        heading="Conclusion",
        body=(
            "The subscription was active and the charge was authorized per the agreed terms. "
            "I request that this dispute be resolved in my favor."
        ),
    ))

    return blocks


def _buyer_message(ctx: DisputeContext) -> str:
    if ctx.claim_type == "INR":
        return (
            f"Hi, I wanted to reach out about your dispute. "
            f"Your order {ctx.order_id} was delivered on {ctx.delivery_date or 'the date shown in tracking'} "
            f"via {ctx.carrier or 'the carrier'}. "
            f"Please check with neighbors or your building's mail area. "
            f"If you still cannot locate the package, please let me know and I'll help resolve this."
        )
    if ctx.claim_type == "SNAD":
        return (
            f"Hi, I'm sorry your order didn't meet your expectations. "
            f"I believe the item was as described, but I want to make this right. "
            f"Please reach out so we can discuss a resolution — I'm open to a return or partial refund "
            f"per my posted return policy."
        )
    if ctx.claim_type == "UNAUTHORIZED":
        return (
            f"Hi, I see a dispute was filed for order {ctx.order_id}. "
            f"This order was placed from your registered account and delivered to your address. "
            f"If you believe someone else used your account, please contact PayPal directly to report fraud. "
            f"I'm happy to provide any additional information."
        )
    return (
        f"Hi, I'd like to resolve your concern about order {ctx.order_id}. "
        f"Please reach out so we can find a solution quickly."
    )


def _strength(ctx: DisputeContext) -> str:
    score = 0
    if ctx.tracking_number:
        score += 1
    if ctx.delivery_date:
        score += 1
    if ctx.has_signature:
        score += 2
    if ctx.has_delivery_photo:
        score += 1
    if ctx.ce30_eligible:
        score += 2
    if ctx.buyer_ip:
        score += 1
    if ctx.prior_order_count >= 2:
        score += 1
    if score >= 5:
        return "STRONG"
    if score >= 2:
        return "MODERATE"
    return "WEAK"


def _checklist(ctx: DisputeContext) -> list[str]:
    items = ["Verify dispute_id matches PayPal Resolution Center case number"]
    if ctx.tracking_number:
        items.append(f"Attach screenshot of tracking page for {ctx.tracking_number}")
    if ctx.has_signature:
        items.append("Attach signature confirmation PDF from carrier")
    if ctx.has_delivery_photo:
        items.append("Attach delivery photo file")
    if ctx.buyer_ip:
        items.append("Attach IP address log export")
    if ctx.tos_accepted_date:
        items.append("Attach ToS acceptance screenshot with timestamp")
    items.append("Submit response before deadline")
    return items


def generate_response(ctx: DisputeContext) -> DisputeResponseReport:
    """
    Generate a complete dispute response package.
    """
    if ctx.claim_type in ("INR",):
        blocks = _inr_response(ctx)
    elif ctx.claim_type == "SNAD":
        blocks = _snad_response(ctx)
    elif ctx.claim_type == "UNAUTHORIZED":
        blocks = _unauthorized_response(ctx)
    elif ctx.claim_type == "SUBSCRIPTION":
        blocks = _subscription_response(ctx)
    else:
        blocks = _inr_response(ctx)  # Default fallback

    # Assemble full text
    full_text_parts = []
    for block in blocks:
        full_text_parts.append(f"=== {block.heading} ===")
        full_text_parts.append(block.body)
        if block.evidence_references:
            full_text_parts.append("Evidence: " + "; ".join(block.evidence_references))
        full_text_parts.append("")
    paypal_text = "\n".join(full_text_parts).strip()

    buyer_msg = _buyer_message(ctx)
    strength = _strength(ctx)
    checklist = _checklist(ctx)

    internal_summary = (
        f"Dispute {ctx.dispute_id} | {ctx.claim_type} | ${ctx.dispute_amount:.2f} | "
        f"Order {ctx.order_id} ({ctx.order_date}) | "
        f"Strength: {strength} | "
        f"Tracking: {ctx.tracking_number or 'N/A'} | "
        f"Sig: {ctx.has_signature} | CE3.0: {ctx.ce30_eligible}"
    )

    summary = (
        f"Response {ctx.dispute_id}: {ctx.claim_type} ${ctx.dispute_amount:.0f} | "
        f"tone={ctx.tone.value} | strength={strength} | "
        f"blocks={len(blocks)} | checklist={len(checklist)} items"
    )

    return DisputeResponseReport(
        dispute_id=ctx.dispute_id,
        claim_type=ctx.claim_type,
        tone=ctx.tone,
        paypal_response_text=paypal_text,
        buyer_message_text=buyer_msg,
        internal_summary=internal_summary,
        blocks=blocks,
        submission_checklist=checklist,
        estimated_strength=strength,
        summary=summary,
    )
