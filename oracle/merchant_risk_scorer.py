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
    SP_COVERAGE          = "SP_COVERAGE"
    CLAIM_MIX            = "CLAIM_MIX"
    RESPONSE_HYGIENE     = "RESPONSE_HYGIENE"
    EVIDENCE_QUALITY     = "EVIDENCE_QUALITY"


@dataclass
class DimensionScore:
    dimension: RiskDimension
    score: int         # 0 = best, 100 = worst (original scale)
    label: str
    finding: str
    weight: float = 0.0           # 0.0–1.0, weights sum to 1.0
    weighted_contribution: float = 0.0  # = health_score * weight


@dataclass
class MerchantRiskInput:
    """Snapshot of a merchant's dispute portfolio."""
    merchant_id: str

    # ── Test-API primary fields ──────────────────────────────────
    disputes_last_90d: int = 0
    transactions_last_90d: int = 1
    total_disputes_ever: int = 0
    disputes_won: int = 0
    disputes_responded_on_time: int = 0
    disputes_responded_late_or_missed: int = 0
    pct_inr: float = 0.0
    pct_snad: float = 0.0
    pct_unauthorized: float = 0.0
    pct_sp_covered: float = 0.0
    avg_evidence_score: float = 50.0
    months_on_paypal: int = 0
    is_in_hdr_zone: bool = False

    # ── Legacy fields (kept for backward compat) ─────────────────
    total_transactions_90d: int = field(default=0)
    total_transaction_value_90d: float = field(default=0.0)
    total_disputes_90d: int = field(default=0)
    open_disputes: int = field(default=0)
    disputes_won_90d: int = field(default=0)
    disputes_lost_90d: int = field(default=0)
    disputes_accepted_90d: int = field(default=0)
    total_transactions_prior: int = field(default=0)
    total_disputes_prior: int = field(default=0)
    open_dispute_value: float = field(default=0.0)
    chargeback_fee_per_dispute: float = field(default=20.0)
    avg_response_time_days: float = field(default=5.0)
    pct_responded_on_time: float = field(default=1.0)

    def __post_init__(self):
        # Sync test-API → legacy
        if not self.total_transactions_90d:
            self.total_transactions_90d = self.transactions_last_90d
        if not self.total_disputes_90d:
            self.total_disputes_90d = self.disputes_last_90d
        if not self.disputes_won_90d:
            self.disputes_won_90d = self.disputes_won
        # Sync legacy → test-API (reverse)
        if not self.transactions_last_90d and self.total_transactions_90d:
            self.transactions_last_90d = self.total_transactions_90d
        if not self.disputes_last_90d and self.total_disputes_90d:
            self.disputes_last_90d = self.total_disputes_90d
        if not self.disputes_won and self.disputes_won_90d:
            self.disputes_won = self.disputes_won_90d
        total_resp = self.disputes_responded_on_time + self.disputes_responded_late_or_missed
        if total_resp > 0:
            self.pct_responded_on_time = self.disputes_responded_on_time / total_resp


