"""
Chargeback Rate Monitor — compute PayPal's rolling 90-day dispute rate and
flag whether a merchant is approaching, at, or beyond HDR thresholds.

PayPal uses a rolling 90-day window for dispute rate calculations:
  dispute_rate = disputes_opened / transactions_processed

HDR (High Dispute Rate) thresholds:
  < 0.50%  → HEALTHY
  0.50%    → ELEVATED  (warning, no penalty)
  1.00%    → LIMITED   (limits on account)
  1.50%    → PENALIZED ($30/dispute chargeback fee)
  3.00%    → SEVERE    (account at risk of termination)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class HDRStatus(str, Enum):
    HEALTHY   = "HEALTHY"    # < 0.50%
    ELEVATED  = "ELEVATED"   # 0.50% – 0.99%
    LIMITED   = "LIMITED"    # 1.00% – 1.49%
    PENALIZED = "PENALIZED"  # 1.50% – 2.99%
    SEVERE    = "SEVERE"     # ≥ 3.00%


# Thresholds as percentages
_HDR_THRESHOLDS = [
    (0.50, HDRStatus.ELEVATED),
    (1.00, HDRStatus.LIMITED),
    (1.50, HDRStatus.PENALIZED),
    (3.00, HDRStatus.SEVERE),
]

# How many disputes need to close to drop one tier (approximate, for guidance)
_TIER_LABELS = {
    HDRStatus.HEALTHY:   "Green — no action required",
    HDRStatus.ELEVATED:  "Yellow — monitor closely, improve resolution rate",
    HDRStatus.LIMITED:   "Orange — account features restricted",
    HDRStatus.PENALIZED: "Red — $30 fee per chargeback applies",
    HDRStatus.SEVERE:    "Critical — account termination risk",
}


@dataclass
class ChargebackRateInput:
    """Input for chargeback rate monitoring."""
    merchant_id: str

    # Rolling 90-day window totals
    disputes_opened_90d: int       # Disputes opened in last 90 days
    transactions_processed_90d: int  # Total transactions in last 90 days

    # Optional: break down by dispute type for analysis
    inr_disputes: int = 0
    snad_disputes: int = 0
    unauthorized_disputes: int = 0
    other_disputes: int = 0

    # Optional prior period for trend
    disputes_opened_prior_90d: Optional[int] = None
    transactions_processed_prior_90d: Optional[int] = None

    # Monthly projection inputs (optional)
    disputes_this_month: int = 0
    transactions_this_month: int = 0
    days_into_month: int = 0        # For monthly rate projection


def _compute_rate(disputes: int, transactions: int) -> float:
    """Return dispute rate as a decimal (not percent)."""
    if transactions == 0:
        return 0.0
    return disputes / transactions


def _classify_rate(rate_pct: float) -> HDRStatus:
    """Classify dispute rate percentage into HDR status."""
    for threshold_pct, status in reversed(_HDR_THRESHOLDS):
        if rate_pct >= threshold_pct:
            return status
    return HDRStatus.HEALTHY


def _disputes_to_reach_next_tier(
        current_disputes: int,
        transactions: int,
        current_status: HDRStatus) -> Optional[int]:
    """
    How many additional disputes would push into the next worse tier.
    Returns None if already at SEVERE.
    """
    if current_status == HDRStatus.SEVERE:
        return None

    tier_order = [
        HDRStatus.HEALTHY,
        HDRStatus.ELEVATED,
        HDRStatus.LIMITED,
        HDRStatus.PENALIZED,
        HDRStatus.SEVERE,
    ]
    current_idx = tier_order.index(current_status)
    next_tier = tier_order[current_idx + 1]

    # Find threshold for next tier
    threshold_map = {v: k for k, v in _HDR_THRESHOLDS}
    threshold_map[HDRStatus.HEALTHY] = 0.0
    next_pct = dict(_HDR_THRESHOLDS).get(next_tier, 100.0)
    if next_tier == HDRStatus.ELEVATED:
        next_pct = 0.50
    elif next_tier == HDRStatus.LIMITED:
        next_pct = 1.00
    elif next_tier == HDRStatus.PENALIZED:
        next_pct = 1.50
    elif next_tier == HDRStatus.SEVERE:
        next_pct = 3.00

    needed_disputes = int(next_pct / 100.0 * transactions)
    return max(0, needed_disputes - current_disputes)


def _disputes_to_reach_safe(
        current_disputes: int,
        transactions: int) -> int:
    """How many disputes need to age out to get back to HEALTHY."""
    safe_threshold = 0.50 / 100.0
    max_allowed = int(safe_threshold * transactions)
    return max(0, current_disputes - max_allowed)


@dataclass
class DisputeTypeSplit:
    """Breakdown of disputes by type."""
    inr_pct: float
    snad_pct: float
    unauthorized_pct: float
    other_pct: float
    dominant_type: str


@dataclass
class ChargebackRateReport:
    """Full chargeback rate analysis."""
    merchant_id: str

    # Current window
    current_rate_pct: float        # Dispute rate as percentage
    current_status: HDRStatus
    status_label: str

    # Counts
    disputes_90d: int
    transactions_90d: int

    # Trend
    prior_rate_pct: Optional[float]
    rate_trend: Optional[str]       # "IMPROVING" / "WORSENING" / "STABLE"
    rate_delta_pct: Optional[float] # Change vs. prior period

    # Safety margins
    disputes_to_next_tier: Optional[int]   # None if already at SEVERE
    disputes_to_safe: int                   # Disputes that need to age out

    # Fee impact
    monthly_fee_risk: float                 # $30 × projected_chargebacks if PENALIZED
    is_fee_active: bool                     # True if rate ≥ 1.50%

    # Type breakdown
    type_split: Optional[DisputeTypeSplit]

    # Projected rate (if monthly data provided)
    projected_rate_pct: Optional[float]
    projected_status: Optional[HDRStatus]

    # Guidance
    immediate_actions: list[str]
    summary: str


def monitor_chargeback_rate(inp: ChargebackRateInput) -> ChargebackRateReport:
    """
    Compute dispute rate and classify against HDR thresholds.
    """
    current_rate = _compute_rate(inp.disputes_opened_90d, inp.transactions_processed_90d)
    current_rate_pct = round(current_rate * 100, 4)
    current_status = _classify_rate(current_rate_pct)

    # Prior period trend
    prior_rate_pct = None
    rate_trend = None
    rate_delta = None
    if inp.disputes_opened_prior_90d is not None and inp.transactions_processed_prior_90d is not None:
        prior_rate = _compute_rate(
            inp.disputes_opened_prior_90d, inp.transactions_processed_prior_90d)
        prior_rate_pct = round(prior_rate * 100, 4)
        rate_delta = round(current_rate_pct - prior_rate_pct, 4)
        if abs(rate_delta) < 0.05:
            rate_trend = "STABLE"
        elif rate_delta > 0:
            rate_trend = "WORSENING"
        else:
            rate_trend = "IMPROVING"

    # Safety margins
    to_next = _disputes_to_reach_next_tier(
        inp.disputes_opened_90d, inp.transactions_processed_90d, current_status)
    to_safe = _disputes_to_reach_safe(
        inp.disputes_opened_90d, inp.transactions_processed_90d)

    # Fee impact
    is_fee_active = current_rate_pct >= 1.50
    monthly_fee = 0.0
    if is_fee_active and inp.disputes_opened_90d > 0:
        # Approx 30-day chargebacks from 90-day rate
        projected_monthly_disputes = round(inp.disputes_opened_90d / 3)
        monthly_fee = projected_monthly_disputes * 30.0

    # Type split
    type_split = None
    total_typed = inp.inr_disputes + inp.snad_disputes + inp.unauthorized_disputes + inp.other_disputes
    if total_typed > 0:
        inr_pct = round(inp.inr_disputes / total_typed * 100, 1)
        snad_pct = round(inp.snad_disputes / total_typed * 100, 1)
        unauth_pct = round(inp.unauthorized_disputes / total_typed * 100, 1)
        other_pct = round(inp.other_disputes / total_typed * 100, 1)
        dominant_map = {
            "INR": inp.inr_disputes,
            "SNAD": inp.snad_disputes,
            "UNAUTHORIZED": inp.unauthorized_disputes,
            "OTHER": inp.other_disputes,
        }
        dominant = max(dominant_map, key=lambda k: dominant_map[k])
        type_split = DisputeTypeSplit(
            inr_pct=inr_pct, snad_pct=snad_pct,
            unauthorized_pct=unauth_pct, other_pct=other_pct,
            dominant_type=dominant,
        )

    # Projected rate (if monthly data provided)
    projected_rate_pct = None
    projected_status = None
    if inp.disputes_this_month > 0 and inp.transactions_this_month > 0 and inp.days_into_month > 0:
        days_in_month = 30
        projected_disputes = round(inp.disputes_this_month / inp.days_into_month * days_in_month)
        projected_transactions = round(inp.transactions_this_month / inp.days_into_month * days_in_month)
        proj_rate = _compute_rate(projected_disputes, projected_transactions)
        projected_rate_pct = round(proj_rate * 100, 4)
        projected_status = _classify_rate(projected_rate_pct)

    # Immediate actions
    actions: list[str] = []
    if current_status == HDRStatus.SEVERE:
        actions.append("CRITICAL: Contact PayPal risk team immediately — account termination risk")
        actions.append("Pause new listings and review all open disputes aggressively")
    elif current_status == HDRStatus.PENALIZED:
        actions.append(f"Fee active: ${monthly_fee:.0f}/month in chargeback fees estimated")
        actions.append("Prioritize winning/settling disputes to reduce 90-day count")
        actions.append("Review buyer communication quality and return policy clarity")
    elif current_status == HDRStatus.LIMITED:
        actions.append("Account features restricted — resolve disputes promptly to drop below 1.00%")
        actions.append(f"Need {to_safe} disputes to age out to reach HEALTHY")
    elif current_status == HDRStatus.ELEVATED:
        actions.append(f"Warning zone — {to_next or 'few'} more disputes reach LIMITED (1.00%)")
        actions.append("Review item descriptions and delivery tracking completeness")
    else:
        if to_next is not None and to_next <= 5:
            actions.append(f"Healthy but tight — only {to_next} disputes to ELEVATED zone")
        else:
            actions.append("Rate healthy — maintain current practices")

    if rate_trend == "WORSENING":
        actions.append(f"Trend worsening: +{rate_delta:.2f}pp vs. prior 90 days")

    if type_split and type_split.dominant_type == "SNAD":
        actions.append("SNAD dominates — improve item descriptions and photo quality")
    elif type_split and type_split.dominant_type == "INR":
        actions.append("INR dominates — improve shipping tracking coverage")

    summary = (
        f"HDR {inp.merchant_id}: "
        f"rate={current_rate_pct:.3f}% | status={current_status.value} | "
        f"disputes={inp.disputes_opened_90d}/{inp.transactions_processed_90d} | "
        f"to_next={to_next} | to_safe={to_safe}"
    )

    return ChargebackRateReport(
        merchant_id=inp.merchant_id,
        current_rate_pct=current_rate_pct,
        current_status=current_status,
        status_label=_TIER_LABELS[current_status],
        disputes_90d=inp.disputes_opened_90d,
        transactions_90d=inp.transactions_processed_90d,
        prior_rate_pct=prior_rate_pct,
        rate_trend=rate_trend,
        rate_delta_pct=rate_delta,
        disputes_to_next_tier=to_next,
        disputes_to_safe=to_safe,
        monthly_fee_risk=monthly_fee,
        is_fee_active=is_fee_active,
        type_split=type_split,
        projected_rate_pct=projected_rate_pct,
        projected_status=projected_status,
        immediate_actions=actions,
        summary=summary,
    )
