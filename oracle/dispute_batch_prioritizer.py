"""
Dispute Batch Prioritizer — rank a batch of disputes by combined urgency,
amount, win probability, HDR impact, seller protection eligibility, and
CE3.0 applicability.

Priority score formula (0–100):
  urgency_pts   (0–40)  — deadline proximity
  amount_pts    (0–30)  — log-scaled transaction amount
  win_pts       (0–20)  — estimated win probability
  hdr_pts       (0–10)  — HDR tier impact
  sp_pts        (0–5)   — Seller Protection eligible
  ce30_pts      (0–5)   — CE3.0 applicable

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeUrgency(str, Enum):
    OVERDUE  = "OVERDUE"    # Already past deadline
    CRITICAL = "CRITICAL"   # ≤ 1 day
    HIGH     = "HIGH"       # ≤ 3 days
    MEDIUM   = "MEDIUM"     # ≤ 7 days
    LOW      = "LOW"        # > 7 days


class HDRTier(str, Enum):
    NORMAL   = "NORMAL"
    ELEVATED = "ELEVATED"   # > 0.50%
    LIMITED  = "LIMITED"    # > 1.00%
    FEE_TIER = "FEE_TIER"   # > 1.50% — $30/dispute fee
    SEVERE   = "SEVERE"     # > 3.00%


@dataclass
class DisputeItem:
    """A single dispute to be prioritized."""
    dispute_id: str
    transaction_amount: float
    days_until_deadline: int            # Negative = overdue
    estimated_win_probability: float    # 0.0–1.0
    hdr_tier: HDRTier = HDRTier.NORMAL
    seller_protection_eligible: bool = False
    ce30_applicable: bool = False
    dispute_type: str = "INR"           # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION
    network: str = "PAYPAL"
    # Optional EV context
    fight_cost: float = 15.0
    chargeback_fee: float = 20.0
    # Metadata
    merchant_response_submitted: bool = False
    notes: str = ""


# ── Scoring sub-functions ────────────────────────────────────────────

def _urgency_points(days: int) -> float:
    if days < 0:
        return 40.0           # Overdue — maximum urgency
    if days <= 1:
        return 36.0
    if days <= 3:
        return 28.0
    if days <= 7:
        return 18.0
    if days <= 14:
        return 10.0
    return 4.0


def _amount_points(amount: float) -> float:
    """Log-scaled: $10→3, $100→12, $1000→20, $10000→27."""
    if amount <= 0:
        return 0.0
    return min(30.0, max(0.0, math.log10(max(amount, 1.0)) * 10.0))


def _win_points(win_prob: float) -> float:
    return round(win_prob * 20.0, 2)


def _hdr_points(tier: HDRTier) -> float:
    return {
        HDRTier.NORMAL:   0.0,
        HDRTier.ELEVATED: 2.0,
        HDRTier.LIMITED:  5.0,
        HDRTier.FEE_TIER: 8.0,
        HDRTier.SEVERE:   10.0,
    }[tier]


def _sp_points(eligible: bool) -> float:
    return 5.0 if eligible else 0.0


def _ce30_points(applicable: bool) -> float:
    return 5.0 if applicable else 0.0


def _compute_score(item: DisputeItem) -> float:
    return (
        _urgency_points(item.days_until_deadline)
        + _amount_points(item.transaction_amount)
        + _win_points(item.estimated_win_probability)
        + _hdr_points(item.hdr_tier)
        + _sp_points(item.seller_protection_eligible)
        + _ce30_points(item.ce30_applicable)
    )


def _urgency_level(days: int) -> DisputeUrgency:
    if days < 0:
        return DisputeUrgency.OVERDUE
    if days <= 1:
        return DisputeUrgency.CRITICAL
    if days <= 3:
        return DisputeUrgency.HIGH
    if days <= 7:
        return DisputeUrgency.MEDIUM
    return DisputeUrgency.LOW


@dataclass
class RankedDispute:
    """A dispute with its computed priority score and breakdown."""
    dispute_id: str
    rank: int                   # 1 = highest priority
    priority_score: float       # 0–100 composite
    urgency: DisputeUrgency
    transaction_amount: float
    estimated_win_probability: float
    hdr_tier: HDRTier
    seller_protection_eligible: bool
    ce30_applicable: bool

    # Score breakdown
    urgency_pts: float
    amount_pts: float
    win_pts: float
    hdr_pts: float
    sp_pts: float
    ce30_pts: float

    rationale: str              # One-line human explanation


@dataclass
class BatchInput:
    """Input for batch prioritization."""
    batch_id: str
    disputes: list[DisputeItem] = field(default_factory=list)
    max_to_return: Optional[int] = None   # None = all


@dataclass
class BatchPriorityReport:
    """Full prioritized batch report."""
    batch_id: str
    total_disputes: int
    ranked: list[RankedDispute]
    overdue_count: int
    critical_count: int
    total_exposure: float       # Sum of all transaction amounts
    expected_value_if_fight_all: float
    summary: str


def prioritize_dispute_batch(inp: BatchInput) -> BatchPriorityReport:
    """Rank a batch of disputes by multi-factor priority score."""
    if not inp.disputes:
        return BatchPriorityReport(
            batch_id=inp.batch_id,
            total_disputes=0,
            ranked=[],
            overdue_count=0,
            critical_count=0,
            total_exposure=0.0,
            expected_value_if_fight_all=0.0,
            summary=f"Batch {inp.batch_id}: 0 disputes",
        )

    scored = [(item, _compute_score(item)) for item in inp.disputes]
    scored.sort(key=lambda x: x[1], reverse=True)

    if inp.max_to_return is not None:
        scored = scored[: inp.max_to_return]

    ranked: list[RankedDispute] = []
    for rank, (item, score) in enumerate(scored, start=1):
        urgency = _urgency_level(item.days_until_deadline)
        u_pts   = _urgency_points(item.days_until_deadline)
        a_pts   = _amount_points(item.transaction_amount)
        w_pts   = _win_points(item.estimated_win_probability)
        h_pts   = _hdr_points(item.hdr_tier)
        s_pts   = _sp_points(item.seller_protection_eligible)
        c_pts   = _ce30_points(item.ce30_applicable)

        parts = []
        if urgency in (DisputeUrgency.OVERDUE, DisputeUrgency.CRITICAL):
            parts.append(f"deadline {urgency.value.lower()}")
        if item.transaction_amount >= 500:
            parts.append(f"high value ${item.transaction_amount:.0f}")
        if item.estimated_win_probability >= 0.70:
            parts.append(f"strong win odds {item.estimated_win_probability:.0%}")
        if item.hdr_tier in (HDRTier.FEE_TIER, HDRTier.SEVERE):
            parts.append(f"HDR {item.hdr_tier.value}")
        if item.seller_protection_eligible:
            parts.append("SP eligible")
        if item.ce30_applicable:
            parts.append("CE3.0 boost")
        rationale = "; ".join(parts) if parts else "standard priority"

        ranked.append(RankedDispute(
            dispute_id=item.dispute_id,
            rank=rank,
            priority_score=round(score, 1),
            urgency=urgency,
            transaction_amount=item.transaction_amount,
            estimated_win_probability=item.estimated_win_probability,
            hdr_tier=item.hdr_tier,
            seller_protection_eligible=item.seller_protection_eligible,
            ce30_applicable=item.ce30_applicable,
            urgency_pts=u_pts,
            amount_pts=round(a_pts, 1),
            win_pts=w_pts,
            hdr_pts=h_pts,
            sp_pts=s_pts,
            ce30_pts=c_pts,
            rationale=rationale,
        ))

    overdue  = sum(1 for r in ranked if r.urgency == DisputeUrgency.OVERDUE)
    critical = sum(1 for r in ranked if r.urgency == DisputeUrgency.CRITICAL)
    exposure = sum(item.transaction_amount for item, _ in scored)
    ev_fight = sum(
        item.estimated_win_probability * item.transaction_amount
        - item.fight_cost
        - (1 - item.estimated_win_probability) * item.chargeback_fee
        for item, _ in scored
    )

    summary = (
        f"Batch {inp.batch_id}: "
        f"{len(ranked)} disputes | "
        f"overdue={overdue} critical={critical} | "
        f"exposure=${exposure:,.0f} | "
        f"ev_fight=${ev_fight:+,.0f}"
    )

    return BatchPriorityReport(
        batch_id=inp.batch_id,
        total_disputes=len(inp.disputes),
        ranked=ranked,
        overdue_count=overdue,
        critical_count=critical,
        total_exposure=round(exposure, 2),
        expected_value_if_fight_all=round(ev_fight, 2),
        summary=summary,
    )
