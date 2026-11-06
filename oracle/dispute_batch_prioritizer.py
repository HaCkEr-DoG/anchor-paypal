"""
Dispute Batch Prioritizer — daily work queue optimizer for dispute managers.

A merchant with 100+ open disputes cannot respond to all equally. This module
takes a portfolio of disputes and produces a daily priority-ordered work queue
that maximizes:
  1. Financial impact (EV saved vs lost)
  2. Deadline urgency (avoid missing response windows)
  3. HDR impact (minimize disputes contributing to elevated rate)
  4. Effort efficiency (highest ROI per hour of work)

Priority score formula (0-100):
  base_score = deadline_urgency_weight × (1 / max(days_remaining, 0.1)) × 10
  ev_component = clamp(ev_fight, -50, 100) / 100 × 20
  hdr_component = is_hdr_contributing × 15
  amount_component = log(amount + 1) / log(10001) × 10
  evidence_component = evidence_readiness_boost × 5

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class WorkAction(str, Enum):
    SUBMIT_EVIDENCE     = "SUBMIT_EVIDENCE"      # Has evidence, submit to PayPal now
    COLLECT_EVIDENCE    = "COLLECT_EVIDENCE"     # Needs evidence gathering first
    PROCESS_REFUND      = "PROCESS_REFUND"       # Decision made: refund
    INITIATE_SETTLEMENT = "INITIATE_SETTLEMENT"  # Contact buyer before deadline
    ESCALATE            = "ESCALATE"             # Needs human or fraud team
    MONITOR             = "MONITOR"              # No action now, check back later
    AUTO_ROUTE          = "AUTO_ROUTE"           # Safe for automatic routing


class PriorityBand(str, Enum):
    P0_FIRE   = "P0_FIRE"    # Act within hours — deadline < 1 day or critical issue
    P1_TODAY  = "P1_TODAY"   # Complete today — deadline 1-3 days
    P2_THIS_WEEK = "P2_THIS_WEEK"  # Schedule this week — deadline 4-7 days
    P3_QUEUE  = "P3_QUEUE"   # Normal queue — deadline > 7 days
    P4_DEFER  = "P4_DEFER"   # Low impact, can defer — resolved or tiny amounts


@dataclass
class DisputeBatchItem:
    """One dispute's summary for batch prioritization."""
    dispute_id: str
    claim_type: str
    dispute_amount: float
    days_until_deadline: int
    ev_fight: float              # Pre-calculated EV (from other oracles)
    is_hdr_contributing: bool
    evidence_ready: bool         # True if evidence has been collected
    evidence_score: float        # 0-100 from evidence_quality_scorer
    response_status: str         # NOT_STARTED / IN_PROGRESS / SUBMITTED / RESOLVED
    recommended_action: str      # From claim_router: AUTO_FIGHT / SETTLE / AUTO_REFUND / etc.
    is_fraud_flagged: bool = False
    estimated_work_hours: float = 1.0
    buyer_tier: str = "STANDARD_BUYER"


@dataclass
class PrioritizedDispute:
    """One dispute with its computed priority score and work plan."""
    dispute_id: str
    priority_score: float          # 0-100
    priority_band: PriorityBand
    work_action: WorkAction
    estimated_work_hours: float

    # Score breakdown
    deadline_component: float
    ev_component: float
    hdr_component: float
    amount_component: float
    evidence_component: float

    # Guidance
    rationale: str
    next_steps: list[str]


@dataclass
class BatchWorkQueue:
    """
    Prioritized work queue for a set of disputes.
    """
    # Ordered list (highest priority first)
    queue: list[PrioritizedDispute]
    total_disputes: int

    # Band counts
    p0_count: int
    p1_count: int
    p2_count: int
    p3_count: int
    p4_count: int

    # Capacity
    total_estimated_hours: float
    can_complete_today: int    # How many P0+P1 items fit in an 8-hour day

    # Financial summary
    total_ev_at_stake: float   # Sum of EV(fight) for all undecided disputes
    hdr_dispute_count: int
    at_risk_amount: float      # Sum of amounts for P0 disputes

    # Guidance
    daily_recommendation: str
    summary: str


def _deadline_component(days: int) -> float:
    """Higher score for fewer days remaining."""
    if days <= 0:
        return 40.0   # Already past deadline — maximum urgency
    return round(min(40.0, 10.0 / max(days, 0.1)), 2)


