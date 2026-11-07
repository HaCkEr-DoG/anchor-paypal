"""
Dispute Deadline Prioritizer — ranks open disputes by urgency.

Combines three signals into a single priority score:
  1. Time pressure  — exponential decay on days remaining (weight 0.50)
  2. EV impact      — expected value of fighting (weight 0.30)
  3. Claim type     — UNAUTH > INR > SNAD > OTHER (weight 0.20)

Outputs a ranked list with urgency labels (CRITICAL/HIGH/MEDIUM/LOW)
and tailored action recommendations per dispute.

PayPal benefit:
  - Merchants act on the right disputes first; missed deadlines → auto-losses
  - Earlier evidence submission correlates with better resolution rates
  - Transparent prioritization builds merchant trust in autonomous agents
  - Feeds into daily digest workflows to surface time-sensitive items
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List

# ── Weights ───────────────────────────────────────────────────────────────────
_W_URGENCY = 0.50
_W_EV      = 0.30
_W_TYPE    = 0.20

# EV above this reference saturates the EV component to 1.0
_EV_REFERENCE_USD = 500.0

# Higher weight → earlier claim type gets pushed up the queue
_CLAIM_TYPE_WEIGHTS = {
    "UNAUTH": 1.00,  # unauthorized — usually easier to prove, high buyer-side risk
    "INR":    0.80,  # item not received
    "SNAD":   0.60,  # significantly not as described
    "OTHER":  0.50,
}


# ── Enums ─────────────────────────────────────────────────────────────────────

class UrgencyLabel(str, Enum):
    CRITICAL = "CRITICAL"   # ≤ 1 day remaining
    HIGH     = "HIGH"       # ≤ 3 days remaining
    MEDIUM   = "MEDIUM"     # ≤ 7 days remaining
    LOW      = "LOW"        # > 7 days remaining


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class OpenDispute:
    dispute_id: str
    claim_type: str              # UNAUTH | INR | SNAD | OTHER
    amount_usd: float
    days_until_deadline: int     # 0 = today is the last day
    win_probability: float = 0.50
    evidence_ready: bool = False  # merchant has evidence assembled


@dataclass
class PrioritizedDispute:
    dispute_id: str
    claim_type: str
    amount_usd: float
    days_until_deadline: int
    win_probability: float
    evidence_ready: bool
    priority_score: float
    urgency_label: UrgencyLabel
    action_recommendation: str
    rank: int = 0  # 1-indexed, assigned by prioritize_disputes


@dataclass
class DeadlinePriorityReport:
    disputes: List[PrioritizedDispute] = field(default_factory=list)
    critical_count: int = 0
    high_count: int = 0
    total_ev_at_risk_usd: float = 0.0
    recommendation_summary: str = ""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _urgency_score(days: int) -> float:
    """Exponential decay: 0d→1.0, 3d→0.368, 7d→0.097, 14d→0.009."""
    if days <= 0:
        return 1.0
    return math.exp(-days / 3.0)


def _ev_score(win_prob: float, amount_usd: float) -> float:
    """EV normalized against _EV_REFERENCE_USD; capped at 1.0."""
    return min(1.0, (win_prob * amount_usd) / _EV_REFERENCE_USD)


def _type_weight(claim_type: str) -> float:
    return _CLAIM_TYPE_WEIGHTS.get(claim_type.upper(), _CLAIM_TYPE_WEIGHTS["OTHER"])


def _urgency_label(days: int) -> UrgencyLabel:
    if days <= 1:
        return UrgencyLabel.CRITICAL
    if days <= 3:
        return UrgencyLabel.HIGH
    if days <= 7:
        return UrgencyLabel.MEDIUM
    return UrgencyLabel.LOW


def _action_rec(label: UrgencyLabel, days: int, evidence_ready: bool) -> str:
    if label == UrgencyLabel.CRITICAL:
        when = "today" if days == 0 else "tomorrow"
        evidence_note = "Evidence ready — submit now." if evidence_ready else "Gather and submit evidence today."
        return f"RESPOND IMMEDIATELY — deadline {when}. {evidence_note}"

    if label == UrgencyLabel.HIGH:
        if evidence_ready:
            return f"Submit evidence today — {days} day(s) remaining."
        return f"Gather evidence now — only {days} day(s) until deadline."

    if label == UrgencyLabel.MEDIUM:
        prefix = "" if evidence_ready else "Review evidence and "
        return f"{prefix}Prepare response within the next {days} days."

    # LOW
    evidence_note = "Evidence ready." if evidence_ready else "Begin evidence collection."
    return f"Monitor — {days} days remaining. {evidence_note}"


# ── Public API ────────────────────────────────────────────────────────────────

def prioritize_disputes(disputes: List[OpenDispute]) -> DeadlinePriorityReport:
    """Score and rank open disputes by urgency. Returns a DeadlinePriorityReport."""
    if not disputes:
        return DeadlinePriorityReport(
            recommendation_summary="No open disputes to prioritize.",
        )

    scored: List[PrioritizedDispute] = []
    for d in disputes:
        u = _urgency_score(d.days_until_deadline)
        e = _ev_score(d.win_probability, d.amount_usd)
        t = _type_weight(d.claim_type)
        score = round(_W_URGENCY * u + _W_EV * e + _W_TYPE * t, 4)
        label = _urgency_label(d.days_until_deadline)
        rec = _action_rec(label, d.days_until_deadline, d.evidence_ready)
        scored.append(PrioritizedDispute(
            dispute_id=d.dispute_id,
            claim_type=d.claim_type,
            amount_usd=d.amount_usd,
            days_until_deadline=d.days_until_deadline,
            win_probability=d.win_probability,
            evidence_ready=d.evidence_ready,
            priority_score=score,
            urgency_label=label,
            action_recommendation=rec,
        ))

    # Primary: highest priority_score first; tiebreak: fewest days remaining
    scored.sort(key=lambda x: (-x.priority_score, x.days_until_deadline))

    for i, pd in enumerate(scored):
        pd.rank = i + 1

    critical = sum(1 for pd in scored if pd.urgency_label == UrgencyLabel.CRITICAL)
    high     = sum(1 for pd in scored if pd.urgency_label == UrgencyLabel.HIGH)
    total_ev = round(sum(pd.win_probability * pd.amount_usd for pd in scored), 2)

    if critical > 0:
        summary = (
            f"{critical} CRITICAL dispute(s) require immediate action. "
            f"Total EV at risk: ${total_ev:.2f}."
        )
    elif high > 0:
        summary = (
            f"{high} HIGH-priority dispute(s) need attention today. "
            f"Total EV at risk: ${total_ev:.2f}."
        )
    else:
        summary = (
            f"No critical deadlines. {len(scored)} dispute(s) monitored. "
            f"Total EV at risk: ${total_ev:.2f}."
        )

    return DeadlinePriorityReport(
        disputes=scored,
        critical_count=critical,
        high_count=high,
        total_ev_at_risk_usd=total_ev,
        recommendation_summary=summary,
    )
