"""
Deadline Tracker — multi-dispute deadline management and portfolio risk calendar.

PayPal disputes have hard deadlines (typically 10-20 days from dispute open).
Missing a deadline means automatic loss — no appeal, no reconsideration.
This module answers two questions:

  1. Per-dispute: "Is this deadline at risk given current evidence state?"
  2. Portfolio: "What is the deadline load over the next N days, and which
     days have dangerous concentrations that need extra staffing?"

Key concepts:
  - Response window: PayPal gives merchants ~10 days after dispute opens
  - Submission buffer: submit at least 2 days early (response_optimizer insight)
  - True deadline vs effective deadline: true = PayPal's cutoff;
    effective = true − buffer (the last day to safely submit)
  - Heat bands: GREEN (>7 days effective), YELLOW (4-7), ORANGE (2-3),
    RED (1), CRITICAL (0 or overdue)
  - Concentration risk: >8 disputes due on the same day = staffing risk

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional


class DeadlineHeat(str, Enum):
    CRITICAL = "CRITICAL"   # 0 days or overdue — act NOW or lose
    RED      = "RED"        # 1 day — immediate action today
    ORANGE   = "ORANGE"     # 2-3 days effective
    YELLOW   = "YELLOW"     # 4-7 days effective
    GREEN    = "GREEN"      # > 7 days effective — healthy


class ResponseStatus(str, Enum):
    NOT_STARTED  = "NOT_STARTED"   # Evidence not begun
    IN_PROGRESS  = "IN_PROGRESS"   # Evidence assembled, not submitted
    SUBMITTED    = "SUBMITTED"     # Response submitted to PayPal
    OVERDUE      = "OVERDUE"       # Missed deadline
    RESOLVED     = "RESOLVED"      # Dispute resolved (win or loss)


_SUBMISSION_BUFFER_DAYS = 2    # Days before true deadline to target submission
_CONCENTRATION_THRESHOLD = 8   # Disputes due on same day that triggers staffing alert


@dataclass
class DisputeDeadlineInput:
    """State of one dispute for deadline tracking."""
    dispute_id: str
    true_deadline: date            # PayPal's hard deadline
    opened_date: date
    response_status: ResponseStatus = ResponseStatus.NOT_STARTED
    claim_type: str = "ITEM_NOT_RECEIVED"
    dispute_amount: float = 0.0
    is_hdr_contributing: bool = True
    # Optional: how many hours of work does this dispute require?
    estimated_work_hours: float = 1.0


@dataclass
class DisputeDeadlineStatus:
    """
    Deadline health for one dispute.
    """
    dispute_id: str
    true_deadline: date
    effective_deadline: date       # true_deadline − buffer
    days_to_true: int
    days_to_effective: int
    heat: DeadlineHeat
    response_status: ResponseStatus
    is_at_risk: bool               # Effective deadline ≤ 1 day and not submitted

    claim_type: str
    dispute_amount: float
    is_hdr_contributing: bool

    action_required: str
    priority_rank: int = 0         # Filled by portfolio sorter


@dataclass
class DayLoad:
    """Deadline load on one calendar day."""
    day: date
    dispute_ids: list[str] = field(default_factory=list)
    total_disputes: int = 0
    total_work_hours: float = 0.0
    hdr_disputes: int = 0
    is_concentration_risk: bool = False
    heat_bands: dict[str, int] = field(default_factory=dict)


@dataclass
class DeadlineCalendar:
    """
    Portfolio-level deadline calendar for the next N days.
    """
    statuses: list[DisputeDeadlineStatus]      # Sorted: CRITICAL first
    day_loads: list[DayLoad]                   # Day-by-day load
    today: date

    # Aggregate counts
    critical_count: int
    red_count: int
    orange_count: int
    yellow_count: int
    green_count: int
    overdue_count: int

    at_risk_count: int
    at_risk_amount: float

    concentration_risk_days: list[date]        # Days with dangerous load
    total_work_hours_next_7d: float

    summary: str


def _effective_deadline(true_deadline: date) -> date:
    return true_deadline - timedelta(days=_SUBMISSION_BUFFER_DAYS)


def _heat(days_to_effective: int, status: ResponseStatus) -> DeadlineHeat:
    if status in (ResponseStatus.SUBMITTED, ResponseStatus.RESOLVED):
        return DeadlineHeat.GREEN
    if days_to_effective < 0:
        return DeadlineHeat.CRITICAL     # Overdue
    if days_to_effective == 0:
        return DeadlineHeat.CRITICAL
    if days_to_effective == 1:
        return DeadlineHeat.RED
    if days_to_effective <= 3:
        return DeadlineHeat.ORANGE
    if days_to_effective <= 7:
        return DeadlineHeat.YELLOW
    return DeadlineHeat.GREEN


def _action_required(
    heat: DeadlineHeat,
    status: ResponseStatus,
    dispute_id: str,
    days_to_effective: int,
) -> str:
    if status in (ResponseStatus.SUBMITTED, ResponseStatus.RESOLVED):
        return f"No action — response already {status.value.lower()}"
    if heat == DeadlineHeat.CRITICAL:
        return f"CRITICAL: Submit response TODAY or lose {dispute_id} by default"
    if heat == DeadlineHeat.RED:
        return f"RED: 1 day remaining — submit response today, {dispute_id} is at risk"
    if heat == DeadlineHeat.ORANGE:
        if status == ResponseStatus.NOT_STARTED:
            return f"Assemble evidence immediately — {days_to_effective} effective days left for {dispute_id}"
        return f"Complete and submit response within {days_to_effective} days — {dispute_id}"
    if heat == DeadlineHeat.YELLOW:
        return f"Queue {dispute_id} for response preparation ({days_to_effective} effective days)"
    return f"Monitor {dispute_id} — {days_to_effective} effective days available"


def track_deadline(inp: DisputeDeadlineInput, today: Optional[date] = None) -> DisputeDeadlineStatus:
    """Compute deadline health for one dispute."""
    if today is None:
        today = date.today()

    eff_deadline = _effective_deadline(inp.true_deadline)
    days_true = (inp.true_deadline - today).days
    days_eff = (eff_deadline - today).days

    heat = _heat(days_eff, inp.response_status)
    is_at_risk = (
        days_eff <= 1
        and inp.response_status not in (ResponseStatus.SUBMITTED, ResponseStatus.RESOLVED)
    )

    action = _action_required(heat, inp.response_status, inp.dispute_id, days_eff)

    return DisputeDeadlineStatus(
        dispute_id=inp.dispute_id,
        true_deadline=inp.true_deadline,
        effective_deadline=eff_deadline,
        days_to_true=days_true,
        days_to_effective=days_eff,
        heat=heat,
        response_status=inp.response_status,
        is_at_risk=is_at_risk,
        claim_type=inp.claim_type,
        dispute_amount=inp.dispute_amount,
        is_hdr_contributing=inp.is_hdr_contributing,
        action_required=action,
    )


def _heat_order(h: DeadlineHeat) -> int:
    return {
        DeadlineHeat.CRITICAL: 0,
        DeadlineHeat.RED: 1,
        DeadlineHeat.ORANGE: 2,
        DeadlineHeat.YELLOW: 3,
        DeadlineHeat.GREEN: 4,
    }[h]


def build_deadline_calendar(
    disputes: list[DisputeDeadlineInput],
    today: Optional[date] = None,
    horizon_days: int = 14,
) -> DeadlineCalendar:
    """
    Build a portfolio deadline calendar for the given horizon.
    Disputes sorted by urgency (CRITICAL first).
    """
    if today is None:
        today = date.today()

    statuses = [track_deadline(d, today) for d in disputes]
    # Sort: CRITICAL first, then by effective deadline
    statuses.sort(key=lambda s: (_heat_order(s.heat), s.days_to_effective))
    for rank, s in enumerate(statuses):
        s.priority_rank = rank + 1

    # Count by heat band
    counts = {h: 0 for h in DeadlineHeat}
    for s in statuses:
        counts[s.heat] += 1

    # Build day-by-day load
    day_loads: list[DayLoad] = []
    for offset in range(horizon_days + 1):
        day = today + timedelta(days=offset)
        day_disputes = [
            (d, s) for d, s in zip(disputes, statuses)
            if s.effective_deadline == day
            and s.response_status not in (ResponseStatus.SUBMITTED, ResponseStatus.RESOLVED)
        ]
        band_counts: dict[str, int] = {}
        for _, s in day_disputes:
            band_counts[s.heat.value] = band_counts.get(s.heat.value, 0) + 1
        load = DayLoad(
            day=day,
            dispute_ids=[d.dispute_id for d, _ in day_disputes],
            total_disputes=len(day_disputes),
            total_work_hours=sum(d.estimated_work_hours for d, _ in day_disputes),
            hdr_disputes=sum(1 for d, _ in day_disputes if d.is_hdr_contributing),
            is_concentration_risk=len(day_disputes) >= _CONCENTRATION_THRESHOLD,
            heat_bands=band_counts,
        )
        day_loads.append(load)

    at_risk = [s for s in statuses if s.is_at_risk]
    at_risk_amount = sum(s.dispute_amount for s in at_risk)
    conc_days = [dl.day for dl in day_loads if dl.is_concentration_risk]
    work_7d = sum(
        dl.total_work_hours for dl in day_loads
        if dl.day <= today + timedelta(days=7)
    )

    summary = (
        f"Deadline calendar ({len(disputes)} disputes, {horizon_days}d horizon): "
        f"CRITICAL={counts[DeadlineHeat.CRITICAL]}, "
        f"RED={counts[DeadlineHeat.RED]}, "
        f"ORANGE={counts[DeadlineHeat.ORANGE]}, "
        f"YELLOW={counts[DeadlineHeat.YELLOW]}, "
        f"GREEN={counts[DeadlineHeat.GREEN]} | "
        f"at_risk={len(at_risk)} (${at_risk_amount:.0f}) | "
        f"conc_risk_days={len(conc_days)}"
    )

    return DeadlineCalendar(
        statuses=statuses,
        day_loads=day_loads,
        today=today,
        critical_count=counts[DeadlineHeat.CRITICAL],
        red_count=counts[DeadlineHeat.RED],
        orange_count=counts[DeadlineHeat.ORANGE],
        yellow_count=counts[DeadlineHeat.YELLOW],
        green_count=counts[DeadlineHeat.GREEN],
        overdue_count=sum(
            1 for s in statuses
            if s.days_to_true < 0 and s.response_status not in
            (ResponseStatus.SUBMITTED, ResponseStatus.RESOLVED)
        ),
        at_risk_count=len(at_risk),
        at_risk_amount=round(at_risk_amount, 2),
        concentration_risk_days=conc_days,
        total_work_hours_next_7d=round(work_7d, 1),
        summary=summary,
    )
