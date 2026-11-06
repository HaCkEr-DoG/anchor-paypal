"""
Appeal Strategy Engine — post-loss dispute appeal decision engine.

When PayPal rules against the merchant, the merchant can appeal within
10 days (PayPal) or 45 days (card network chargeback). This module decides:
  1. Whether to appeal at all (EV analysis)
  2. What new evidence is needed to change the outcome
  3. The appeal strength (STRONG/MODERATE/WEAK)
  4. The appeal text to submit

PayPal appeal grounds:
  - New evidence not submitted in original response
  - PayPal made a factual error (wrong tracking number, etc.)
  - Buyer acknowledged receipt (new communication)
  - CE3.0 eligibility discovered after original response

Card network chargeback re-presentment:
  - Full compelling evidence package
  - Chargeback reason code specific requirements
  - Pre-arbitration vs. arbitration

Chargeback reason codes → evidence requirements:
  10.4 (Visa Unauthorized):   CE3.0 + IP/device match + prior orders
  10.1 (Visa EMV):            Not appeallable
  13.1 (Visa SNAD):           Listing accuracy + return policy + comms
  13.3 (Visa Damaged):        Photos + carrier damage report
  4853 (Mastercard SNAD):     Listing + return policy + buyer comms
  4863 (Mastercard Missing):  Tracking + signature
  4853 (Mastercard Charity):  Donation receipt

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AppealDecision(str, Enum):
    APPEAL_STRONG    = "APPEAL_STRONG"    # Strong case, file immediately
    APPEAL_MODERATE  = "APPEAL_MODERATE"  # Worth appealing with new evidence
    DO_NOT_APPEAL    = "DO_NOT_APPEAL"    # EV negative or deadline passed
    ESCALATE_NETWORK = "ESCALATE_NETWORK"  # Escalate to card network


class AppealGround(str, Enum):
    NEW_EVIDENCE          = "NEW_EVIDENCE"
    FACTUAL_ERROR         = "FACTUAL_ERROR"
    BUYER_ACKNOWLEDGMENT  = "BUYER_ACKNOWLEDGMENT"
    CE30_ELIGIBLE         = "CE30_ELIGIBLE"
    WRONG_REASON_CODE     = "WRONG_REASON_CODE"
    POLICY_VIOLATION      = "POLICY_VIOLATION"


@dataclass
class AppealInput:
    """Input for appeal strategy decision."""
    dispute_id: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED
    dispute_amount: float
    original_outcome: str         # LOST / PARTIAL_WIN
    days_since_ruling: int        # Days since PayPal ruled against merchant
    is_chargeback: bool           # True = card network; False = PayPal dispute

    # New evidence available (not in original submission)
    has_new_tracking_evidence: bool = False
    has_buyer_acknowledgment: bool = False    # Buyer messaged saying they got it
    has_new_delivery_photo: bool = False
    has_signature_confirmation: bool = False
    ce30_eligible: bool = False
    has_factual_error_proof: bool = False     # PayPal cited wrong tracking, etc.

    # Original submission
    original_evidence_items: int = 0  # Count of items in original package

    # Economics
    appeal_cost: float = 15.0     # Time + filing cost for appeal
    p_appeal_win: float = 0.0     # Estimated, computed internally if 0.0

    # Chargeback specifics
    chargeback_reason_code: str = ""
    card_network: str = ""        # VISA / MASTERCARD / AMEX / DISCOVER


@dataclass
class AppealStrategyReport:
    """
    Appeal strategy recommendation.
    """
    dispute_id: str
    decision: AppealDecision
    appeal_grounds: list[AppealGround]
    appeal_strength: str           # STRONG / MODERATE / WEAK / NONE
    p_appeal_win: float
    ev_appeal: float               # EV(appeal) = p_win × amount − appeal_cost
    deadline_days_remaining: int   # 0 = deadline passed

    # Required new evidence
    required_evidence: list[str]
    optional_evidence: list[str]

    # Appeal text
    appeal_text: str               # Template text to submit

    # Guidance
    rationale: str
    summary: str


def _appeal_deadline(is_chargeback: bool) -> int:
    """Days allowed to appeal after ruling."""
    return 45 if is_chargeback else 10


def _estimate_p_win(inp: AppealInput) -> float:
    """Estimate appeal win probability based on new evidence."""
    base = 0.15  # Base appeal win rate without new evidence

    if inp.has_factual_error_proof:
        base += 0.40   # PayPal made a factual error — almost always overturned
    if inp.has_buyer_acknowledgment:
        base += 0.35   # Buyer saying they got it is very strong
    if inp.ce30_eligible and inp.claim_type == "UNAUTHORIZED":
        base += 0.30
    if inp.has_new_tracking_evidence:
        base += 0.20
    if inp.has_signature_confirmation:
        base += 0.15
    if inp.has_new_delivery_photo:
        base += 0.10

    return min(0.92, base)


def _appeal_grounds(inp: AppealInput) -> list[AppealGround]:
    grounds: list[AppealGround] = []
    if inp.has_factual_error_proof:
        grounds.append(AppealGround.FACTUAL_ERROR)
    if inp.has_buyer_acknowledgment:
        grounds.append(AppealGround.BUYER_ACKNOWLEDGMENT)
    if inp.ce30_eligible and inp.claim_type == "UNAUTHORIZED":
        grounds.append(AppealGround.CE30_ELIGIBLE)
    if inp.has_new_tracking_evidence or inp.has_new_delivery_photo or inp.has_signature_confirmation:
        grounds.append(AppealGround.NEW_EVIDENCE)
    return grounds


def _appeal_text(inp: AppealInput, grounds: list[AppealGround]) -> str:
    parts = [f"Appeal — Dispute {inp.dispute_id}", ""]

    parts.append(
        f"I am appealing the ruling on dispute {inp.dispute_id} "
        f"for ${inp.dispute_amount:.2f}. "
        f"The original ruling was issued {inp.days_since_ruling} days ago."
    )
    parts.append("")

    if AppealGround.FACTUAL_ERROR in grounds:
        parts.append(
            "GROUND: Factual Error — The ruling appears to have relied on incorrect "
            "information. I am providing documentation correcting this error."
        )
    if AppealGround.BUYER_ACKNOWLEDGMENT in grounds:
        parts.append(
            "GROUND: Buyer Acknowledgment — The buyer has since communicated "
            "confirming receipt of the item. This message is attached."
        )
    if AppealGround.CE30_ELIGIBLE in grounds:
        parts.append(
            "GROUND: Compelling Evidence 3.0 — This transaction qualifies under "
            "Visa's CE3.0 program. The buyer has prior non-disputed transactions "
            "from the same device and IP address within 120 days."
        )
    if AppealGround.NEW_EVIDENCE in grounds:
        parts.append(
            "GROUND: New Evidence — Additional proof of delivery not included in "
            "my original response is now available and attached."
        )

    parts.append("")
    parts.append(
        "I respectfully request that this ruling be reconsidered in light of "
        "the new information provided. I believe this transaction meets all "
        "requirements for Seller Protection."
    )

    return "\n".join(parts)


def _required_evidence(inp: AppealInput, grounds: list[AppealGround]) -> list[str]:
    items: list[str] = []
    if AppealGround.FACTUAL_ERROR in grounds:
        items.append("Documentation proving the factual error (tracking screenshot, carrier record)")
    if AppealGround.BUYER_ACKNOWLEDGMENT in grounds:
        items.append("Screenshot of buyer's message confirming receipt (with timestamp)")
    if AppealGround.CE30_ELIGIBLE in grounds:
        items.append("Prior transaction records showing same IP/device (last 120 days)")
        items.append("At least 2 prior non-disputed transactions from buyer")
    if AppealGround.NEW_EVIDENCE in grounds:
        items.append("New tracking delivery confirmation")
        if inp.has_signature_confirmation:
            items.append("Signature confirmation PDF from carrier")
        if inp.has_new_delivery_photo:
            items.append("Delivery photo with timestamp")
    return items


def evaluate_appeal(inp: AppealInput) -> AppealStrategyReport:
    """
    Determine whether and how to appeal a lost PayPal dispute.
    """
    deadline = _appeal_deadline(inp.is_chargeback)
    days_remaining = deadline - inp.days_since_ruling

    if days_remaining <= 0:
        return AppealStrategyReport(
            dispute_id=inp.dispute_id,
            decision=AppealDecision.DO_NOT_APPEAL,
            appeal_grounds=[],
            appeal_strength="NONE",
            p_appeal_win=0.0,
            ev_appeal=-inp.appeal_cost,
            deadline_days_remaining=0,
            required_evidence=[],
            optional_evidence=[],
            appeal_text="",
            rationale=f"Appeal deadline passed ({deadline}d after ruling, {inp.days_since_ruling}d elapsed).",
            summary=f"Appeal {inp.dispute_id}: DEADLINE_PASSED — cannot appeal.",
        )

    p_win = inp.p_appeal_win if inp.p_appeal_win > 0.0 else _estimate_p_win(inp)
    ev = p_win * inp.dispute_amount - inp.appeal_cost
    grounds = _appeal_grounds(inp)

    # Strength
    if p_win >= 0.65:
        strength = "STRONG"
    elif p_win >= 0.35:
        strength = "MODERATE"
    elif p_win >= 0.15:
        strength = "WEAK"
    else:
        strength = "NONE"

    # Decision
    if ev <= 0 and strength == "NONE":
        decision = AppealDecision.DO_NOT_APPEAL
    elif strength == "STRONG":
        decision = AppealDecision.APPEAL_STRONG
    elif strength == "MODERATE":
        decision = AppealDecision.APPEAL_MODERATE
    elif inp.is_chargeback and inp.chargeback_reason_code in ("10.4", "4853"):
        decision = AppealDecision.ESCALATE_NETWORK
    else:
        decision = AppealDecision.DO_NOT_APPEAL

    req_evidence = _required_evidence(inp, grounds)
    opt_evidence: list[str] = []
    if inp.claim_type == "INR" and not inp.has_signature_confirmation:
        opt_evidence.append("Request retrospective signature confirmation record from carrier")
    if inp.claim_type == "SNAD":
        opt_evidence.append("Screenshots showing listing accuracy at time of purchase")

    appeal_text = _appeal_text(inp, grounds) if decision != AppealDecision.DO_NOT_APPEAL else ""

    rationale_parts = []
    if grounds:
        rationale_parts.append(f"Appeal grounds: {', '.join(g.value for g in grounds)}")
    rationale_parts.append(f"P(win)={p_win:.0%}, EV=${ev:.0f}, deadline={days_remaining}d remaining")
    if decision == AppealDecision.DO_NOT_APPEAL:
        rationale_parts.append("EV negative and no strong grounds — do not appeal")

    summary = (
        f"Appeal {inp.dispute_id}: {decision.value} | "
        f"strength={strength} | p_win={p_win:.0%} | ev=${ev:.0f} | "
        f"deadline={days_remaining}d | grounds={len(grounds)}"
    )

    return AppealStrategyReport(
        dispute_id=inp.dispute_id,
        decision=decision,
        appeal_grounds=grounds,
        appeal_strength=strength,
        p_appeal_win=round(p_win, 4),
        ev_appeal=round(ev, 2),
        deadline_days_remaining=days_remaining,
        required_evidence=req_evidence,
        optional_evidence=opt_evidence,
        appeal_text=appeal_text,
        rationale=" | ".join(rationale_parts),
        summary=summary,
    )
