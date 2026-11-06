"""
Dispute Auto-Responder — generate a structured response strategy and
draft response points for a PayPal dispute based on claim type, evidence
availability, and buyer communication context.

This module does NOT generate prose text — it produces a structured
response plan with key points, evidence checklist, and tone guidance
that an LLM or human can use to compose the actual response.

Response strategies:
  FIGHT    — contest the dispute with evidence
  SETTLE   — offer partial/full refund to close
  ACCEPT   — accept immediately (cost of fighting > refund)
  ESCALATE — escalate to card network

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResponseStrategy(str, Enum):
    FIGHT    = "FIGHT"
    SETTLE   = "SETTLE"
    ACCEPT   = "ACCEPT"
    ESCALATE = "ESCALATE"


class ResponseTone(str, Enum):
    FORMAL       = "FORMAL"       # Legal/professional language
    COOPERATIVE  = "COOPERATIVE"  # Helpful, good-faith
    FIRM         = "FIRM"         # Clear but not aggressive
    DE_ESCALATE  = "DE_ESCALATE"  # Calm hostile buyer


class EvidenceAvailability(str, Enum):
    ALL_PRESENT  = "ALL_PRESENT"
    PARTIAL      = "PARTIAL"
    MINIMAL      = "MINIMAL"
    NONE         = "NONE"


@dataclass
class DisputeAutoResponderInput:
    """Input for auto-response strategy generation."""
    dispute_id: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED / DUPLICATE
    transaction_amount: float

    # Evidence flags
    has_tracking_number: bool = False
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    has_product_photos: bool = False
    has_communications: bool = False
    has_refund_policy: bool = False
    has_ip_log: bool = False
    has_two_fa_record: bool = False

    # Context
    buyer_hostile: bool = False
    buyer_requested_refund: bool = False
    days_until_deadline: int = 10
    p_win: float = 0.50               # Calibrated win probability
    fight_cost: float = 10.0          # Internal cost to fight
    chargeback_fee: float = 20.0      # Fee if we lose

    # SP / CE3.0 info
    sp_eligible: bool = False
    ce30_applicable: bool = False


def _assess_evidence(inp: DisputeAutoResponderInput) -> EvidenceAvailability:
    ct = inp.claim_type.upper()
    if ct == "INR":
        present = sum([inp.has_tracking_number, inp.has_delivery_confirmation])
        if present == 2:
            return EvidenceAvailability.ALL_PRESENT
        if present == 1:
            return EvidenceAvailability.PARTIAL
        return EvidenceAvailability.NONE

    if ct == "SNAD":
        present = sum([inp.has_product_photos, inp.has_refund_policy, inp.has_communications])
        if present == 3:
            return EvidenceAvailability.ALL_PRESENT
        if present >= 1:
            return EvidenceAvailability.PARTIAL
        return EvidenceAvailability.NONE

    if ct == "UNAUTHORIZED":
        present = sum([inp.has_ip_log, inp.has_two_fa_record])
        if present == 2:
            return EvidenceAvailability.ALL_PRESENT
        if present == 1:
            return EvidenceAvailability.PARTIAL
        return EvidenceAvailability.NONE

    if ct == "DUPLICATE":
        return EvidenceAvailability.ALL_PRESENT  # Usually provable from records

    return EvidenceAvailability.PARTIAL


def _choose_strategy(inp: DisputeAutoResponderInput,
                     evidence: EvidenceAvailability) -> ResponseStrategy:
    amount = inp.transaction_amount
    fight_cost = inp.fight_cost + inp.chargeback_fee

    # Accept immediately if amount is tiny relative to fight cost
    if amount < fight_cost * 0.5 and not inp.sp_eligible:
        return ResponseStrategy.ACCEPT

    # EV-based decision
    ev_fight = inp.p_win * amount - fight_cost
    ev_refund = -amount

    if ev_fight > ev_refund and evidence in (
            EvidenceAvailability.ALL_PRESENT, EvidenceAvailability.PARTIAL):
        if inp.ce30_applicable:
            return ResponseStrategy.ESCALATE  # CE3.0 → escalate to Visa
        return ResponseStrategy.FIGHT

    if inp.buyer_requested_refund and amount < 100.0:
        return ResponseStrategy.ACCEPT

    if evidence == EvidenceAvailability.MINIMAL or evidence == EvidenceAvailability.NONE:
        if amount < 50.0:
            return ResponseStrategy.ACCEPT
        return ResponseStrategy.SETTLE

    return ResponseStrategy.SETTLE


def _response_tone(inp: DisputeAutoResponderInput) -> ResponseTone:
    if inp.buyer_hostile:
        return ResponseTone.DE_ESCALATE
    if inp.claim_type.upper() == "UNAUTHORIZED":
        return ResponseTone.FORMAL
    if inp.p_win < 0.4:
        return ResponseTone.COOPERATIVE
    return ResponseTone.FIRM


def _build_key_points(
        inp: DisputeAutoResponderInput,
        strategy: ResponseStrategy,
        evidence: EvidenceAvailability) -> list[str]:
    ct = inp.claim_type.upper()
    points: list[str] = []

    if strategy == ResponseStrategy.ACCEPT:
        points.append("Acknowledge the dispute and offer full refund")
        points.append(f"Reference dispute {inp.dispute_id} and transaction amount")
        return points

    if strategy == ResponseStrategy.SETTLE:
        points.append(f"Offer partial settlement (50-70% of ${inp.transaction_amount:.2f})")
        points.append("Explain steps taken to fulfill the order")
        if inp.has_communications:
            points.append("Reference prior buyer communications showing good faith")
        return points

    # FIGHT or ESCALATE
    if ct == "INR":
        if inp.has_tracking_number:
            points.append("Provide carrier tracking number with shipment date")
        if inp.has_delivery_confirmation:
            points.append("Include delivery confirmation scan at buyer's address")
        if inp.has_signature_confirmation:
            points.append("Attach signature confirmation record")
        points.append("Show order timeline: order date → ship date → delivery date")

    elif ct == "SNAD":
        if inp.has_product_photos:
            points.append("Attach product photos showing item matches description")
        if inp.has_refund_policy:
            points.append("Reference published return policy in effect at time of purchase")
        if inp.has_communications:
            points.append("Include buyer communications showing no prior complaints")
        points.append("Provide original product listing with full description")

    elif ct == "UNAUTHORIZED":
        if inp.has_ip_log:
            points.append("Submit IP address log matching buyer's known address")
        if inp.has_two_fa_record:
            points.append("Include 2FA or authentication record for the transaction")
        if inp.ce30_applicable:
            points.append("CE3.0: attach 2+ prior non-disputed transactions to same buyer/device/IP")
        points.append("Show order history proving buyer pattern of past purchases")

    elif ct == "DUPLICATE":
        points.append("Provide original transaction record showing single charge")
        points.append("Show no duplicate charge in payment processor logs")

    if inp.sp_eligible:
        points.append("Note Seller Protection eligibility — fee waiver applies if dispute resolves in merchant's favor")

    return points


def _evidence_checklist(
        inp: DisputeAutoResponderInput,
        evidence: EvidenceAvailability) -> list[str]:
    ct = inp.claim_type.upper()
    checklist: list[str] = []
    items: list[tuple[str, bool]] = []

    if ct == "INR":
        items = [
            ("Tracking number", inp.has_tracking_number),
            ("Delivery confirmation", inp.has_delivery_confirmation),
            ("Signature confirmation", inp.has_signature_confirmation),
        ]
    elif ct == "SNAD":
        items = [
            ("Product photos", inp.has_product_photos),
            ("Buyer communications", inp.has_communications),
            ("Return policy", inp.has_refund_policy),
        ]
    elif ct == "UNAUTHORIZED":
        items = [
            ("IP address log", inp.has_ip_log),
            ("2FA record", inp.has_two_fa_record),
        ]
    elif ct == "DUPLICATE":
        items = [
            ("Original transaction record", True),
        ]

    for label, present in items:
        status = "READY" if present else "MISSING"
        checklist.append(f"[{status}] {label}")

    return checklist


@dataclass
class DisputeAutoResponderReport:
    """Structured response plan for a PayPal dispute."""
    dispute_id: str
    claim_type: str

    strategy: ResponseStrategy
    tone: ResponseTone
    evidence_availability: EvidenceAvailability

    key_points: list[str]
    evidence_checklist: list[str]

    deadline_warning: Optional[str]
    sp_note: Optional[str]
    ce30_note: Optional[str]

    summary: str


def generate_response_plan(
        inp: DisputeAutoResponderInput) -> DisputeAutoResponderReport:
    """
    Generate a structured dispute response plan.
    """
    evidence = _assess_evidence(inp)
    strategy = _choose_strategy(inp, evidence)
    tone = _response_tone(inp)
    key_points = _build_key_points(inp, strategy, evidence)
    checklist = _evidence_checklist(inp, evidence)

    deadline_warning = None
    if inp.days_until_deadline <= 1:
        deadline_warning = "CRITICAL: Response deadline tomorrow — submit immediately"
    elif inp.days_until_deadline <= 3:
        deadline_warning = f"URGENT: {inp.days_until_deadline} days until response deadline"

    sp_note = None
    if inp.sp_eligible:
        sp_note = "Seller Protection eligible — chargeback fee waived if case won"

    ce30_note = None
    if inp.ce30_applicable:
        ce30_note = "CE3.0 eligible (Visa UNAUTHORIZED) — submit prior transactions for +18pp win boost"

    summary = (
        f"AutoResponder {inp.dispute_id} [{inp.claim_type.upper()}]: "
        f"strategy={strategy.value} | tone={tone.value} | "
        f"evidence={evidence.value} | p_win={inp.p_win:.0%} | "
        f"points={len(key_points)}"
    )

    return DisputeAutoResponderReport(
        dispute_id=inp.dispute_id,
        claim_type=inp.claim_type.upper(),
        strategy=strategy,
        tone=tone,
        evidence_availability=evidence,
        key_points=key_points,
        evidence_checklist=checklist,
        deadline_warning=deadline_warning,
        sp_note=sp_note,
        ce30_note=ce30_note,
        summary=summary,
    )
