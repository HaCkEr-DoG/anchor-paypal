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
    claim_type: str = "INR"            # test-API alias for dispute_type
    dispute_type: str = field(default="")
    network: str = "PAYPAL"
    # Optional EV context
    fight_cost: float = 15.0
    chargeback_fee: float = 20.0
    # Metadata
    merchant_response_submitted: bool = False
    notes: str = ""

    def __post_init__(self):
        if not self.dispute_type:
            self.dispute_type = self.claim_type
        elif not self.claim_type or self.claim_type == "INR":
            self.claim_type = self.dispute_type


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


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
from enum import Enum as _Enum


class DisputeStatus(str, _Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    UNDER_REVIEW = "UNDER_REVIEW"
    NEEDS_RESPONSE = "NEEDS_RESPONSE"


class WorkAction(str, _Enum):
    FIGHT = "FIGHT"
    ACCEPT = "ACCEPT"
    NEGOTIATE = "NEGOTIATE"
    ESCALATE = "ESCALATE"


BatchDisputeItem = DisputeItem
DisputeForPriority = DisputeItem
BatchPrioritizerInput = BatchInput
PriorityTier = DisputeUrgency


def prioritize_disputes(inp):
    """Alias for prioritize_dispute_batch accepting BatchInput or list[DisputeItem]."""
    if isinstance(inp, list):
        inp = BatchInput(disputes=inp)
    return prioritize_dispute_batch(inp)


# ---------------------------------------------------------------------------
# Wave15 / test-API — new DisputeBatchItem, PriorityBand, WorkAction, prioritize_batch
# ---------------------------------------------------------------------------
from enum import Enum as _Enum3
from dataclasses import dataclass as _dc3, field as _f3


class PriorityBand(str, _Enum3):
    P0_FIRE      = "P0_FIRE"       # days_until_deadline <= 0
    P1_TODAY     = "P1_TODAY"      # <= 2
    P2_THIS_WEEK = "P2_THIS_WEEK"  # <= 7
    P3_QUEUE     = "P3_QUEUE"      # <= 14
    P4_DEFER     = "P4_DEFER"      # > 14


class WorkAction(str, _Enum3):
    SUBMIT_EVIDENCE  = "SUBMIT_EVIDENCE"
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    PROCESS_REFUND   = "PROCESS_REFUND"
    ESCALATE         = "ESCALATE"
    MONITOR          = "MONITOR"


@_dc3
class DisputeBatchItem:
    dispute_id: str
    claim_type: str = "INR"
    dispute_amount: float = 100.0
    days_until_deadline: int = 10
    ev_fight: float = 0.0
    is_hdr_contributing: bool = False
    evidence_ready: bool = False
    evidence_score: float = 50.0
    response_status: str = "NOT_STARTED"
    recommended_action: str = "AUTO_FIGHT"
    is_fraud_flagged: bool = False
    estimated_work_hours: float = 1.0
    buyer_tier: str = "STANDARD_BUYER"


@_dc3
class _QueueEntry:
    dispute_id: str
    priority_band: PriorityBand
    priority_score: float
    work_action: WorkAction
    next_steps: list


@_dc3
class _BatchPriorityReportV2:
    total_disputes: int
    queue: list
    p0_count: int
    p1_count: int
    p2_count: int
    p3_count: int
    p4_count: int
    total_ev_at_stake: float
    hdr_dispute_count: int
    can_complete_today: int
    summary: str


def _band_for_days(days: int) -> PriorityBand:
    if days <= 0:
        return PriorityBand.P0_FIRE
    if days <= 2:
        return PriorityBand.P1_TODAY
    if days <= 7:
        return PriorityBand.P2_THIS_WEEK
    if days <= 14:
        return PriorityBand.P3_QUEUE
    return PriorityBand.P4_DEFER


def _work_action_for(item: DisputeBatchItem) -> WorkAction:
    if item.is_fraud_flagged:
        return WorkAction.ESCALATE
    if item.response_status.upper() in ("SUBMITTED", "RESPONDED"):
        return WorkAction.MONITOR
    if item.recommended_action.upper() in ("AUTO_REFUND", "REFUND"):
        return WorkAction.PROCESS_REFUND
    if not item.evidence_ready or item.evidence_score < 50:
        return WorkAction.COLLECT_EVIDENCE
    return WorkAction.SUBMIT_EVIDENCE


def _next_steps_for(item: DisputeBatchItem, action: WorkAction) -> list:
    if action == WorkAction.ESCALATE:
        return ["Flag for fraud team review", "Freeze dispute response"]
    if action == WorkAction.MONITOR:
        return ["Check dispute status update", "Await network decision"]
    if action == WorkAction.PROCESS_REFUND:
        return ["Issue refund via PayPal dashboard", "Mark dispute resolved"]
    if action == WorkAction.COLLECT_EVIDENCE:
        return ["Gather tracking/proof documents", "Upload to dispute portal"]
    return ["Submit evidence package", f"Deadline in {item.days_until_deadline} days"]


_BAND_ORDER = {
    PriorityBand.P0_FIRE: 0,
    PriorityBand.P1_TODAY: 1,
    PriorityBand.P2_THIS_WEEK: 2,
    PriorityBand.P3_QUEUE: 3,
    PriorityBand.P4_DEFER: 4,
}


def prioritize_batch(items):
    """Prioritize a list of DisputeBatchItem objects (wave15 test-API)."""
    if not items:
        return _BatchPriorityReportV2(
            total_disputes=0, queue=[], p0_count=0, p1_count=0, p2_count=0,
            p3_count=0, p4_count=0, total_ev_at_stake=0.0, hdr_dispute_count=0,
            can_complete_today=0, summary="Batch: 0 disputes",
        )

    entries = []
    for item in items:
        band = _band_for_days(item.days_until_deadline)
        # Priority score: urgency (40) + ev (30) + hdr (10) + evidence (20)
        urgency_pts = {
            PriorityBand.P0_FIRE: 40, PriorityBand.P1_TODAY: 32,
            PriorityBand.P2_THIS_WEEK: 20, PriorityBand.P3_QUEUE: 10,
            PriorityBand.P4_DEFER: 4,
        }[band]
        ev_pts = min(30.0, max(0.0, item.ev_fight / 10.0))
        hdr_pts = 10.0 if item.is_hdr_contributing else 0.0
        ev_score_pts = min(20.0, item.evidence_score / 5.0)
        score = round(urgency_pts + ev_pts + hdr_pts + ev_score_pts, 2)

        action = _work_action_for(item)
        steps = _next_steps_for(item, action)
        entries.append(_QueueEntry(
            dispute_id=item.dispute_id,
            priority_band=band,
            priority_score=score,
            work_action=action,
            next_steps=steps,
        ))

    # Sort: band order first, then score descending within band
    entries.sort(key=lambda e: (_BAND_ORDER[e.priority_band], -e.priority_score))

    band_counts = {b: 0 for b in PriorityBand}
    for e in entries:
        band_counts[e.priority_band] += 1

    total_ev = sum(i.ev_fight for i in items)
    hdr_count = sum(1 for i in items if i.is_hdr_contributing)
    today_count = band_counts[PriorityBand.P0_FIRE] + band_counts[PriorityBand.P1_TODAY]

    summary = (
        f"Batch: {len(items)} disputes | "
        f"P0={band_counts[PriorityBand.P0_FIRE]} P1={band_counts[PriorityBand.P1_TODAY]} | "
        f"ev=${total_ev:+,.0f} | hdr={hdr_count}"
    )

    return _BatchPriorityReportV2(
        total_disputes=len(items),
        queue=entries,
        p0_count=band_counts[PriorityBand.P0_FIRE],
        p1_count=band_counts[PriorityBand.P1_TODAY],
        p2_count=band_counts[PriorityBand.P2_THIS_WEEK],
        p3_count=band_counts[PriorityBand.P3_QUEUE],
        p4_count=band_counts[PriorityBand.P4_DEFER],
        total_ev_at_stake=round(total_ev, 4),
        hdr_dispute_count=hdr_count,
        can_complete_today=today_count,
        summary=summary,
    )


DisputeBatchItem = DisputeBatchItem  # explicit export
PriorityBand = PriorityBand
WorkAction = WorkAction

# ---------------------------------------------------------------------------
# Wave21 / Wave31 test-API — comprehensive prioritize_disputes
# ---------------------------------------------------------------------------
from enum import Enum as _Enum6
from dataclasses import dataclass as _dc6, field as _f6


class PriorityTier(str, _Enum6):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"


# Extend PriorityBand to include wave31 values alongside wave15 values
class PriorityBand(str, _Enum6):  # noqa: F811 — intentional override
    P0_FIRE      = "P0_FIRE"
    P1_TODAY     = "P1_TODAY"
    P1_CRITICAL  = "P1_CRITICAL"
    P2_THIS_WEEK = "P2_THIS_WEEK"
    P2_HIGH      = "P2_HIGH"
    P3_QUEUE     = "P3_QUEUE"
    P3_MEDIUM    = "P3_MEDIUM"
    P4_DEFER     = "P4_DEFER"
    P4_LOW       = "P4_LOW"


@_dc6
class BatchDisputeItem:
    dispute_id: str
    claim_type: str = "INR"
    dispute_amount: float = 100.0
    days_until_deadline: int = 10
    status: object = None          # DisputeStatus enum or str
    p_win: float = 0.50
    is_hdr_contributing: bool = False
    sp_eligible: bool = False
    has_lifecycle_anomaly: bool = False


@_dc6
class DisputeForPriority:
    dispute_id: str
    claim_type: str = "INR"
    amount: float = 100.0
    days_until_deadline: int = 10
    p_win: float = 0.50
    is_hdr_active: bool = False
    sp_eligible: bool = False


@_dc6
class BatchPrioritizerInput:
    merchant_id: str
    disputes: list = _f6(default_factory=list)
    fight_cost_per_dispute: float = 15.0
    chargeback_fee: float = 20.0


@_dc6
class _QueueEntry21:
    rank: int
    dispute_id: str
    priority_tier: PriorityTier
    priority_score: float
    work_estimate_minutes: float
    scoring_breakdown: dict


@_dc6
class _BatchReport21:
    total_disputes: int
    queue: list
    total_amount_at_risk: float
    total_hdr_contributing: int
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int
    immediate_actions: list
    estimated_total_work_hours: float
    summary: str


@_dc6
class _PrioritizedItem31:
    dispute_id: str
    priority_score: float
    priority_band: PriorityBand
    ev_estimate: float


@_dc6
class _BatchReport31:
    merchant_id: str
    prioritized: list
    total_disputes: int
    p1_count: int
    p2_count: int
    p3_count: int
    p4_count: int
    total_ev_if_all_fought: float
    total_amount_at_risk: float
    top_actions: list
    summary: str


_CLAIM_TYPE_SCORE = {"INR": 10.0, "UNAUTHORIZED": 8.0, "SNAD": 6.0}


def _prioritize_list(items: list) -> _BatchReport21:
    """Handle list[BatchDisputeItem] input (wave21)."""
    if not items:
        return _BatchReport21(
            total_disputes=0, queue=[], total_amount_at_risk=0.0,
            total_hdr_contributing=0, critical_count=0, high_count=0,
            medium_count=0, low_count=0, immediate_actions=[],
            estimated_total_work_hours=0.0, summary="Batch: 0 disputes",
        )

    entries = []
    for item in items:
        # Determine tier
        is_critical = item.days_until_deadline < 0 or item.has_lifecycle_anomaly
        if is_critical:
            tier = PriorityTier.CRITICAL
        elif item.days_until_deadline <= 3:
            tier = PriorityTier.HIGH
        elif item.days_until_deadline <= 10:
            tier = PriorityTier.MEDIUM
        else:
            tier = PriorityTier.LOW

        # Scoring breakdown
        deadline_pts = max(0.0, 40.0 - max(0, item.days_until_deadline) * 2.0)
        if item.days_until_deadline < 0:
            deadline_pts = 50.0
        amount_pts = min(20.0, item.dispute_amount / 50.0)
        p_win_pts = item.p_win * 15.0
        ct_pts = _CLAIM_TYPE_SCORE.get(item.claim_type.upper(), 5.0)
        hdr_pts = 8.0 if item.is_hdr_contributing else 0.0
        sp_pts = 5.0 if item.sp_eligible else 0.0
        anomaly_pts = 10.0 if item.has_lifecycle_anomaly else 0.0
        breakdown = {
            "deadline": round(deadline_pts, 2),
            "amount": round(amount_pts, 2),
            "p_win": round(p_win_pts, 2),
            "claim_type": round(ct_pts, 2),
            "hdr": round(hdr_pts, 2),
            "sp": round(sp_pts, 2),
            "anomaly": round(anomaly_pts, 2),
        }
        score = round(sum(breakdown.values()), 2)

        work_mins = 30.0 + (20.0 if item.is_hdr_contributing else 0.0)
        entries.append(_QueueEntry21(
            rank=0,  # will fill in
            dispute_id=item.dispute_id,
            priority_tier=tier,
            priority_score=score,
            work_estimate_minutes=work_mins,
            scoring_breakdown=breakdown,
        ))

    entries.sort(key=lambda e: -e.priority_score)
    for i, e in enumerate(entries):
        e.rank = i + 1

    tier_counts = {t: 0 for t in PriorityTier}
    for e in entries:
        tier_counts[e.priority_tier] += 1

    immediate_actions = []
    for e in entries:
        if e.priority_tier == PriorityTier.CRITICAL:
            orig = next(x for x in items if x.dispute_id == e.dispute_id)
            if orig.days_until_deadline < 0:
                immediate_actions.append(f"{e.dispute_id}: overdue by {-orig.days_until_deadline} days")
            else:
                immediate_actions.append(f"{e.dispute_id}: lifecycle anomaly detected")

    total_amount = sum(i.dispute_amount for i in items)
    hdr_count = sum(1 for i in items if i.is_hdr_contributing)
    total_work_hrs = sum(e.work_estimate_minutes for e in entries) / 60.0

    summary = (
        f"Batch: {len(items)} disputes | "
        f"critical={tier_counts[PriorityTier.CRITICAL]} high={tier_counts[PriorityTier.HIGH]} | "
        f"total=${total_amount:.0f}"
    )
    return _BatchReport21(
        total_disputes=len(items),
        queue=entries,
        total_amount_at_risk=round(total_amount, 2),
        total_hdr_contributing=hdr_count,
        critical_count=tier_counts[PriorityTier.CRITICAL],
        high_count=tier_counts[PriorityTier.HIGH],
        medium_count=tier_counts[PriorityTier.MEDIUM],
        low_count=tier_counts[PriorityTier.LOW],
        immediate_actions=immediate_actions,
        estimated_total_work_hours=round(total_work_hrs, 2),
        summary=summary,
    )


def _prioritize_batch_inp(inp: BatchPrioritizerInput) -> _BatchReport31:
    """Handle BatchPrioritizerInput (wave31)."""
    disputes = inp.disputes
    if not disputes:
        return _BatchReport31(
            merchant_id=inp.merchant_id, prioritized=[], total_disputes=0,
            p1_count=0, p2_count=0, p3_count=0, p4_count=0,
            total_ev_if_all_fought=0.0, total_amount_at_risk=0.0,
            top_actions=["No disputes to prioritize"], summary=f"Merchant {inp.merchant_id}: 0 disputes",
        )

    items = []
    for d in disputes:
        # EV = p_win * amount - fight_cost - (1-p_win) * chargeback_fee
        ev = d.p_win * d.amount - inp.fight_cost_per_dispute - (1 - d.p_win) * inp.chargeback_fee

        # Priority score
        deadline_pts = max(0.0, 40.0 - max(0, d.days_until_deadline) * 1.5)
        if d.days_until_deadline <= 0:
            deadline_pts = 45.0
        amount_pts = min(25.0, d.amount / 40.0)
        pwin_pts = d.p_win * 15.0
        hdr_pts = 8.0 if d.is_hdr_active else 0.0
        sp_pts = 5.0 if d.sp_eligible else 0.0
        score = round(deadline_pts + amount_pts + pwin_pts + hdr_pts + sp_pts, 2)

        # Priority band: use wave31 bands
        if d.days_until_deadline <= 0 or score >= 50:
            band = PriorityBand.P1_CRITICAL
        elif score >= 35:
            band = PriorityBand.P2_HIGH
        elif score >= 20:
            band = PriorityBand.P3_MEDIUM
        else:
            band = PriorityBand.P4_LOW

        items.append(_PrioritizedItem31(
            dispute_id=d.dispute_id,
            priority_score=score,
            priority_band=band,
            ev_estimate=round(ev, 4),
        ))

    items.sort(key=lambda x: -x.priority_score)

    band_counts = {
        "p1": sum(1 for x in items if x.priority_band in (PriorityBand.P1_CRITICAL, PriorityBand.P1_TODAY, PriorityBand.P0_FIRE)),
        "p2": sum(1 for x in items if x.priority_band in (PriorityBand.P2_HIGH, PriorityBand.P2_THIS_WEEK)),
        "p3": sum(1 for x in items if x.priority_band in (PriorityBand.P3_MEDIUM, PriorityBand.P3_QUEUE)),
        "p4": sum(1 for x in items if x.priority_band in (PriorityBand.P4_LOW, PriorityBand.P4_DEFER)),
    }

    total_ev = round(sum(x.ev_estimate for x in items), 4)
    total_amount = round(sum(d.amount for d in disputes), 2)

    top_actions = []
    for x in items[:3]:
        if x.priority_band == PriorityBand.P1_CRITICAL:
            top_actions.append(f"Urgent: respond to {x.dispute_id}")
        else:
            top_actions.append(f"Process {x.dispute_id} (score={x.priority_score:.0f})")

    summary = (
        f"Merchant {inp.merchant_id}: {len(disputes)} disputes | "
        f"P1={band_counts['p1']} P2={band_counts['p2']} | "
        f"total_ev=${total_ev:+,.0f}"
    )

    return _BatchReport31(
        merchant_id=inp.merchant_id,
        prioritized=items,
        total_disputes=len(disputes),
        p1_count=band_counts["p1"],
        p2_count=band_counts["p2"],
        p3_count=band_counts["p3"],
        p4_count=band_counts["p4"],
        total_ev_if_all_fought=total_ev,
        total_amount_at_risk=total_amount,
        top_actions=top_actions,
        summary=summary,
    )


def prioritize_disputes(inp):  # noqa: F811 — intentional override
    """Route to the correct handler based on input type (wave21/wave31/legacy)."""
    if isinstance(inp, list):
        return _prioritize_list(inp)
    if isinstance(inp, BatchPrioritizerInput):
        return _prioritize_batch_inp(inp)
    # Legacy BatchInput path
    from oracle.dispute_batch_prioritizer import prioritize_dispute_batch as _pbatch
    return _pbatch(inp)