def _ev_component(ev: float) -> float:
    """Higher score for more positive EV (more worth fighting)."""
    return round(max(0.0, min(20.0, (ev + 50.0) / 150.0 * 20.0)), 2)


def _hdr_component(is_hdr: bool) -> float:
    return 15.0 if is_hdr else 0.0


def _amount_component(amount: float) -> float:
    """Log-scaled amount importance."""
    return round(math.log(amount + 1) / math.log(10001) * 10.0, 2)


def _evidence_component(evidence_score: float, evidence_ready: bool) -> float:
    if evidence_ready and evidence_score >= 65.0:
        return 5.0   # Ready to submit — bonus
    if evidence_ready:
        return 2.0
    return 0.0


def _pick_work_action(item: DisputeBatchItem) -> WorkAction:
    if item.response_status in ("SUBMITTED", "RESOLVED"):
        return WorkAction.MONITOR
    if item.is_fraud_flagged:
        return WorkAction.ESCALATE
    if item.recommended_action == "AUTO_REFUND":
        return WorkAction.PROCESS_REFUND
    if item.recommended_action == "SETTLE":
        return WorkAction.INITIATE_SETTLEMENT
    if item.recommended_action == "ESCALATE":
        return WorkAction.ESCALATE
    if item.recommended_action in ("AUTO_FIGHT", "HUMAN_REVIEW"):
        if item.evidence_ready and item.evidence_score >= 45:
            return WorkAction.SUBMIT_EVIDENCE
        return WorkAction.COLLECT_EVIDENCE
    return WorkAction.AUTO_ROUTE


def _priority_band(score: float, days: int) -> PriorityBand:
    if days <= 0:
        return PriorityBand.P0_FIRE
    if score >= 70 or days <= 1:
        return PriorityBand.P0_FIRE
    if score >= 45 or days <= 3:
        return PriorityBand.P1_TODAY
    if score >= 25 or days <= 7:
        return PriorityBand.P2_THIS_WEEK
    if score >= 10:
        return PriorityBand.P3_QUEUE
    return PriorityBand.P4_DEFER


def _next_steps(item: DisputeBatchItem, action: WorkAction) -> list[str]:
    steps: list[str] = []
    if action == WorkAction.SUBMIT_EVIDENCE:
        steps.append("Log in to PayPal Resolution Center")
        steps.append(f"Submit evidence for dispute {item.dispute_id}")
        steps.append("Save submission confirmation number")
    elif action == WorkAction.COLLECT_EVIDENCE:
        steps.append(f"Gather evidence for {item.claim_type} claim")
        if item.claim_type == "INR":
            steps.append("Export tracking page screenshot showing delivery")
        elif item.claim_type == "SNAD":
            steps.append("Take photos of actual product vs listing")
        steps.append("Score evidence with evidence_quality_scorer before submitting")
    elif action == WorkAction.PROCESS_REFUND:
        steps.append(f"Issue refund of ${item.dispute_amount:.2f}")
        steps.append("Send buyer confirmation message")
        steps.append("Mark dispute as resolved")
    elif action == WorkAction.INITIATE_SETTLEMENT:
        steps.append("Send buyer settlement offer via PayPal messaging")
        steps.append("Set 48-hour deadline on offer")
    elif action == WorkAction.ESCALATE:
        steps.append("Flag dispute for fraud team review")
        steps.append("Document all network signals in case notes")
    elif action == WorkAction.MONITOR:
        steps.append("Check dispute status in 24 hours")
    return steps


