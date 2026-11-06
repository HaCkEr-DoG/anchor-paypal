"""
Dispute Timeline Tracker — model the complete lifecycle of a PayPal dispute
from open to resolution, tracking all milestones and projecting outcomes.

Dispute lifecycle phases:
  1. INQUIRY — buyer contacts merchant directly (optional pre-dispute phase)
  2. OPEN — buyer opens formal dispute with PayPal
  3. UNDER_REVIEW — PayPal is reviewing evidence
  4. AWAITING_MERCHANT — merchant must respond within deadline
  5. AWAITING_BUYER — buyer must respond or accept resolution
  6. RULING_ISSUED — PayPal has issued a determination
  7. APPEALING — party has appealed the ruling
  8. CLOSED — dispute resolved (win/lose/settled)

Key timelines:
  PayPal response window: 10 days from dispute open
  PayPal review: 30–60 days after all evidence submitted
  PayPal ruling appeal: 10 days from ruling
  Card network appeal: 45 days from ruling
  Chargeback resolution: 30–90 days from filing

Milestone tracking:
  - Each phase transition is a milestone with date
  - Missed milestones are tracked and penalized in win probability
  - Upcoming milestones are flagged with urgency levels

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputePhase(str, Enum):
    INQUIRY            = "INQUIRY"
    OPEN               = "OPEN"
    AWAITING_MERCHANT  = "AWAITING_MERCHANT"
    UNDER_REVIEW       = "UNDER_REVIEW"
    AWAITING_BUYER     = "AWAITING_BUYER"
    RULING_ISSUED      = "RULING_ISSUED"
    APPEALING          = "APPEALING"
    CLOSED             = "CLOSED"


class MilestoneStatus(str, Enum):
    COMPLETED  = "COMPLETED"
    PENDING    = "PENDING"
    OVERDUE    = "OVERDUE"
    UPCOMING   = "UPCOMING"
    SKIPPED    = "SKIPPED"


class MilestoneUrgency(str, Enum):
    CRITICAL = "CRITICAL"   # ≤ 1 day
    HIGH     = "HIGH"       # 2–3 days
    MEDIUM   = "MEDIUM"     # 4–7 days
    LOW      = "LOW"        # > 7 days


class NetworkType(str, Enum):
    PAYPAL      = "PAYPAL"
    VISA        = "VISA"
    MASTERCARD  = "MASTERCARD"
    AMEX        = "AMEX"
    DISCOVER    = "DISCOVER"


@dataclass
class Milestone:
    """One tracked milestone in the dispute lifecycle."""
    name: str
    phase: DisputePhase
    deadline_day: int            # Day number from dispute open (day 0)
    status: MilestoneStatus
    days_remaining: int          # Positive = future, negative = overdue
    urgency: Optional[MilestoneUrgency]
    description: str
    is_critical: bool            # Missing this damages the case critically
    completed_on_day: Optional[int] = None


@dataclass
class DisputeTimelineInput:
    """Input for dispute timeline tracking."""
    dispute_id: str
    network: str                 # PAYPAL / VISA / MASTERCARD / AMEX / DISCOVER
    claim_type: str              # INR / SNAD / UNAUTHORIZED
    current_phase: str           # Current DisputePhase value
    day_opened: int = 0          # Always 0 (reference point)
    current_day: int = 3         # Days since dispute opened

    # Completed milestones (day numbers, None = not completed)
    merchant_responded_day: Optional[int] = None
    evidence_submitted_day: Optional[int] = None
    ruling_issued_day: Optional[int] = None
    appeal_filed_day: Optional[int] = None

    # Network-specific
    is_escalated_to_network: bool = False


_RESPONSE_DEADLINES: dict[str, int] = {
    "PAYPAL":     10,
    "VISA":       20,
    "MASTERCARD": 45,
    "AMEX":       20,
    "DISCOVER":   20,
}

_APPEAL_DEADLINES: dict[str, int] = {
    "PAYPAL":     10,   # 10 days after ruling
    "VISA":       45,
    "MASTERCARD": 45,
    "AMEX":       45,
    "DISCOVER":   45,
}


def _urgency_from_days(days: int) -> Optional[MilestoneUrgency]:
    if days < 0:
        return MilestoneUrgency.CRITICAL  # Overdue
    if days <= 1:
        return MilestoneUrgency.CRITICAL
    if days <= 3:
        return MilestoneUrgency.HIGH
    if days <= 7:
        return MilestoneUrgency.MEDIUM
    return MilestoneUrgency.LOW


def _build_milestones(inp: DisputeTimelineInput) -> list[Milestone]:
    milestones: list[Milestone] = []
    network = inp.network.upper()
    phase = inp.current_phase.upper()
    day = inp.current_day

    # 1. Merchant Response
    response_deadline = _RESPONSE_DEADLINES.get(network, 10)
    if inp.merchant_responded_day is not None:
        status = MilestoneStatus.COMPLETED
        days_rem = response_deadline - inp.merchant_responded_day
        urgency = None
    elif day > response_deadline:
        status = MilestoneStatus.OVERDUE
        days_rem = response_deadline - day
        urgency = MilestoneUrgency.CRITICAL
    elif phase in ("OPEN", "AWAITING_MERCHANT"):
        status = MilestoneStatus.PENDING
        days_rem = response_deadline - day
        urgency = _urgency_from_days(days_rem)
    else:
        status = MilestoneStatus.SKIPPED
        days_rem = 0
        urgency = None

    milestones.append(Milestone(
        name="merchant_response",
        phase=DisputePhase.AWAITING_MERCHANT,
        deadline_day=response_deadline,
        status=status,
        days_remaining=days_rem,
        urgency=urgency,
        description=f"Merchant must respond by day {response_deadline}",
        is_critical=True,
        completed_on_day=inp.merchant_responded_day,
    ))

    # 2. Evidence Submission
    evidence_deadline = response_deadline + 5  # 5 days after response window
    if inp.evidence_submitted_day is not None:
        ev_status = MilestoneStatus.COMPLETED
        ev_days_rem = evidence_deadline - inp.evidence_submitted_day
        ev_urgency = None
    elif day > evidence_deadline:
        ev_status = MilestoneStatus.OVERDUE
        ev_days_rem = evidence_deadline - day
        ev_urgency = MilestoneUrgency.CRITICAL
    elif phase in ("OPEN", "AWAITING_MERCHANT", "UNDER_REVIEW"):
        ev_status = MilestoneStatus.PENDING
        ev_days_rem = evidence_deadline - day
        ev_urgency = _urgency_from_days(ev_days_rem)
    else:
        ev_status = MilestoneStatus.SKIPPED
        ev_days_rem = 0
        ev_urgency = None

    milestones.append(Milestone(
        name="evidence_submission",
        phase=DisputePhase.UNDER_REVIEW,
        deadline_day=evidence_deadline,
        status=ev_status,
        days_remaining=ev_days_rem,
        urgency=ev_urgency,
        description=f"All evidence must be submitted by day {evidence_deadline}",
        is_critical=True,
        completed_on_day=inp.evidence_submitted_day,
    ))

    # 3. Ruling
    ruling_expected_day = evidence_deadline + 30
    if inp.ruling_issued_day is not None:
        r_status = MilestoneStatus.COMPLETED
        r_days_rem = 0
        r_urgency = None
    elif day > ruling_expected_day + 15:
        r_status = MilestoneStatus.OVERDUE
        r_days_rem = ruling_expected_day - day
        r_urgency = MilestoneUrgency.MEDIUM
    elif phase == "UNDER_REVIEW":
        r_status = MilestoneStatus.UPCOMING
        r_days_rem = ruling_expected_day - day
        r_urgency = _urgency_from_days(r_days_rem)
    else:
        r_status = MilestoneStatus.PENDING
        r_days_rem = ruling_expected_day - day
        r_urgency = _urgency_from_days(max(0, r_days_rem))

    milestones.append(Milestone(
        name="ruling_expected",
        phase=DisputePhase.RULING_ISSUED,
        deadline_day=ruling_expected_day,
        status=r_status,
        days_remaining=r_days_rem,
        urgency=r_urgency,
        description=f"PayPal ruling expected around day {ruling_expected_day}",
        is_critical=False,
        completed_on_day=inp.ruling_issued_day,
    ))

    # 4. Appeal window (after ruling)
    if inp.ruling_issued_day is not None:
        appeal_deadline = inp.ruling_issued_day + _APPEAL_DEADLINES.get(network, 10)
        if inp.appeal_filed_day is not None:
            ap_status = MilestoneStatus.COMPLETED
            ap_days_rem = 0
            ap_urgency = None
        elif day > appeal_deadline:
            ap_status = MilestoneStatus.OVERDUE
            ap_days_rem = appeal_deadline - day
            ap_urgency = MilestoneUrgency.CRITICAL
        else:
            ap_status = MilestoneStatus.PENDING
            ap_days_rem = appeal_deadline - day
            ap_urgency = _urgency_from_days(ap_days_rem)

        milestones.append(Milestone(
            name="appeal_window",
            phase=DisputePhase.APPEALING,
            deadline_day=appeal_deadline,
            status=ap_status,
            days_remaining=ap_days_rem,
            urgency=ap_urgency,
            description=f"Appeal must be filed by day {appeal_deadline} from dispute open",
            is_critical=True,
            completed_on_day=inp.appeal_filed_day,
        ))

    return milestones


@dataclass
class DisputeTimelineReport:
    """Full dispute timeline with milestone tracking."""
    dispute_id: str
    network: str
    current_phase: DisputePhase
    current_day: int

    milestones: list[Milestone]

    # Summary counts
    completed_milestones: int
    pending_milestones: int
    overdue_milestones: int
    critical_overdue: int           # Overdue critical milestones

    # Projected completion
    estimated_resolution_day: int
    estimated_resolution_range: tuple[int, int]  # (optimistic, pessimistic)

    # Phase progression
    days_in_current_phase: int
    next_expected_phase: Optional[DisputePhase]
    next_phase_in_days: int

    # Guidance
    immediate_actions: list[str]
    summary: str


def track_dispute_timeline(inp: DisputeTimelineInput) -> DisputeTimelineReport:
    """
    Build a complete dispute timeline with milestone tracking and projections.
    """
    milestones = _build_milestones(inp)
    network = inp.network.upper()

    completed = sum(1 for m in milestones if m.status == MilestoneStatus.COMPLETED)
    pending   = sum(1 for m in milestones if m.status == MilestoneStatus.PENDING)
    overdue   = sum(1 for m in milestones if m.status == MilestoneStatus.OVERDUE)
    crit_over = sum(1 for m in milestones
                    if m.status == MilestoneStatus.OVERDUE and m.is_critical)

    # Estimate resolution
    response_d  = _RESPONSE_DEADLINES.get(network, 10)
    evidence_d  = response_d + 5
    review_d    = evidence_d + 30
    appeal_d    = review_d + _APPEAL_DEADLINES.get(network, 10)
    resolution_opt  = review_d + 5     # Optimistic: quick ruling, no appeal
    resolution_pess = appeal_d + 20    # Pessimistic: appeal + delay

    # Next phase logic
    phase = inp.current_phase.upper()
    phase_map = {
        "INQUIRY":           (DisputePhase.OPEN, 0),
        "OPEN":              (DisputePhase.AWAITING_MERCHANT, 0),
        "AWAITING_MERCHANT": (DisputePhase.UNDER_REVIEW, max(0, response_d - inp.current_day)),
        "UNDER_REVIEW":      (DisputePhase.RULING_ISSUED, max(0, evidence_d + 30 - inp.current_day)),
        "AWAITING_BUYER":    (DisputePhase.RULING_ISSUED, 5),
        "RULING_ISSUED":     (DisputePhase.CLOSED, _APPEAL_DEADLINES.get(network, 10)),
        "APPEALING":         (DisputePhase.CLOSED, 20),
        "CLOSED":            (None, 0),
    }
    next_phase_info = phase_map.get(phase, (None, 0))
    next_phase, next_in_days = next_phase_info

    # Days in current phase (approximate from milestones)
    days_in_phase = inp.current_day  # Simplified: days since open

    # Immediate actions
    actions: list[str] = []
    for m in milestones:
        if m.status == MilestoneStatus.OVERDUE and m.is_critical:
            actions.append(f"OVERDUE: {m.description} — act immediately")
        elif m.status in (MilestoneStatus.PENDING,) and m.urgency in (
                MilestoneUrgency.CRITICAL, MilestoneUrgency.HIGH):
            actions.append(f"URGENT ({m.days_remaining}d left): {m.description}")

    if not actions:
        actions.append("No immediate actions — monitor timeline")

    summary = (
        f"Timeline {inp.dispute_id} [{network}]: "
        f"day={inp.current_day} | phase={phase} | "
        f"completed={completed} | overdue={overdue} | "
        f"est_resolution≈day{review_d + 5}"
    )

    return DisputeTimelineReport(
        dispute_id=inp.dispute_id,
        network=inp.network,
        current_phase=DisputePhase(inp.current_phase.upper()),
        current_day=inp.current_day,
        milestones=milestones,
        completed_milestones=completed,
        pending_milestones=pending,
        overdue_milestones=overdue,
        critical_overdue=crit_over,
        estimated_resolution_day=review_d + 5,
        estimated_resolution_range=(resolution_opt, resolution_pess),
        days_in_current_phase=days_in_phase,
        next_expected_phase=next_phase,
        next_phase_in_days=next_in_days,
        immediate_actions=actions,
        summary=summary,
    )
