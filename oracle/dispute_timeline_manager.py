"""
Dispute Timeline Manager — manage and validate all deadline-driven
steps in a dispute's lifecycle, ensuring actions are taken within
network-mandated windows.

Deadline types:
  1. Response deadline — merchant must respond within X days of dispute filing
  2. Evidence submission — final deadline for submitting evidence package
  3. Rebuttal window — time to respond to buyer's rebuttal
  4. Appeal window — time to appeal an adverse ruling
  5. Arbitration escalation — deadline to escalate to card network arbitration

Network windows (calendar days from dispute notification):
  PayPal:    Response=10, Evidence=10, Appeal=10
  Visa:      Response=20, Evidence=20, Rebuttal=10, Appeal=45
  MC:        Response=20, Evidence=20, Rebuttal=10, Appeal=45
  Amex:      Response=20, Evidence=14, Rebuttal=10, Appeal=45
  Discover:  Response=20, Evidence=20, Rebuttal=10, Appeal=45

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Network(str, Enum):
    PAYPAL     = "PAYPAL"
    VISA       = "VISA"
    MASTERCARD = "MASTERCARD"
    AMEX       = "AMEX"
    DISCOVER   = "DISCOVER"


class DeadlineType(str, Enum):
    RESPONSE    = "RESPONSE"
    EVIDENCE    = "EVIDENCE"
    REBUTTAL    = "REBUTTAL"
    APPEAL      = "APPEAL"
    ARBITRATION = "ARBITRATION"


class UrgencyLevel(str, Enum):
    OVERDUE  = "OVERDUE"    # Already expired
    CRITICAL = "CRITICAL"   # ≤ 1 day
    HIGH     = "HIGH"       # ≤ 3 days
    MEDIUM   = "MEDIUM"     # ≤ 7 days
    LOW      = "LOW"        # > 7 days
    NA       = "NA"         # Not applicable (window not started)


class DisputeStage(str, Enum):
    FILED           = "FILED"
    RESPONDED       = "RESPONDED"
    EVIDENCE_FILED  = "EVIDENCE_FILED"
    REBUTTAL_PERIOD = "REBUTTAL_PERIOD"
    RULED           = "RULED"
    APPEALED        = "APPEALED"
    CLOSED          = "CLOSED"


# Network windows in calendar days
_WINDOWS: dict[str, dict[str, Optional[int]]] = {
    "PAYPAL": {
        "RESPONSE":    10,
        "EVIDENCE":    10,
        "REBUTTAL":    None,  # PayPal doesn't have a formal rebuttal
        "APPEAL":      10,
        "ARBITRATION": None,
    },
    "VISA": {
        "RESPONSE":    20,
        "EVIDENCE":    20,
        "REBUTTAL":    10,
        "APPEAL":      45,
        "ARBITRATION": 10,
    },
    "MASTERCARD": {
        "RESPONSE":    20,
        "EVIDENCE":    20,
        "REBUTTAL":    10,
        "APPEAL":      45,
        "ARBITRATION": 10,
    },
    "AMEX": {
        "RESPONSE":    20,
        "EVIDENCE":    14,
        "REBUTTAL":    10,
        "APPEAL":      45,
        "ARBITRATION": 10,
    },
    "DISCOVER": {
        "RESPONSE":    20,
        "EVIDENCE":    20,
        "REBUTTAL":    10,
        "APPEAL":      45,
        "ARBITRATION": 10,
    },
}

_DAY_SECS = 86400


def _days_remaining(deadline_epoch: int, now_epoch: int) -> int:
    delta_secs = deadline_epoch - now_epoch
    return max(int(delta_secs // _DAY_SECS), -999)


def _urgency(days_left: int) -> UrgencyLevel:
    if days_left < 0:
        return UrgencyLevel.OVERDUE
    if days_left <= 1:
        return UrgencyLevel.CRITICAL
    if days_left <= 3:
        return UrgencyLevel.HIGH
    if days_left <= 7:
        return UrgencyLevel.MEDIUM
    return UrgencyLevel.LOW


@dataclass
class DisputeDeadline:
    """A single deadline in the dispute timeline."""
    deadline_type: DeadlineType
    deadline_epoch: int
    days_remaining: int
    urgency: UrgencyLevel
    action_required: str
    is_applicable: bool    # False if network doesn't have this window


@dataclass
class TimelineInput:
    """Input for dispute timeline management."""
    dispute_id: str
    network: Network
    dispute_filed_epoch: int    # When the dispute was notified to merchant
    now_epoch: int              # Current time for deadline calculations

    stage: DisputeStage = DisputeStage.FILED

    # Stage start times (None = not started yet)
    response_submitted_epoch: Optional[int] = None
    evidence_submitted_epoch: Optional[int] = None
    ruling_epoch: Optional[int] = None
    rebuttal_period_started_epoch: Optional[int] = None


@dataclass
class TimelineReport:
    """Full dispute timeline with all deadlines."""
    dispute_id: str
    network: str
    stage: DisputeStage

    deadlines: list[DisputeDeadline]
    next_action_deadline: Optional[DisputeDeadline]   # Most urgent open deadline
    is_any_overdue: bool
    days_since_filing: int
    summary: str


def _make_deadline(
        dtype: DeadlineType,
        start_epoch: Optional[int],
        window_days: Optional[int],
        now_epoch: int,
        action: str) -> Optional[DisputeDeadline]:
    if window_days is None:
        return DisputeDeadline(
            deadline_type=dtype,
            deadline_epoch=0,
            days_remaining=0,
            urgency=UrgencyLevel.NA,
            action_required=action,
            is_applicable=False,
        )
    if start_epoch is None:
        return None   # Window hasn't started
    deadline_epoch = start_epoch + window_days * _DAY_SECS
    days_left = _days_remaining(deadline_epoch, now_epoch)
    return DisputeDeadline(
        deadline_type=dtype,
        deadline_epoch=deadline_epoch,
        days_remaining=days_left,
        urgency=_urgency(days_left),
        action_required=action,
        is_applicable=True,
    )


def build_dispute_timeline(inp: TimelineInput) -> TimelineReport:
    """Build the full deadline timeline for a dispute."""
    network_key = inp.network.value
    windows = _WINDOWS.get(network_key, _WINDOWS["VISA"])

    deadlines: list[DisputeDeadline] = []

    # Response deadline
    d = _make_deadline(
        DeadlineType.RESPONSE,
        inp.dispute_filed_epoch,
        windows["RESPONSE"],
        inp.now_epoch,
        "Submit initial response to dispute")
    if d:
        deadlines.append(d)

    # Evidence deadline (starts when dispute filed)
    d = _make_deadline(
        DeadlineType.EVIDENCE,
        inp.dispute_filed_epoch,
        windows["EVIDENCE"],
        inp.now_epoch,
        "Submit complete evidence package")
    if d:
        deadlines.append(d)

    # Rebuttal deadline
    d = _make_deadline(
        DeadlineType.REBUTTAL,
        inp.rebuttal_period_started_epoch,
        windows["REBUTTAL"],
        inp.now_epoch,
        "Submit rebuttal to buyer's response")
    if d:
        deadlines.append(d)

    # Appeal deadline
    d = _make_deadline(
        DeadlineType.APPEAL,
        inp.ruling_epoch,
        windows["APPEAL"],
        inp.now_epoch,
        "File appeal of adverse ruling")
    if d:
        deadlines.append(d)

    # Arbitration deadline
    d = _make_deadline(
        DeadlineType.ARBITRATION,
        inp.ruling_epoch,
        windows["ARBITRATION"],
        inp.now_epoch,
        "Escalate to network arbitration")
    if d:
        deadlines.append(d)

    # Add NA placeholders for windows that don't exist on this network
    for dtype, action in [
            (DeadlineType.REBUTTAL, "Rebuttal not applicable for this network"),
            (DeadlineType.ARBITRATION, "Arbitration not applicable for this network"),
    ]:
        if not any(dd.deadline_type == dtype for dd in deadlines):
            if windows.get(dtype.value) is None:
                deadlines.append(DisputeDeadline(
                    deadline_type=dtype,
                    deadline_epoch=0,
                    days_remaining=0,
                    urgency=UrgencyLevel.NA,
                    action_required=action,
                    is_applicable=False,
                ))

    # Find next action (most urgent applicable open deadline)
    _urgency_order = [
        UrgencyLevel.OVERDUE, UrgencyLevel.CRITICAL, UrgencyLevel.HIGH,
        UrgencyLevel.MEDIUM, UrgencyLevel.LOW,
    ]
    open_deadlines = [dd for dd in deadlines
                      if dd.is_applicable and dd.urgency != UrgencyLevel.NA]
    next_action = (min(open_deadlines,
                       key=lambda dd: _urgency_order.index(dd.urgency))
                   if open_deadlines else None)

    is_overdue = any(dd.urgency == UrgencyLevel.OVERDUE
                     for dd in deadlines if dd.is_applicable)

    days_since = (inp.now_epoch - inp.dispute_filed_epoch) // _DAY_SECS

    next_type = next_action.deadline_type.value if next_action else "NONE"
    next_days  = next_action.days_remaining if next_action else 0

    summary = (
        f"Timeline {inp.dispute_id}: "
        f"stage={inp.stage.value} | next={next_type}({next_days}d) | "
        f"overdue={is_overdue}"
    )

    return TimelineReport(
        dispute_id=inp.dispute_id,
        network=network_key,
        stage=inp.stage,
        deadlines=deadlines,
        next_action_deadline=next_action,
        is_any_overdue=is_overdue,
        days_since_filing=days_since,
        summary=summary,
    )
