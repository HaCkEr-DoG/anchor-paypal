"""
Dispute Deadline Prioritizer — rank open disputes by urgency × value.

The problem: a merchant has 50 open disputes. All of them need a response.
But the merchant has 2 hours today. Which ones get worked on RIGHT NOW?

Naive answer: sort by deadline date.
Better answer: sort by URGENCY × VALUE × WIN PROBABILITY × CAPACITY FIT.

Key insight: a $20 dispute expiring in 1 day is not more valuable than a
$2,000 dispute expiring in 3 days. This module scores every open dispute
on a composite urgency index and returns a ranked, time-budgeted work queue.

Urgency index formula:
  urgency_score = (deadline_weight × time_pressure)
                + (ev_weight × normalized_ev)
                + (win_weight × win_probability)
                + (capacity_weight × fits_today_flag)

  where deadline_weight=0.40, ev_weight=0.30, win_weight=0.20, capacity_weight=0.10

PayPal benefit:
  - Reduces missed-deadline chargeback losses (bank escalation when merchant
    misses PayPal response window)
  - Signals which merchant segments are chronically overloaded
    (high deadline misses = capacity problem → route to PayPal seller tools)
  - Surfaces disputes where deadline pressure is high but EV is low
    (refund immediately rather than waste response capacity)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import Optional


class Priority(str, Enum):
    P0_CRITICAL = "P0_CRITICAL"   # deadline today or tomorrow — act NOW
    P1_HIGH     = "P1_HIGH"       # 2-3 days — act today
    P2_MEDIUM   = "P2_MEDIUM"     # 4-7 days — plan this week
    P3_LOW      = "P3_LOW"        # 7+ days — schedule
    P4_DEFER    = "P4_DEFER"      # low EV + far deadline — refund and move on


class ActionType(str, Enum):
    RESPOND_NOW        = "RESPOND_NOW"
    GATHER_EVIDENCE    = "GATHER_EVIDENCE"
    ISSUE_REFUND       = "ISSUE_REFUND"
    ESCALATE_TO_TEAM   = "ESCALATE_TO_TEAM"
    SCHEDULE_FOR_LATER = "SCHEDULE_FOR_LATER"


@dataclass
class OpenDispute:
    dispute_id: str
    amount: float
    reason_code: str            # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION
    deadline: date              # PayPal response deadline
    win_probability: float      # 0.0 - 1.0
    expected_value: float       # pre-computed EV (from refund_roi_calculator)
    evidence_ready: bool = False  # does the merchant already have evidence?
    estimated_response_minutes: int = 60  # how long responding will take


@dataclass
class PrioritizedDispute:
    dispute_id: str
    amount: float
    reason_code: str
    deadline: date
    days_remaining: int

    priority: Priority
    urgency_score: float        # 0.0 - 1.0 composite
    deadline_component: float
    ev_component: float
    win_component: float

    recommended_action: ActionType
    action_detail: str
    estimated_minutes: int      # how long this will take

    paypal_signal: str


@dataclass
class PrioritizedQueue:
    merchant_id: str
    total_disputes: int
    total_minutes_required: int
    critical_count: int
    high_count: int
    items: list[PrioritizedDispute]  # sorted by urgency_score desc

    # Time budget analysis
    can_complete_all_today: bool
    minutes_available_today: int

    # Overflow
    deferred_disputes: list[str]  # dispute IDs that should be deferred

    paypal_capacity_signal: str
    summary: str


@dataclass
class PrioritizerInput:
    merchant_id: str
    disputes: list[OpenDispute]
    minutes_available_today: int = 240   # 4 hours default
    reference_date: Optional[date] = None  # defaults to today


# ─── Scoring functions ─────────────────────────────────────────────────────────

def _days_remaining(d: OpenDispute, ref: date) -> int:
    delta = (d.deadline - ref).days
    return max(0, delta)


def _deadline_component(days: int) -> float:
    """Higher score = more urgent deadline."""
    if days == 0:
        return 1.00
    if days == 1:
        return 0.90
    if days <= 2:
        return 0.75
    if days <= 3:
        return 0.60
    if days <= 5:
        return 0.40
    if days <= 7:
        return 0.25
    if days <= 14:
        return 0.10
    return 0.03


def _ev_component(ev: float, max_ev: float) -> float:
    """Normalize EV to 0-1 relative to the batch maximum."""
    if max_ev <= 0:
        return 0.0
    return max(0.0, min(1.0, ev / max_ev))


def _urgency_score(
    deadline_c: float, ev_c: float, win_prob: float,
) -> float:
    score = (
        0.40 * deadline_c
        + 0.30 * ev_c
        + 0.20 * win_prob
        + 0.10 * (1.0 if deadline_c > 0.5 else 0.0)  # capacity bonus for P0/P1
    )
    return round(min(1.0, score), 4)


def _priority(days: int, urgency: float) -> Priority:
    if days <= 1:
        return Priority.P0_CRITICAL
    if days <= 3 or urgency >= 0.65:
        return Priority.P1_HIGH
    if days <= 7 or urgency >= 0.40:
        return Priority.P2_MEDIUM
    if urgency >= 0.20:
        return Priority.P3_LOW
    return Priority.P4_DEFER


def _action(d: OpenDispute, days: int, priority: Priority) -> tuple[ActionType, str]:
    if priority == Priority.P4_DEFER:
        return (
            ActionType.ISSUE_REFUND,
            f"EV is low and deadline is {days} days away. Issue refund now to free capacity.",
        )
    if days == 0:
        return (
            ActionType.RESPOND_NOW,
            f"DEADLINE TODAY — submit response immediately for {d.dispute_id}.",
        )
    if d.evidence_ready:
        return (
            ActionType.RESPOND_NOW,
            f"Evidence ready. Submit response for {d.dispute_id} to close this dispute.",
        )
    if days <= 2:
        return (
            ActionType.GATHER_EVIDENCE,
            f"{days}d remaining. Gather evidence now — {d.reason_code} disputes need "
            f"{'tracking + POD' if d.reason_code == 'INR' else 'product photos + invoice' if d.reason_code == 'SNAD' else 'transaction records'}.",
        )
    if d.amount >= 500 and d.win_probability >= 0.7:
        return (
            ActionType.ESCALATE_TO_TEAM,
            f"High-value ${d.amount:.0f} dispute with {d.win_probability:.0%} win rate — escalate to dedicated dispute team.",
        )
    return (
        ActionType.SCHEDULE_FOR_LATER,
        f"Schedule response for {d.dispute_id} within {max(1, days - 2)} days.",
    )


# ─── Main function ─────────────────────────────────────────────────────────────

def prioritize_disputes(inp: PrioritizerInput) -> PrioritizedQueue:
    """
    Rank all open disputes by composite urgency score and return a
    time-budgeted work queue with per-dispute recommended actions.
    """
    ref = inp.reference_date or date.today()
    disputes = inp.disputes

    if not disputes:
        return PrioritizedQueue(
            merchant_id=inp.merchant_id,
            total_disputes=0,
            total_minutes_required=0,
            critical_count=0,
            high_count=0,
            items=[],
            can_complete_all_today=True,
            minutes_available_today=inp.minutes_available_today,
            deferred_disputes=[],
            paypal_capacity_signal=f"Merchant {inp.merchant_id} has no open disputes.",
            summary=f"Queue {inp.merchant_id}: 0 open disputes",
        )

    # Normalize EV across the batch
    max_ev = max((d.expected_value for d in disputes), default=1.0)
    if max_ev <= 0:
        max_ev = 1.0

    scored: list[PrioritizedDispute] = []
    for d in disputes:
        days = _days_remaining(d, ref)
        dc = _deadline_component(days)
        ec = _ev_component(d.expected_value, max_ev)
        urgency = _urgency_score(dc, ec, d.win_probability)
        prio = _priority(days, urgency)
        action, detail = _action(d, days, prio)

        ps = (
            f"Dispute {d.dispute_id}: {prio.value} priority, "
            f"{days}d remaining, EV={d.expected_value:.2f}, urgency={urgency:.3f}"
        )

        scored.append(PrioritizedDispute(
            dispute_id=d.dispute_id,
            amount=d.amount,
            reason_code=d.reason_code,
            deadline=d.deadline,
            days_remaining=days,
            priority=prio,
            urgency_score=urgency,
            deadline_component=dc,
            ev_component=ec,
            win_component=d.win_probability,
            recommended_action=action,
            action_detail=detail,
            estimated_minutes=d.estimated_response_minutes,
            paypal_signal=ps,
        ))

    # Sort by urgency desc, then by days remaining asc as tiebreaker
    scored.sort(key=lambda x: (-x.urgency_score, x.days_remaining))

    total_mins = sum(x.estimated_minutes for x in scored)
    critical = sum(1 for x in scored if x.priority == Priority.P0_CRITICAL)
    high = sum(1 for x in scored if x.priority == Priority.P1_HIGH)

    # Time-budget deferred list
    deferred = []
    running_mins = 0
    for item in scored:
        if item.priority == Priority.P4_DEFER:
            deferred.append(item.dispute_id)
        elif running_mins + item.estimated_minutes > inp.minutes_available_today:
            deferred.append(item.dispute_id)
        else:
            running_mins += item.estimated_minutes

    can_complete = total_mins <= inp.minutes_available_today

    # PayPal capacity signal
    if critical > 0:
        ps = (
            f"ALERT: Merchant {inp.merchant_id} has {critical} P0 disputes expiring today/tomorrow. "
            f"Total response time: {total_mins}min vs {inp.minutes_available_today}min available. "
            f"{'On track.' if can_complete else 'CAPACITY OVERLOAD — recommend proactive refund of low-EV disputes.'}"
        )
    elif not can_complete:
        ps = (
            f"Merchant {inp.merchant_id} capacity overloaded: {total_mins}min needed, "
            f"{inp.minutes_available_today}min available. {len(deferred)} disputes will overflow. "
            f"PayPal risk: potential missed-deadline chargebacks."
        )
    else:
        ps = (
            f"Merchant {inp.merchant_id}: {len(disputes)} disputes, {total_mins}min required, "
            f"within daily capacity. Queue is manageable."
        )

    summary = (
        f"Queue {inp.merchant_id}: {len(disputes)} disputes | "
        f"P0={critical} P1={high} | "
        f"total={total_mins}min capacity={inp.minutes_available_today}min | "
        f"deferred={len(deferred)}"
    )

    return PrioritizedQueue(
        merchant_id=inp.merchant_id,
        total_disputes=len(disputes),
        total_minutes_required=total_mins,
        critical_count=critical,
        high_count=high,
        items=scored,
        can_complete_all_today=can_complete,
        minutes_available_today=inp.minutes_available_today,
        deferred_disputes=deferred,
        paypal_capacity_signal=ps,
        summary=summary,
    )
