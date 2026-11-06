"""
Deadline Manager — compute and track all deadlines for an open dispute.

PayPal disputes involve multiple overlapping deadlines across three phases:
  1. Buyer escalation window (10 days after PayPal opens dispute)
  2. Merchant response window (varies by claim type and network)
  3. Appeal window (10 days after PayPal ruling; 45 days for card network)

Missing any single deadline turns a winnable dispute into an auto-loss.
This module computes all relevant deadlines, flags overdue items, and
generates a time-ordered action schedule.

Deadline rules:
  PayPal INR         — merchant must respond within 10 days of opening
  PayPal SNAD        — 10 days
  PayPal UNAUTHORIZED— 10 days
  Visa chargeback    — 20 days (extendable to 45)
  Mastercard         — 45 days
  Amex               — 20 days
  Discover           — 20 days
  Appeal (PayPal)    — 10 days after ruling date
  Appeal (network)   — 45 days after ruling date
  CE3.0 rebuttal     — 20 days after notification

Risk tiers:
  RED    — Overdue or ≤ 1 day remaining
  ORANGE — 2–3 days remaining
  YELLOW — 4–7 days remaining
  GREEN  — > 7 days remaining

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DeadlineType(str, Enum):
    MERCHANT_RESPONSE    = "MERCHANT_RESPONSE"
    EVIDENCE_SUBMISSION  = "EVIDENCE_SUBMISSION"
    APPEAL_PAYPAL        = "APPEAL_PAYPAL"
    APPEAL_NETWORK       = "APPEAL_NETWORK"
    CE30_REBUTTAL        = "CE30_REBUTTAL"
    BUYER_ESCALATION     = "BUYER_ESCALATION"
    REFUND_WINDOW        = "REFUND_WINDOW"


class DeadlineUrgency(str, Enum):
    RED    = "RED"      # Overdue or ≤ 1 day
    ORANGE = "ORANGE"   # 2–3 days
    YELLOW = "YELLOW"   # 4–7 days
    GREEN  = "GREEN"    # > 7 days


class NetworkType(str, Enum):
    PAYPAL     = "PAYPAL"
    VISA       = "VISA"
    MASTERCARD = "MASTERCARD"
    AMEX       = "AMEX"
    DISCOVER   = "DISCOVER"


class DisputePhase(str, Enum):
    OPEN         = "OPEN"          # Dispute opened, response pending
    UNDER_REVIEW = "UNDER_REVIEW"  # Evidence submitted, PayPal reviewing
    RULING_ISSUED= "RULING_ISSUED" # Ruling given, appeal window open
    APPEALING    = "APPEALING"     # Appeal filed, awaiting decision
    CLOSED       = "CLOSED"        # Final disposition


@dataclass
class DeadlineItem:
    """One specific deadline."""
    deadline_type: DeadlineType
    days_remaining: int           # Negative = overdue
    urgency: DeadlineUrgency
    description: str
    action_required: str
    is_overdue: bool
    network: Optional[NetworkType] = None


@dataclass
class DeadlineInput:
    """Input for deadline computation."""
    dispute_id: str
    network: NetworkType
    phase: DisputePhase

    # Time context (days since/until events)
    days_since_dispute_opened: int
    days_since_evidence_submitted: int = -1  # -1 = not yet submitted
    days_since_ruling: int = -1              # -1 = no ruling yet
    days_since_appeal_filed: int = -1        # -1 = no appeal

    # Claim context
    claim_type: str = "INR"      # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION / DUPLICATE
    is_ce30_notified: bool = False
    days_since_ce30_notification: int = -1

    # Merchant context
    has_responded: bool = False
    has_submitted_evidence: bool = False
    has_filed_appeal: bool = False

    # Optional extensions
    response_extended: bool = False   # Visa allows extension to 45 days


# Base response deadlines in days
_RESPONSE_DEADLINES: dict[NetworkType, int] = {
    NetworkType.PAYPAL:     10,
    NetworkType.VISA:       20,
    NetworkType.MASTERCARD: 45,
    NetworkType.AMEX:       20,
    NetworkType.DISCOVER:   20,
}

_APPEAL_DEADLINES: dict[NetworkType, int] = {
    NetworkType.PAYPAL:     10,
    NetworkType.VISA:       45,
    NetworkType.MASTERCARD: 45,
    NetworkType.AMEX:       45,
    NetworkType.DISCOVER:   45,
}


def _urgency(days: int) -> DeadlineUrgency:
    if days <= 1:
        return DeadlineUrgency.RED
    if days <= 3:
        return DeadlineUrgency.ORANGE
    if days <= 7:
        return DeadlineUrgency.YELLOW
    return DeadlineUrgency.GREEN


def _merchant_response_deadline(inp: DeadlineInput) -> Optional[DeadlineItem]:
    if inp.has_responded or inp.phase not in (DisputePhase.OPEN,):
        return None
    base = _RESPONSE_DEADLINES[inp.network]
    if inp.response_extended and inp.network == NetworkType.VISA:
        base = 45
    days_remaining = base - inp.days_since_dispute_opened
    return DeadlineItem(
        deadline_type=DeadlineType.MERCHANT_RESPONSE,
        days_remaining=days_remaining,
        urgency=_urgency(days_remaining),
        description=f"{inp.network.value} merchant response deadline ({base}-day window)",
        action_required="Submit initial response with supporting evidence",
        is_overdue=days_remaining < 0,
        network=inp.network,
    )


def _evidence_submission_deadline(inp: DeadlineInput) -> Optional[DeadlineItem]:
    if inp.has_submitted_evidence or inp.phase not in (DisputePhase.OPEN, DisputePhase.UNDER_REVIEW):
        return None
    base = _RESPONSE_DEADLINES[inp.network]
    if inp.response_extended and inp.network == NetworkType.VISA:
        base = 45
    # Evidence should be submitted with or shortly after the response
    days_remaining = base - inp.days_since_dispute_opened
    return DeadlineItem(
        deadline_type=DeadlineType.EVIDENCE_SUBMISSION,
        days_remaining=days_remaining,
        urgency=_urgency(days_remaining),
        description="Evidence package submission deadline",
        action_required="Compile and upload complete evidence package (tracking, photos, logs)",
        is_overdue=days_remaining < 0,
        network=inp.network,
    )


def _appeal_deadline(inp: DeadlineInput) -> Optional[DeadlineItem]:
    if inp.days_since_ruling < 0 or inp.has_filed_appeal:
        return None
    if inp.phase not in (DisputePhase.RULING_ISSUED,):
        return None
    base = _APPEAL_DEADLINES[inp.network]
    days_remaining = base - inp.days_since_ruling
    return DeadlineItem(
        deadline_type=(DeadlineType.APPEAL_PAYPAL if inp.network == NetworkType.PAYPAL
                        else DeadlineType.APPEAL_NETWORK),
        days_remaining=days_remaining,
        urgency=_urgency(days_remaining),
        description=f"{inp.network.value} appeal deadline after ruling ({base}-day window)",
        action_required="File appeal with updated evidence if merchant lost the dispute",
        is_overdue=days_remaining < 0,
        network=inp.network,
    )


def _ce30_deadline(inp: DeadlineInput) -> Optional[DeadlineItem]:
    if not inp.is_ce30_notified or inp.days_since_ce30_notification < 0:
        return None
    days_remaining = 20 - inp.days_since_ce30_notification
    return DeadlineItem(
        deadline_type=DeadlineType.CE30_REBUTTAL,
        days_remaining=days_remaining,
        urgency=_urgency(days_remaining),
        description="CE3.0 Compelling Evidence rebuttal deadline (20 days from notification)",
        action_required="Submit CE3.0 package: 2+ prior non-disputed transactions to same buyer",
        is_overdue=days_remaining < 0,
        network=NetworkType.VISA,
    )


def _buyer_escalation_deadline(inp: DeadlineInput) -> Optional[DeadlineItem]:
    if inp.phase != DisputePhase.OPEN or inp.network != NetworkType.PAYPAL:
        return None
    days_remaining = 20 - inp.days_since_dispute_opened   # Buyer has ~20 days to escalate
    if days_remaining <= 0:
        return None   # Escalation window already passed
    return DeadlineItem(
        deadline_type=DeadlineType.BUYER_ESCALATION,
        days_remaining=days_remaining,
        urgency=_urgency(days_remaining),
        description="Buyer escalation window — buyer can escalate to PayPal claim until day 20",
        action_required="Resolve directly with buyer before escalation if possible",
        is_overdue=False,
        network=NetworkType.PAYPAL,
    )


@dataclass
class DeadlineReport:
    """Complete deadline schedule for a dispute."""
    dispute_id: str
    network: NetworkType
    phase: DisputePhase

    # All applicable deadlines
    deadlines: list[DeadlineItem]

    # Priority summary
    most_urgent: Optional[DeadlineItem]   # The deadline needing immediate attention
    overdue_count: int
    red_count: int
    orange_count: int

    # Time context
    days_since_opened: int
    days_since_ruling: int   # -1 if no ruling

    # Guidance
    immediate_action: str
    schedule: list[str]     # Ordered list of upcoming actions
    overall_urgency: DeadlineUrgency

    summary: str


def compute_deadlines(inp: DeadlineInput) -> DeadlineReport:
    """
    Compute all relevant deadlines and generate a prioritized action schedule.
    """
    computed: list[DeadlineItem] = []

    for fn in (_merchant_response_deadline,
               _evidence_submission_deadline,
               _appeal_deadline,
               _ce30_deadline,
               _buyer_escalation_deadline):
        item = fn(inp)
        if item is not None:
            computed.append(item)

    # Sort: overdue first, then by days remaining ascending
    computed.sort(key=lambda d: (0 if d.is_overdue else 1, d.days_remaining))

    overdue_count = sum(1 for d in computed if d.is_overdue)
    red_count     = sum(1 for d in computed if d.urgency == DeadlineUrgency.RED)
    orange_count  = sum(1 for d in computed if d.urgency == DeadlineUrgency.ORANGE)

    most_urgent = computed[0] if computed else None

    overall_urgency = (DeadlineUrgency.RED    if red_count > 0
                       else DeadlineUrgency.ORANGE if orange_count > 0
                       else DeadlineUrgency.YELLOW if any(d.urgency == DeadlineUrgency.YELLOW for d in computed)
                       else DeadlineUrgency.GREEN)

    if not computed:
        immediate_action = "No pending deadlines detected — verify dispute phase is correct"
    elif overdue_count > 0:
        immediate_action = (f"OVERDUE: {overdue_count} deadline(s) missed — "
                            f"contact PayPal support immediately; {computed[0].action_required}")
    elif most_urgent and most_urgent.urgency == DeadlineUrgency.RED:
        immediate_action = f"URGENT (≤1 day): {most_urgent.action_required}"
    elif most_urgent and most_urgent.urgency == DeadlineUrgency.ORANGE:
        immediate_action = f"ACT NOW ({most_urgent.days_remaining}d): {most_urgent.action_required}"
    else:
        immediate_action = (f"Next: {most_urgent.action_required} "
                            f"({most_urgent.days_remaining}d remaining)")

    schedule = [
        f"[{d.urgency.value}] {d.days_remaining}d — {d.description}: {d.action_required}"
        for d in computed
    ]

    summary = (
        f"Deadlines {inp.dispute_id} [{inp.network.value}/{inp.phase.value}]: "
        f"total={len(computed)} | overdue={overdue_count} | "
        f"red={red_count} | orange={orange_count} | "
        f"overall={overall_urgency.value}"
    )

    return DeadlineReport(
        dispute_id=inp.dispute_id,
        network=inp.network,
        phase=inp.phase,
        deadlines=computed,
        most_urgent=most_urgent,
        overdue_count=overdue_count,
        red_count=red_count,
        orange_count=orange_count,
        days_since_opened=inp.days_since_dispute_opened,
        days_since_ruling=inp.days_since_ruling,
        immediate_action=immediate_action,
        schedule=schedule,
        overall_urgency=overall_urgency,
        summary=summary,
    )
