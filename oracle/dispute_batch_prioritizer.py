"""
Dispute Batch Prioritizer — rank a batch of open disputes by urgency and
expected value impact, so the merchant handles the most important ones first.

Priority scoring:
  P = deadline_urgency + amount_weight + win_probability_bonus + hdr_impact

This module ensures merchants never miss high-value disputes with imminent
deadlines, and focuses effort where EV is highest.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PriorityBand(str, Enum):
    P1_CRITICAL = "P1_CRITICAL"   # Act today
    P2_HIGH     = "P2_HIGH"       # Act within 48h
    P3_MEDIUM   = "P3_MEDIUM"     # Act this week
    P4_LOW      = "P4_LOW"        # Monitor


@dataclass
class DisputeForPriority:
    """One dispute to be prioritized."""
    dispute_id: str
    claim_type: str
    amount: float
    days_until_deadline: int
    p_win: float              # Calibrated win probability
    sp_eligible: bool = False
    is_hdr_active: bool = False   # True if merchant is in PENALIZED tier
    ce30_applicable: bool = False
    has_all_evidence: bool = False


@dataclass
class PrioritizedDispute:
    """One dispute with its computed priority."""
    dispute_id: str
    claim_type: str
    amount: float
    days_until_deadline: int
    priority_score: float
    priority_band: PriorityBand
    ev_estimate: float          # Expected value if fought
    action_summary: str


@dataclass
class BatchPrioritizerInput:
    """Input: a list of open disputes to rank."""
    merchant_id: str
    disputes: list[DisputeForPriority]
    fight_cost_per_dispute: float = 10.0
    chargeback_fee: float = 20.0


def _priority_score(d: DisputeForPriority, fight_cost: float, cb_fee: float) -> float:
    """Compute a priority score 0–100."""

    # 1. Deadline urgency (0–40 pts)
    if d.days_until_deadline <= 0:
        urgency = 40.0
    elif d.days_until_deadline <= 1:
        urgency = 38.0
    elif d.days_until_deadline <= 2:
        urgency = 32.0
    elif d.days_until_deadline <= 3:
        urgency = 25.0
    elif d.days_until_deadline <= 7:
        urgency = 15.0
    else:
        urgency = max(0, 10 - d.days_until_deadline / 3)

    # 2. Amount weight (0–30 pts, logarithmic)
    import math
    amount_pts = min(30.0, math.log10(max(1, d.amount)) / math.log10(1000) * 30)

    # 3. Win probability bonus (0–20 pts)
    win_pts = d.p_win * 20

    # 4. HDR impact (0–10 pts)
    hdr_pts = 10.0 if d.is_hdr_active else 0.0

    # 5. SP bonus (0–5 pts)
    sp_pts = 5.0 if d.sp_eligible else 0.0

    # 6. CE3.0 bonus (0–5 pts)
    ce30_pts = 5.0 if d.ce30_applicable else 0.0

    # 7. Evidence ready bonus (0–5 pts)
    evidence_pts = 5.0 if d.has_all_evidence else 0.0

    return round(urgency + amount_pts + win_pts + hdr_pts + sp_pts + ce30_pts + evidence_pts, 2)


def _ev_estimate(d: DisputeForPriority, fight_cost: float, cb_fee: float) -> float:
    """EV of fighting this dispute."""
    return round(d.p_win * d.amount - fight_cost - (1 - d.p_win) * cb_fee, 2)


def _band(score: float) -> PriorityBand:
    if score >= 60:
        return PriorityBand.P1_CRITICAL
    if score >= 40:
        return PriorityBand.P2_HIGH
    if score >= 20:
        return PriorityBand.P3_MEDIUM
    return PriorityBand.P4_LOW


def _action_summary(d: DisputeForPriority, ev: float, band: PriorityBand) -> str:
    deadline_str = "OVERDUE" if d.days_until_deadline <= 0 else f"{d.days_until_deadline}d"
    ev_str = f"+${ev:.2f}" if ev >= 0 else f"-${abs(ev):.2f}"
    return (
        f"{band.value}: ${d.amount:.0f} {d.claim_type} | "
        f"deadline={deadline_str} | p_win={d.p_win:.0%} | EV={ev_str}"
    )


@dataclass
class BatchPrioritizerReport:
    """Ranked dispute batch."""
    merchant_id: str
    total_disputes: int

    prioritized: list[PrioritizedDispute]  # Sorted highest priority first

    # Band counts
    p1_count: int
    p2_count: int
    p3_count: int
    p4_count: int

    # Financial summary
    total_amount_at_risk: float
    total_positive_ev_disputes: int
    total_ev_if_all_fought: float

    # Guidance
    top_actions: list[str]
    summary: str


def prioritize_disputes(inp: BatchPrioritizerInput) -> BatchPrioritizerReport:
    """
    Rank a batch of disputes by urgency and EV impact.
    """
    prioritized: list[PrioritizedDispute] = []

    for d in inp.disputes:
        score = _priority_score(d, inp.fight_cost_per_dispute, inp.chargeback_fee)
        ev = _ev_estimate(d, inp.fight_cost_per_dispute, inp.chargeback_fee)
        band = _band(score)
        action = _action_summary(d, ev, band)

        prioritized.append(PrioritizedDispute(
            dispute_id=d.dispute_id,
            claim_type=d.claim_type,
            amount=d.amount,
            days_until_deadline=d.days_until_deadline,
            priority_score=score,
            priority_band=band,
            ev_estimate=ev,
            action_summary=action,
        ))

    prioritized.sort(key=lambda x: x.priority_score, reverse=True)

    p1 = sum(1 for p in prioritized if p.priority_band == PriorityBand.P1_CRITICAL)
    p2 = sum(1 for p in prioritized if p.priority_band == PriorityBand.P2_HIGH)
    p3 = sum(1 for p in prioritized if p.priority_band == PriorityBand.P3_MEDIUM)
    p4 = sum(1 for p in prioritized if p.priority_band == PriorityBand.P4_LOW)

    total_amount = sum(p.amount for p in prioritized)
    positive_ev = sum(1 for p in prioritized if p.ev_estimate >= 0)
    total_ev = sum(p.ev_estimate for p in prioritized)

    # Top actions (first 5 disputes)
    top_actions = [p.action_summary for p in prioritized[:5]]

    summary = (
        f"Batch {inp.merchant_id}: "
        f"{len(prioritized)} disputes | "
        f"P1={p1} P2={p2} P3={p3} P4={p4} | "
        f"total=${total_amount:.2f} | total_ev=${total_ev:.2f}"
    )

    return BatchPrioritizerReport(
        merchant_id=inp.merchant_id,
        total_disputes=len(prioritized),
        prioritized=prioritized,
        p1_count=p1,
        p2_count=p2,
        p3_count=p3,
        p4_count=p4,
        total_amount_at_risk=round(total_amount, 2),
        total_positive_ev_disputes=positive_ev,
        total_ev_if_all_fought=round(total_ev, 2),
        top_actions=top_actions,
        summary=summary,
    )
