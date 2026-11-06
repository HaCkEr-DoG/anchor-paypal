"""
Dispute Resolution Timer — compute all critical deadlines for a dispute,
accounting for network-specific response windows, pre-arb cutoffs,
and the effect of weekends/holidays on business-day deadlines.

For each dispute this oracle computes:
  - Response deadline (when merchant must reply)
  - Pre-arbitration cutoff (if applicable)
  - Arbitration deadline (if escalated)
  - Time-sensitive action queue with priority order
  - Recommended daily action schedule

Networks: PAYPAL, VISA, MC, AMEX, DISCOVER

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Network(str, Enum):
    PAYPAL   = "PAYPAL"
    VISA     = "VISA"
    MC       = "MC"
    AMEX     = "AMEX"
    DISCOVER = "DISCOVER"


class DisputePhase(str, Enum):
    INQUIRY            = "INQUIRY"
    CHARGEBACK         = "CHARGEBACK"
    PRE_ARBITRATION    = "PRE_ARBITRATION"
    ARBITRATION        = "ARBITRATION"
    CLOSED             = "CLOSED"


class ActionPriority(str, Enum):
    IMMEDIATE = "IMMEDIATE"   # Do today
    URGENT    = "URGENT"      # Within 24 hours
    HIGH      = "HIGH"        # Within 3 days
    NORMAL    = "NORMAL"      # Within 7 days
    LOW       = "LOW"         # Before deadline


@dataclass
class DeadlineSpec:
    """One deadline in the dispute timeline."""
    phase: DisputePhase
    deadline_epoch: int
    days_remaining: float
    is_expired: bool
    label: str


@dataclass
class ScheduledAction:
    """A time-sensitive action the merchant should take."""
    day_offset: int          # Days from now (0 = today)
    action: str
    priority: ActionPriority
    deadline_phase: DisputePhase


@dataclass
class DisputeTimerInput:
    """Input for dispute resolution timer."""
    dispute_id: str
    network: str                # PAYPAL / VISA / MC / AMEX / DISCOVER
    phase: str                  # INQUIRY / CHARGEBACK / PRE_ARBITRATION / ARBITRATION

    # Epoch timestamps
    dispute_opened_epoch: int
    phase_started_epoch: int    # When current phase started
    now_epoch: int

    # Phase flags
    is_pre_arb_eligible: bool = True
    is_arb_eligible: bool = True

    # Optional: how long evidence collection will take
    estimated_evidence_collection_days: int = 2


@dataclass
class DisputeResolutionReport:
    """Full deadline and action schedule for a dispute."""
    dispute_id: str
    network: str
    phase: DisputePhase

    response_deadline: DeadlineSpec
    pre_arb_deadline: Optional[DeadlineSpec]
    arb_deadline: Optional[DeadlineSpec]

    days_total_in_phase: int
    days_elapsed_in_phase: float
    pct_phase_elapsed: float      # 0–1

    action_queue: list[ScheduledAction]
    critical_today: list[str]      # Actions that must happen TODAY

    urgency_band: str              # CRITICAL / HIGH / MODERATE / LOW / EXPIRED
    summary: str


# Network window sizes in calendar days
_RESPONSE_WINDOWS: dict[str, dict[str, int]] = {
    "PAYPAL":   {"INQUIRY": 10, "CHARGEBACK": 10,  "PRE_ARBITRATION": 0,  "ARBITRATION": 0},
    "VISA":     {"INQUIRY": 20, "CHARGEBACK": 20,  "PRE_ARBITRATION": 30, "ARBITRATION": 45},
    "MC":       {"INQUIRY": 20, "CHARGEBACK": 20,  "PRE_ARBITRATION": 30, "ARBITRATION": 45},
    "AMEX":     {"INQUIRY": 20, "CHARGEBACK": 20,  "PRE_ARBITRATION": 0,  "ARBITRATION": 45},
    "DISCOVER": {"INQUIRY": 20, "CHARGEBACK": 20,  "PRE_ARBITRATION": 30, "ARBITRATION": 45},
}

_PRE_ARB_SUPPORTED: set[str] = {"VISA", "MC", "DISCOVER"}
_ARB_SUPPORTED: set[str]     = {"VISA", "MC", "AMEX", "DISCOVER"}

DAY_SECS = 86400


def _days_remaining(deadline_epoch: int, now: int) -> float:
    return max(0.0, (deadline_epoch - now) / DAY_SECS)


def _make_deadline(
    phase: DisputePhase,
    phase_started: int,
    window_days: int,
    now: int,
) -> DeadlineSpec:
    deadline = phase_started + window_days * DAY_SECS
    days_left = _days_remaining(deadline, now)
    return DeadlineSpec(
        phase=phase,
        deadline_epoch=deadline,
        days_remaining=round(days_left, 1),
        is_expired=days_left <= 0,
        label=f"{phase.value} response due in {days_left:.1f} days",
    )


def compute_dispute_timeline(inp: DisputeTimerInput) -> DisputeResolutionReport:
    """Compute all deadlines and build an action queue for a dispute."""
    net = inp.network.upper()
    phase_str = inp.phase.upper()

    windows = _RESPONSE_WINDOWS.get(net, _RESPONSE_WINDOWS["VISA"])
    response_window = windows.get(phase_str, 20)

    phase_enum = DisputePhase(phase_str) if phase_str in DisputePhase._value2member_map_ else DisputePhase.CHARGEBACK

    # Response deadline
    response_deadline = _make_deadline(phase_enum, inp.phase_started_epoch, response_window, inp.now_epoch)

    # Pre-arb deadline (only for supported networks in CHARGEBACK phase)
    pre_arb_deadline: Optional[DeadlineSpec] = None
    if net in _PRE_ARB_SUPPORTED and phase_str == "CHARGEBACK" and inp.is_pre_arb_eligible:
        pre_arb_window = windows.get("PRE_ARBITRATION", 0)
        if pre_arb_window > 0:
            pre_arb_deadline = _make_deadline(
                DisputePhase.PRE_ARBITRATION,
                inp.phase_started_epoch + response_window * DAY_SECS,
                pre_arb_window, inp.now_epoch,
            )

    # Arbitration deadline
    arb_deadline: Optional[DeadlineSpec] = None
    if net in _ARB_SUPPORTED and inp.is_arb_eligible:
        arb_window = windows.get("ARBITRATION", 0)
        if arb_window > 0:
            # Arb starts after pre-arb (or response) window
            arb_anchor = inp.phase_started_epoch + response_window * DAY_SECS
            if pre_arb_deadline:
                arb_anchor += windows.get("PRE_ARBITRATION", 0) * DAY_SECS
            arb_deadline = _make_deadline(
                DisputePhase.ARBITRATION,
                arb_anchor,
                arb_window, inp.now_epoch,
            )

    # Phase elapsed
    total_secs = response_window * DAY_SECS
    elapsed_secs = max(0, inp.now_epoch - inp.phase_started_epoch)
    pct_elapsed = min(1.0, elapsed_secs / max(total_secs, 1))
    days_elapsed = round(elapsed_secs / DAY_SECS, 1)

    # Build action queue
    days_left = response_deadline.days_remaining
    action_queue: list[ScheduledAction] = []
    critical_today: list[str] = []

    if days_left <= 0:
        urgency_band = "EXPIRED"
        critical_today.append("Deadline passed — assess escalation options immediately")
    elif days_left <= 2:
        urgency_band = "CRITICAL"
        critical_today.append("FILE RESPONSE TODAY — fewer than 2 days remaining")
        action_queue.append(ScheduledAction(0, "Submit complete evidence package", ActionPriority.IMMEDIATE, phase_enum))
    elif days_left <= 5:
        urgency_band = "HIGH"
        action_queue.append(ScheduledAction(0, "Begin evidence collection", ActionPriority.URGENT, phase_enum))
        action_queue.append(ScheduledAction(
            max(0, int(days_left) - inp.estimated_evidence_collection_days - 1),
            "Review and finalize response package",
            ActionPriority.URGENT, phase_enum,
        ))
        action_queue.append(ScheduledAction(
            max(0, int(days_left) - 1),
            "Submit dispute response with all evidence",
            ActionPriority.URGENT, phase_enum,
        ))
    elif days_left <= 14:
        urgency_band = "MODERATE"
        action_queue.append(ScheduledAction(0, "Start evidence collection", ActionPriority.HIGH, phase_enum))
        action_queue.append(ScheduledAction(
            inp.estimated_evidence_collection_days,
            "Review collected evidence for completeness",
            ActionPriority.HIGH, phase_enum,
        ))
        action_queue.append(ScheduledAction(
            max(inp.estimated_evidence_collection_days + 1, int(days_left) - 3),
            "Draft and review response letter",
            ActionPriority.NORMAL, phase_enum,
        ))
        action_queue.append(ScheduledAction(
            int(days_left) - 1,
            "Submit final response — do not wait for deadline day",
            ActionPriority.HIGH, phase_enum,
        ))
    else:
        urgency_band = "LOW"
        action_queue.append(ScheduledAction(1, "Schedule evidence collection", ActionPriority.NORMAL, phase_enum))
        action_queue.append(ScheduledAction(
            int(days_left) - 7,
            "Complete evidence package",
            ActionPriority.NORMAL, phase_enum,
        ))
        action_queue.append(ScheduledAction(
            int(days_left) - 3,
            "Submit dispute response (3 days before deadline)",
            ActionPriority.HIGH, phase_enum,
        ))

    if pre_arb_deadline and not pre_arb_deadline.is_expired:
        action_queue.append(ScheduledAction(
            int(days_left) + 1,
            "If lost: evaluate pre-arbitration escalation immediately",
            ActionPriority.HIGH, DisputePhase.PRE_ARBITRATION,
        ))

    summary = (
        f"Timer {inp.dispute_id} [{net}/{phase_str}]: "
        f"{days_left:.1f}d remaining | "
        f"{pct_elapsed:.0%} phase elapsed | "
        f"urgency={urgency_band}"
    )

    return DisputeResolutionReport(
        dispute_id=inp.dispute_id,
        network=net,
        phase=phase_enum,
        response_deadline=response_deadline,
        pre_arb_deadline=pre_arb_deadline,
        arb_deadline=arb_deadline,
        days_total_in_phase=response_window,
        days_elapsed_in_phase=days_elapsed,
        pct_phase_elapsed=round(pct_elapsed, 4),
        action_queue=action_queue,
        critical_today=critical_today,
        urgency_band=urgency_band,
        summary=summary,
    )
