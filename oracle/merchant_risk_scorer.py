"""
Merchant Risk Scorer — assess a merchant's current dispute risk exposure
across five dimensions and project their HDR (High Dispute Rate) status.

Covers:
  - Current dispute rate vs. PayPal HDR thresholds
  - Rolling 90-day dispute volume trajectory
  - Revenue-at-risk from open disputes
  - Fee tier exposure (normal / elevated / limited / fee_tier / severe)
  - Recommended actions to reduce dispute rate

HDR thresholds (PayPal rolling 90-day):
  NORMAL   < 0.50%
  ELEVATED 0.50–0.99%
  LIMITED  1.00–1.49%
  FEE_TIER 1.50–2.99%  ($30/dispute fee)
  SEVERE   ≥ 3.00%     (possible suspension risk)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class HdrTier(str, Enum):
    NORMAL   = "NORMAL"    # < 0.50%
    ELEVATED = "ELEVATED"  # 0.50–0.99%
    LIMITED  = "LIMITED"   # 1.00–1.49%
    FEE_TIER = "FEE_TIER"  # 1.50–2.99%
    SEVERE   = "SEVERE"    # ≥ 3.00%


class RiskTrend(str, Enum):
    IMPROVING  = "IMPROVING"
    STABLE     = "STABLE"
    WORSENING  = "WORSENING"
    CRITICAL   = "CRITICAL"   # Accelerating toward SEVERE


class RiskDimension(str, Enum):
    DISPUTE_RATE         = "DISPUTE_RATE"
    VOLUME_TRAJECTORY    = "VOLUME_TRAJECTORY"
    REVENUE_EXPOSURE     = "REVENUE_EXPOSURE"
    FEE_EXPOSURE         = "FEE_EXPOSURE"
    RESOLUTION_EFFICIENCY = "RESOLUTION_EFFICIENCY"


@dataclass
class DimensionScore:
    dimension: RiskDimension
    score: int         # 0 = best, 100 = worst
    label: str
    finding: str


@dataclass
class MerchantRiskInput:
    """Snapshot of a merchant's dispute portfolio."""
    merchant_id: str

    # Transaction volume (rolling 90 days)
    total_transactions_90d: int
    total_transaction_value_90d: float

    # Dispute counts (rolling 90 days)
    total_disputes_90d: int
    open_disputes: int
    disputes_won_90d: int
    disputes_lost_90d: int
    disputes_accepted_90d: int

    # Prior period (90–180 days ago) for trend
    total_transactions_prior: int = 0
    total_disputes_prior: int = 0

    # Financial exposure
    open_dispute_value: float = 0.0    # Sum of open dispute amounts
    chargeback_fee_per_dispute: float = 20.0

    # Resolution efficiency
    avg_response_time_days: float = 5.0   # Merchant's avg response time
    pct_responded_on_time: float = 1.0    # Fraction responded before deadline


@dataclass
class MerchantRiskReport:
    """Full merchant risk assessment."""
    merchant_id: str

    dispute_rate_90d: float          # disputes / transactions
    hdr_tier: HdrTier
    hdr_tier_next: HdrTier           # What tier they'd hit if disputes grow 20%
    fee_exposure_per_dispute: float  # Additional fee if in FEE_TIER

    dimensions: list[DimensionScore]
    overall_risk_score: int          # 0 = no risk, 100 = maximum risk

    trend: RiskTrend
    trend_dispute_rate_prior: float

    revenue_at_risk: float
    projected_fees_if_current_rate: float   # At current rate × fee tier

    recommendations: list[str]
    urgent_actions: list[str]        # Must-do to avoid tier escalation

    summary: str


_HDR_THRESHOLDS = [
    (0.0050, HdrTier.ELEVATED),
    (0.0100, HdrTier.LIMITED),
    (0.0150, HdrTier.FEE_TIER),
    (0.0300, HdrTier.SEVERE),
]