def prioritize_batch(
    disputes: list[DisputeBatchItem],
    work_hours_per_day: float = 8.0,
) -> BatchWorkQueue:
    """
    Produce a prioritized work queue from a list of disputes.
    """
    prioritized: list[PrioritizedDispute] = []

    for item in disputes:
        d_comp = _deadline_component(item.days_until_deadline)
        e_comp = _ev_component(item.ev_fight)
        h_comp = _hdr_component(item.is_hdr_contributing)
        a_comp = _amount_component(item.dispute_amount)
        ev_comp = _evidence_component(item.evidence_score, item.evidence_ready)

        score = round(d_comp + e_comp + h_comp + a_comp + ev_comp, 2)
        band = _priority_band(score, item.days_until_deadline)
        action = _pick_work_action(item)
        steps = _next_steps(item, action)

        rationale_parts = []
        if item.days_until_deadline <= 1:
            rationale_parts.append(f"deadline in {item.days_until_deadline}d")
        if item.ev_fight > 0:
            rationale_parts.append(f"EV+${item.ev_fight:.0f}")
        if item.is_hdr_contributing:
            rationale_parts.append("HDR-contributing")
        if item.is_fraud_flagged:
            rationale_parts.append("fraud flagged")
        rationale = "; ".join(rationale_parts) if rationale_parts else "normal priority"

        prioritized.append(PrioritizedDispute(
            dispute_id=item.dispute_id,
            priority_score=score,
            priority_band=band,
            work_action=action,
            estimated_work_hours=item.estimated_work_hours,
            deadline_component=d_comp,
            ev_component=e_comp,
            hdr_component=h_comp,
            amount_component=a_comp,
            evidence_component=ev_comp,
            rationale=rationale,
            next_steps=steps,
        ))

    # Sort: P0 first, then by score descending
    band_order = {PriorityBand.P0_FIRE: 0, PriorityBand.P1_TODAY: 1,
                  PriorityBand.P2_THIS_WEEK: 2, PriorityBand.P3_QUEUE: 3,
                  PriorityBand.P4_DEFER: 4}
    prioritized.sort(key=lambda x: (band_order[x.priority_band], -x.priority_score))

    # Counts
    band_counts = {b: 0 for b in PriorityBand}
    for pd in prioritized:
        band_counts[pd.priority_band] += 1

    # Capacity
    hours_used = 0.0
    can_complete = 0
    p0_p1 = [pd for pd in prioritized if pd.priority_band in (PriorityBand.P0_FIRE, PriorityBand.P1_TODAY)]
    for pd in p0_p1:
        if hours_used + pd.estimated_work_hours <= work_hours_per_day:
            hours_used += pd.estimated_work_hours
            can_complete += 1
        else:
            break

    total_ev = round(sum(item.ev_fight for item in disputes if item.response_status not in ("SUBMITTED", "RESOLVED")), 2)
    hdr_count = sum(1 for item in disputes if item.is_hdr_contributing)
    p0_items = [item for item in disputes if
                _priority_band(_deadline_component(item.days_until_deadline), item.days_until_deadline) == PriorityBand.P0_FIRE]
    at_risk = round(sum(item.dispute_amount for item in p0_items), 2)
    total_hours = round(sum(pd.estimated_work_hours for pd in prioritized), 2)

    # Daily recommendation
    p0 = band_counts[PriorityBand.P0_FIRE]
    p1 = band_counts[PriorityBand.P1_TODAY]
    if p0 > 0:
        rec = f"URGENT: {p0} P0 disputes need immediate action today (deadline < 24h). "
    else:
        rec = "No P0 fires today. "
    if p1 > 0:
        rec += f"Schedule {min(p1, can_complete)} P1 disputes for today."

    summary = (
        f"Batch: {len(disputes)} disputes | "
        f"P0={band_counts[PriorityBand.P0_FIRE]} P1={band_counts[PriorityBand.P1_TODAY]} "
        f"P2={band_counts[PriorityBand.P2_THIS_WEEK]} P3={band_counts[PriorityBand.P3_QUEUE]} "
        f"P4={band_counts[PriorityBand.P4_DEFER]} | "
        f"total_EV=${total_ev:+.0f} | "
        f"can_complete_today={can_complete}"
    )

    return BatchWorkQueue(
        queue=prioritized,
        total_disputes=len(disputes),
        p0_count=band_counts[PriorityBand.P0_FIRE],
        p1_count=band_counts[PriorityBand.P1_TODAY],
        p2_count=band_counts[PriorityBand.P2_THIS_WEEK],
        p3_count=band_counts[PriorityBand.P3_QUEUE],
        p4_count=band_counts[PriorityBand.P4_DEFER],
        total_estimated_hours=total_hours,
        can_complete_today=can_complete,
        total_ev_at_stake=total_ev,
        hdr_dispute_count=hdr_count,
        at_risk_amount=at_risk,
        daily_recommendation=rec,
        summary=summary,
    )