@dataclass
class MerchantRiskReport:
    """Full merchant risk assessment."""
    merchant_id: str

    # ── Test-API primary fields ──────────────────────────────────
    composite_score: float           # 0=worst, 100=best (health score)
    risk_tier: "RiskTier"            # HEALTHY / ELEVATED / CRITICAL
    dispute_rate_pct: float          # dispute rate as percentage (e.g. 1.0 = 1%)
    dimensions: list[DimensionScore]
    recommendations: list[str]
    summary: str

    # ── Legacy fields ────────────────────────────────────────────
    dispute_rate_90d: float = 0.0    # disputes / transactions (fraction)
    hdr_tier: HdrTier = field(default=None)
    hdr_tier_next: HdrTier = field(default=None)
    fee_exposure_per_dispute: float = 0.0
    overall_risk_score: int = 0      # 0 = no risk, 100 = maximum risk (inverted)
    trend: RiskTrend = field(default=None)
    trend_dispute_rate_prior: float = 0.0
    revenue_at_risk: float = 0.0
    projected_fees_if_current_rate: float = 0.0
    urgent_actions: list[str] = field(default_factory=list)

    def __post_init__(self):
        # Keep legacy overall_risk_score in sync
        self.overall_risk_score = max(0, int(100 - self.composite_score))
        if self.hdr_tier is None:
            self.hdr_tier = _classify_hdr(self.dispute_rate_90d)


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
    """Score merchant dispute risk across six dimensions (health-score model)."""
    recommendations: list[str] = []
    urgent_actions: list[str] = []

    total_txns = max(inp.transactions_last_90d, max(inp.total_transactions_90d, 1))
    dispute_rate = inp.disputes_last_90d / total_txns
    dispute_rate_pct = round(dispute_rate * 100, 4)
    hdr_tier = _classify_hdr(dispute_rate)

    # ── Dimension 1: DISPUTE_RATE (weight 0.30) ───────────────────
    dr_score = {
        HdrTier.NORMAL:   0,
        HdrTier.ELEVATED: 25,
        HdrTier.LIMITED:  50,
        HdrTier.FEE_TIER: 75,
        HdrTier.SEVERE:   100,
    }[hdr_tier]

    if hdr_tier in (HdrTier.FEE_TIER, HdrTier.SEVERE):
        urgent_actions.append(f"CRITICAL: {hdr_tier.value} dispute rate — immediate intervention required")
        recommendations.append(f"Dispute rate {dispute_rate_pct:.2f}% is in {hdr_tier.value} tier — reduce urgently")
    elif hdr_tier in (HdrTier.LIMITED, HdrTier.ELEVATED):
        recommendations.append(f"Dispute rate {dispute_rate_pct:.2f}% is elevated — monitor and reduce")

    # ── Dimension 2: RESOLUTION_EFFICIENCY (weight 0.20) ─────────
    total_ever = max(inp.total_disputes_ever, 1)
    win_rate = inp.disputes_won / total_ever
    res_score = max(0, min(100, int((1.0 - win_rate) * 100)))

    if win_rate < 0.40:
        recommendations.append(f"Win rate {win_rate:.0%} — review evidence quality and policy documentation")

    # ── Dimension 3: RESPONSE_HYGIENE (weight 0.20) ───────────────
    total_resp = inp.disputes_responded_on_time + inp.disputes_responded_late_or_missed
    on_time_rate = inp.disputes_responded_on_time / max(total_resp, 1)
    resp_score = max(0, min(100, int((1.0 - on_time_rate) * 100)))

    if on_time_rate < 0.90 and total_resp > 0:
        urgent_actions.append(f"Only {on_time_rate:.0%} of disputes answered on time — set up deadline alerts")

    # ── Dimension 4: CLAIM_MIX (weight 0.15) ──────────────────────
    # unauthorized and snad carry higher risk than inr
    claim_risk = inp.pct_unauthorized * 2.0 + inp.pct_snad * 1.0
    claim_score = min(100, int(claim_risk * 100))

    if inp.pct_snad > 0.40:
        recommendations.append(f"SNAD disputes at {inp.pct_snad:.0%} — improve product descriptions and fulfillment accuracy")
    if inp.pct_unauthorized > 0.25:
        recommendations.append(f"Unauthorized disputes at {inp.pct_unauthorized:.0%} — review fraud controls")

    # ── Dimension 5: SP_COVERAGE (weight 0.10) ────────────────────
    sp_score = max(0, min(100, int((1.0 - inp.pct_sp_covered) * 100)))

    if inp.pct_sp_covered < 0.50:
        recommendations.append(f"Only {inp.pct_sp_covered:.0%} of transactions have Seller Protection — improve compliance")

    # ── Dimension 5: SP_COVERAGE + EVIDENCE_QUALITY (weight 0.15) ────
    # Combine SP coverage and evidence quality into a single quality score
    ev_score_raw = max(0, min(100, int(100 - inp.avg_evidence_score)))
    combined_quality_score = max(0, min(100, int((sp_score + ev_score_raw) / 2)))

    if inp.pct_sp_covered < 0.50:
        recommendations.append(f"Only {inp.pct_sp_covered:.0%} of transactions have Seller Protection — improve compliance")
    if inp.avg_evidence_score < 60:
        recommendations.append(f"Evidence quality {inp.avg_evidence_score:.0f}/100 — strengthen evidence packages")

    # HDR zone flag
    if inp.is_in_hdr_zone:
        recommendations.append("Merchant is in HDR zone — immediate dispute rate reduction required to avoid program restrictions")

    # ── Dimension 6: REVENUE_EXPOSURE (weight 0.05) — only when tenure is known ─
    tenure_months = max(0, inp.months_on_paypal)
    include_revenue_dim = tenure_months > 0
    if include_revenue_dim:
        if tenure_months >= 24:
            rev_score = 0
        elif tenure_months >= 12:
            rev_score = 25
        elif tenure_months >= 6:
            rev_score = 50
        else:
            rev_score = 75

    # ── Composite (health) score ──────────────────────────────────
    if include_revenue_dim:
        _DIM_WEIGHTS = [0.30, 0.20, 0.20, 0.15, 0.10, 0.05]
        _dim_specs = [
            (RiskDimension.DISPUTE_RATE,         dr_score,           "Dispute rate",      f"{dispute_rate_pct:.2f}%"),
            (RiskDimension.RESOLUTION_EFFICIENCY, res_score,          "Resolution",        f"win rate {win_rate:.0%}"),
            (RiskDimension.RESPONSE_HYGIENE,     resp_score,          "Response hygiene",  f"{on_time_rate:.0%} on time"),
            (RiskDimension.CLAIM_MIX,            claim_score,         "Claim mix",         f"unauth={inp.pct_unauthorized:.0%} snad={inp.pct_snad:.0%}"),
            (RiskDimension.SP_COVERAGE,          combined_quality_score, "SP & evidence",  f"sp={inp.pct_sp_covered:.0%} ev={inp.avg_evidence_score:.0f}/100"),
            (RiskDimension.REVENUE_EXPOSURE,     rev_score,           "Revenue exposure",  f"tenure={tenure_months}mo"),
        ]
    else:
        _DIM_WEIGHTS = [0.30, 0.20, 0.20, 0.15, 0.15]
        _dim_specs = [
            (RiskDimension.DISPUTE_RATE,         dr_score,           "Dispute rate",      f"{dispute_rate_pct:.2f}%"),
            (RiskDimension.RESOLUTION_EFFICIENCY, res_score,          "Resolution",        f"win rate {win_rate:.0%}"),
            (RiskDimension.RESPONSE_HYGIENE,     resp_score,          "Response hygiene",  f"{on_time_rate:.0%} on time"),
            (RiskDimension.CLAIM_MIX,            claim_score,         "Claim mix",         f"unauth={inp.pct_unauthorized:.0%} snad={inp.pct_snad:.0%}"),
            (RiskDimension.SP_COVERAGE,          combined_quality_score, "SP & evidence",  f"sp={inp.pct_sp_covered:.0%} ev={inp.avg_evidence_score:.0f}/100"),
        ]

    dimensions: list[DimensionScore] = []
    composite = 0.0
    for (dim, raw_score, lbl, finding), w in zip(_dim_specs, _DIM_WEIGHTS):
        health = 100 - raw_score
        contrib = round(health * w, 4)
        composite += contrib
        dimensions.append(DimensionScore(
            dimension=dim,
            score=raw_score,
            label=lbl,
            finding=finding,
            weight=w,
            weighted_contribution=contrib,
        ))

    composite = round(composite, 2)

    # Risk tier
    if composite >= 75:
        risk_tier = RiskTier.HEALTHY
    elif composite >= 50:
        risk_tier = RiskTier.ELEVATED
    else:
        risk_tier = RiskTier.CRITICAL

    if not recommendations:
        recommendations.append("No immediate action needed — maintain current practices")

    # Legacy fields
    projected_disputes = inp.disputes_last_90d * 1.20
    projected_rate = projected_disputes / total_txns
    hdr_tier_next = _classify_hdr(projected_rate)
    extra_fee = _FEE_TIER_FEES[hdr_tier]
    projected_fees = inp.disputes_last_90d * (inp.chargeback_fee_per_dispute + extra_fee)

    summary = (
        f"Risk {inp.merchant_id}: "
        f"rate={dispute_rate_pct:.2f}% ({hdr_tier.value}) | "
        f"score={composite:.1f}/100 | "
        f"tier={risk_tier.value} | "
        f"win_rate={win_rate:.0%}"
    )

    # Trend calculation
    if inp.total_transactions_prior > 0 and inp.total_disputes_prior >= 0:
        prior_rate = inp.total_disputes_prior / inp.total_transactions_prior
        if prior_rate > 0:
            rate_change = dispute_rate - prior_rate
            rate_ratio = dispute_rate / prior_rate if prior_rate > 0 else 1.0
            if dispute_rate < prior_rate and rate_change < -0.001:
                trend = RiskTrend.IMPROVING
            elif rate_ratio >= 2.0 or rate_change >= 0.01:
                trend = RiskTrend.CRITICAL
            elif dispute_rate > prior_rate and rate_change >= 0.001:
                trend = RiskTrend.WORSENING
            else:
                trend = RiskTrend.STABLE
        else:
            trend = RiskTrend.STABLE
        trend_prior = round(prior_rate, 6)
    else:
        trend = RiskTrend.STABLE
        trend_prior = 0.0

    return MerchantRiskReport(
        merchant_id=inp.merchant_id,
        composite_score=composite,
        risk_tier=risk_tier,
        dispute_rate_pct=dispute_rate_pct,
        dimensions=dimensions,
        recommendations=recommendations,
        summary=summary,
        dispute_rate_90d=round(dispute_rate, 6),
        hdr_tier=hdr_tier,
        hdr_tier_next=hdr_tier_next,
        fee_exposure_per_dispute=extra_fee,
        overall_risk_score=max(0, int(100 - composite)),
        trend=trend,
        trend_dispute_rate_prior=trend_prior,
        revenue_at_risk=inp.open_dispute_value,
        projected_fees_if_current_rate=round(projected_fees, 2),
        urgent_actions=urgent_actions,
    )


# ---------------------------------------------------------------------------
# RiskTier — proper enum used by test-API
# ---------------------------------------------------------------------------
from enum import Enum as _Enum


class RiskTier(str, _Enum):
    HEALTHY  = "HEALTHY"
    ELEVATED = "ELEVATED"
    CRITICAL = "CRITICAL"