_FEE_TIER_FEES: dict[HdrTier, float] = {
    HdrTier.NORMAL:   0.0,
    HdrTier.ELEVATED: 0.0,
    HdrTier.LIMITED:  0.0,
    HdrTier.FEE_TIER: 30.0,
    HdrTier.SEVERE:   30.0,
}


def _classify_hdr(rate: float) -> HdrTier:
    for threshold, tier in reversed(_HDR_THRESHOLDS):
        if rate >= threshold:
            return tier
    return HdrTier.NORMAL


def score_merchant_risk(inp: MerchantRiskInput) -> MerchantRiskReport:
    """Score merchant dispute risk across five dimensions."""
    dimensions: list[DimensionScore] = []
    recommendations: list[str] = []
    urgent_actions: list[str] = []

    # ── Dispute rate ─────────────────────────────────────────────
    total_txns = max(inp.total_transactions_90d, 1)
    dispute_rate = inp.total_disputes_90d / total_txns
    hdr_tier = _classify_hdr(dispute_rate)

    dr_score = {
        HdrTier.NORMAL:   0,
        HdrTier.ELEVATED: 25,
        HdrTier.LIMITED:  50,
        HdrTier.FEE_TIER: 75,
        HdrTier.SEVERE:   100,
    }[hdr_tier]

    dimensions.append(DimensionScore(
        RiskDimension.DISPUTE_RATE,
        dr_score,
        hdr_tier.value,
        f"Dispute rate {dispute_rate:.3%} ({inp.total_disputes_90d}/{inp.total_transactions_90d})",
    ))

    if hdr_tier in (HdrTier.FEE_TIER, HdrTier.SEVERE):
        urgent_actions.append(f"CRITICAL: {hdr_tier.value} dispute rate — immediate intervention required")
    elif hdr_tier == HdrTier.LIMITED:
        urgent_actions.append("WARNING: Approaching FEE_TIER — reduce disputes by 20% to avoid $30/dispute fee")

    # Projected rate if disputes grow 20%
    projected_disputes = inp.total_disputes_90d * 1.20
    projected_rate = projected_disputes / total_txns
    hdr_tier_next = _classify_hdr(projected_rate)

    # ── Volume trajectory ─────────────────────────────────────────
    prior_rate = 0.0
    if inp.total_transactions_prior > 0:
        prior_rate = inp.total_disputes_prior / max(inp.total_transactions_prior, 1)

    if prior_rate > 0:
        rate_change = dispute_rate - prior_rate
        if rate_change > 0.005:
            traj_score = 75
            trend = RiskTrend.WORSENING
        elif rate_change > 0.010:
            traj_score = 100
            trend = RiskTrend.CRITICAL
        elif rate_change < -0.005:
            traj_score = 10
            trend = RiskTrend.IMPROVING
        else:
            traj_score = 30
            trend = RiskTrend.STABLE
    else:
        traj_score = 30
        trend = RiskTrend.STABLE
        rate_change = 0.0

    if rate_change > 0.010:
        urgent_actions.append("Dispute rate accelerating rapidly — investigate root cause")
        trend = RiskTrend.CRITICAL

    dimensions.append(DimensionScore(
        RiskDimension.VOLUME_TRAJECTORY,
        traj_score,
        trend.value,
        f"Rate {'+' if rate_change >= 0 else ''}{rate_change:.3%} vs prior period",
    ))

    # ── Revenue exposure ──────────────────────────────────────────
    revenue_at_risk = inp.open_dispute_value
    rev_pct = revenue_at_risk / max(inp.total_transaction_value_90d, 1)
    if rev_pct >= 0.05:
        rev_score = 80
    elif rev_pct >= 0.02:
        rev_score = 50
    elif rev_pct >= 0.01:
        rev_score = 25
    else:
        rev_score = 5

    dimensions.append(DimensionScore(
        RiskDimension.REVENUE_EXPOSURE,
        rev_score,
        f"{rev_pct:.2%} of 90d revenue at risk",
        f"${revenue_at_risk:.0f} in open disputes",
    ))

    # ── Fee exposure ──────────────────────────────────────────────
    extra_fee = _FEE_TIER_FEES[hdr_tier]
    total_fee_exposure = inp.total_disputes_90d * (inp.chargeback_fee_per_dispute + extra_fee)
    fee_score = min(100, int((extra_fee / 30.0) * 75 + rev_score * 0.25))

    if hdr_tier == HdrTier.FEE_TIER:
        recommendations.append(f"FEE_TIER active: $30 extra fee per dispute = ${inp.total_disputes_90d * 30:.0f} extra/quarter")
    elif hdr_tier_next == HdrTier.FEE_TIER and hdr_tier != HdrTier.FEE_TIER:
        recommendations.append("One tier away from FEE_TIER — $30/dispute fee applies at 1.50%")

    dimensions.append(DimensionScore(
        RiskDimension.FEE_EXPOSURE,
        fee_score,
        f"+${extra_fee:.0f}/dispute surcharge",
        f"Total fee exposure ${total_fee_exposure:.0f} at current dispute count",
    ))

    # ── Resolution efficiency ─────────────────────────────────────
    total_resolved = inp.disputes_won_90d + inp.disputes_lost_90d + inp.disputes_accepted_90d
    win_rate = inp.disputes_won_90d / max(total_resolved, 1)
    eff_score = max(0, int((1.0 - win_rate) * 60 + (1.0 - inp.pct_responded_on_time) * 40))

    if win_rate < 0.40:
        recommendations.append(f"Win rate {win_rate:.0%} — review evidence quality and policy documentation")
    if inp.avg_response_time_days > 7:
        recommendations.append("Average response time > 7 days — automate evidence collection")
    if inp.pct_responded_on_time < 0.90:
        urgent_actions.append(f"Only {inp.pct_responded_on_time:.0%} of disputes answered on time — set up deadline alerts")

    dimensions.append(DimensionScore(
        RiskDimension.RESOLUTION_EFFICIENCY,
        eff_score,
        f"Win rate {win_rate:.0%} | avg response {inp.avg_response_time_days:.1f}d",
        f"{total_resolved} disputes resolved; {inp.open_disputes} still open",
    ))

    # ── Overall score ─────────────────────────────────────────────
    weights = [0.35, 0.20, 0.20, 0.15, 0.10]
    overall = int(sum(d.score * w for d, w in zip(dimensions, weights)))

    # General recommendations
    if hdr_tier == HdrTier.NORMAL:
        recommendations.append("Dispute rate healthy — maintain current practices")
    if dispute_rate > 0.0100:
        recommendations.append("Analyze top dispute reason codes and address root causes")
    if not recommendations:
        recommendations.append("No immediate action needed — monitor monthly")

    projected_fees = inp.total_disputes_90d * (inp.chargeback_fee_per_dispute + extra_fee)

    summary = (
        f"Risk {inp.merchant_id}: "
        f"rate={dispute_rate:.3%} ({hdr_tier.value}) | "
        f"score={overall}/100 | "
        f"trend={trend.value} | "
        f"revenue_at_risk=${revenue_at_risk:.0f}"
    )

    return MerchantRiskReport(
        merchant_id=inp.merchant_id,
        dispute_rate_90d=round(dispute_rate, 6),
        hdr_tier=hdr_tier,
        hdr_tier_next=hdr_tier_next,
        fee_exposure_per_dispute=extra_fee,
        dimensions=dimensions,
        overall_risk_score=overall,
        trend=trend,
        trend_dispute_rate_prior=round(prior_rate, 6),
        revenue_at_risk=round(revenue_at_risk, 2),
        projected_fees_if_current_rate=round(projected_fees, 2),
        recommendations=recommendations,
        urgent_actions=urgent_actions,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
RiskTier = HdrTier
