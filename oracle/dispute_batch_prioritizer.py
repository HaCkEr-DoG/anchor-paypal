"""
Dispute Batch Prioritizer — rank a batch of open disputes by urgency.

When a merchant has many open disputes, working them in the wrong order
leads to missed deadlines and suboptimal outcomes. This module scores and
sorts a batch of disputes into a prioritized work queue.

Scoring factors (each contributes points):
  1. Deadline urgency   — disputes due soonest score highest
  2. Amount             — higher-value disputes score higher
  3. Win probability    — disputes with high p_win prioritized (ROI-positive)
  4. HDR contribution   — disputes contributing to HDR rate score higher
  5. SP eligibility     — disputes with Seller Protection score higher
  6. Claim type         — INR before SNAD (easier to win)
  7. Anomaly flag       — disputes with lifecycle anomalies score highest

Priority tiers:
  CRITICAL  — Act within 2 hours (deadline ≤ 2 days or severe anomaly)
  HIGH      — Act within 1 business day
  MEDIUM    — Act within 3 business days
  LOW       — Act within 1 week

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PriorityTier(str, Enum):
    CRITICAL = "CRITICAL"   # Act within 2h
    HIGH     = "HIGH"       # Act within 1 business day
    MEDIUM   = "MEDIUM"     # Act within 3 business days
    LOW      = "LOW"        # Act within 1 week


class DisputeStatus(str, Enum):
    NEEDS_RESPONSE  = "NEEDS_RESPONSE"
    IN_PROGRESS     = "IN_PROGRESS"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    APPEALING       = "APPEALING"
    NEW             = "NEW"


@dataclass
class BatchDisputeItem:
    """One dispute in the batch for prioritization."""
    dispute_id: str
    claim_type: str              # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION / DUPLICATE
    dispute_amount: float
    days_until_deadline: int     # Days remaining to respond (negative = overdue)
    status: DisputeStatus
    p_win: float                 # Estimated win probability 0.0–1.0
    is_hdr_contributing: bool = True
    sp_eligible: bool = False
    has_lifecycle_anomaly: bool = False
    evidence_ready: bool = False   # Has evidence been collected yet?
    is_high_value: bool = False    # Amount >= $750 (triggers signature requirement)


@dataclass
class PrioritizedDispute:
    """One dispute with its computed priority."""
    dispute_id: str
    claim_type: str
    dispute_amount: float
    days_until_deadline: int
    priority_tier: PriorityTier
    priority_score: float          # Higher = more urgent
    rank: int                      # 1 = most urgent
    action_required: str
    work_estimate_minutes: int     # Estimated work time
    scoring_breakdown: dict        # Component scores


@dataclass
class BatchPriorityReport:
    """
    Prioritized work queue for a batch of disputes.
    """
    total_disputes: int
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int

    # Ordered work queue
    queue: list[PrioritizedDispute]

    # Aggregate metrics
    total_amount_at_risk: float
    total_hdr_contributing: int
    estimated_total_work_hours: float

    # Headline recommendations
    immediate_actions: list[str]   # What to do RIGHT NOW
    summary: str


# ─────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────

def _score_deadline(days: int) -> float:
    """Score deadline urgency. Overdue gets maximum score."""
    if days <= 0:
        return 100.0   # Overdue — maximum urgency
    if days == 1:
        return 85.0
    if days == 2:
        return 70.0
    if days <= 4:
        return 55.0
    if days <= 7:
        return 35.0
    if days <= 14:
        return 15.0
    return 5.0


def _score_amount(amount: float) -> float:
    """Score by dispute amount (diminishing returns above $1000)."""
    if amount >= 1000:
        return 25.0
    if amount >= 500:
        return 20.0
    if amount >= 200:
        return 15.0
    if amount >= 100:
        return 10.0
    if amount >= 50:
        return 7.0
    return 3.0


def _score_win_probability(p_win: float) -> float:
    """Higher win prob = higher score (ROI-positive first)."""
    if p_win >= 0.75:
        return 20.0
    if p_win >= 0.60:
        return 15.0
    if p_win >= 0.45:
        return 10.0
    if p_win >= 0.30:
        return 5.0
    return 2.0   # Low win prob still needs attention; don't skip


def _score_claim_type(claim_type: str) -> float:
    """INR before SNAD; UNAUTHORIZED next (SP might apply)."""
    return {
        "INR":          15.0,
        "UNAUTHORIZED": 12.0,
        "DUPLICATE":    10.0,
        "SUBSCRIPTION": 8.0,
        "SNAD":         6.0,
        "OTHER":        4.0,
    }.get(claim_type.upper(), 4.0)


def _score_dispute(item: BatchDisputeItem) -> tuple[float, dict]:
    s_deadline = _score_deadline(item.days_until_deadline)
    s_amount   = _score_amount(item.dispute_amount)
    s_p_win    = _score_win_probability(item.p_win)
    s_claim    = _score_claim_type(item.claim_type)
    s_hdr      = 8.0 if item.is_hdr_contributing else 0.0
    s_sp       = 5.0 if item.sp_eligible else 0.0
    s_anomaly  = 20.0 if item.has_lifecycle_anomaly else 0.0
    s_evidence = -5.0 if not item.evidence_ready else 0.0  # Penalize if no evidence

    total = (s_deadline + s_amount + s_p_win + s_claim +
             s_hdr + s_sp + s_anomaly + s_evidence)

    breakdown = {
        "deadline":    s_deadline,
        "amount":      s_amount,
        "p_win":       s_p_win,
        "claim_type":  s_claim,
        "hdr":         s_hdr,
        "sp":          s_sp,
        "anomaly":     s_anomaly,
        "evidence":    s_evidence,
    }
    return round(total, 2), breakdown


def _tier(score: float, item: BatchDisputeItem) -> PriorityTier:
    if item.days_until_deadline <= 2 or item.has_lifecycle_anomaly:
        return PriorityTier.CRITICAL
    if score >= 80:
        return PriorityTier.CRITICAL
    if score >= 55:
        return PriorityTier.HIGH
    if score >= 30:
        return PriorityTier.MEDIUM
    return PriorityTier.LOW


def _action_required(item: BatchDisputeItem, tier: PriorityTier) -> str:
    if item.days_until_deadline <= 0:
        return f"OVERDUE by {abs(item.days_until_deadline)}d — submit response immediately"
    if tier == PriorityTier.CRITICAL:
        return f"Submit response within {item.days_until_deadline}d — CRITICAL deadline"
    if not item.evidence_ready:
        return "Collect evidence package; response pending"
    if item.status == DisputeStatus.NEW:
        return "Review and begin evidence collection"
    if item.status == DisputeStatus.AWAITING_REVIEW:
        return "Monitor for PayPal decision"
    if item.status == DisputeStatus.APPEALING:
        return "Track appeal progress"
    return "Complete evidence submission"


def _work_minutes(item: BatchDisputeItem) -> int:
    """Rough estimate of work time in minutes."""
    base = 30 if item.evidence_ready else 60
    if item.claim_type.upper() == "SNAD":
        base += 30   # SNAD needs more documentation
    if item.dispute_amount >= 500:
        base += 15
    if item.has_lifecycle_anomaly:
        base += 15
    return base


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def prioritize_disputes(disputes: list[BatchDisputeItem]) -> BatchPriorityReport:
    """
    Score and rank a batch of disputes into a prioritized work queue.
    """
    scored: list[tuple[float, dict, BatchDisputeItem]] = []
    for d in disputes:
        score, breakdown = _score_dispute(d)
        scored.append((score, breakdown, d))

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)

    queue: list[PrioritizedDispute] = []
    for rank, (score, breakdown, item) in enumerate(scored, start=1):
        tier = _tier(score, item)
        queue.append(PrioritizedDispute(
            dispute_id=item.dispute_id,
            claim_type=item.claim_type,
            dispute_amount=item.dispute_amount,
            days_until_deadline=item.days_until_deadline,
            priority_tier=tier,
            priority_score=score,
            rank=rank,
            action_required=_action_required(item, tier),
            work_estimate_minutes=_work_minutes(item),
            scoring_breakdown=breakdown,
        ))

    critical = [q for q in queue if q.priority_tier == PriorityTier.CRITICAL]
    high     = [q for q in queue if q.priority_tier == PriorityTier.HIGH]
    medium   = [q for q in queue if q.priority_tier == PriorityTier.MEDIUM]
    low      = [q for q in queue if q.priority_tier == PriorityTier.LOW]

    total_at_risk   = round(sum(d.dispute_amount for d in disputes), 2)
    hdr_count       = sum(1 for d in disputes if d.is_hdr_contributing)
    total_work_hrs  = round(sum(q.work_estimate_minutes for q in queue) / 60, 1)

    # Immediate actions
    immediate: list[str] = []
    overdue = [q for q in queue if q.days_until_deadline <= 0]
    if overdue:
        immediate.append(
            f"{len(overdue)} overdue dispute(s): {', '.join(q.dispute_id for q in overdue[:3])}"
            f"{'...' if len(overdue) > 3 else ''} — respond NOW"
        )
    if critical:
        ids = ", ".join(q.dispute_id for q in critical[:3])
        immediate.append(f"{len(critical)} CRITICAL dispute(s) — handle within 2h: {ids}")
    if hdr_count > 0:
        immediate.append(f"{hdr_count} disputes contribute to HDR rate — resolve quickly to protect standing")

    summary = (
        f"Batch of {len(disputes)} disputes | "
        f"CRITICAL={len(critical)} HIGH={len(high)} MED={len(medium)} LOW={len(low)} | "
        f"at_risk=${total_at_risk:.0f} | hdr_contributing={hdr_count} | "
        f"est_work={total_work_hrs}h"
    )

    return BatchPriorityReport(
        total_disputes=len(disputes),
        critical_count=len(critical),
        high_count=len(high),
        medium_count=len(medium),
        low_count=len(low),
        queue=queue,
        total_amount_at_risk=total_at_risk,
        total_hdr_contributing=hdr_count,
        estimated_total_work_hours=total_work_hrs,
        immediate_actions=immediate,
        summary=summary,
    )
