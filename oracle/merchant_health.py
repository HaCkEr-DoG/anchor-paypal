"""
Merchant Health Dashboard Oracle — consolidated health score for the merchant.

Every other oracle module answers one question. This one answers:
  "How healthy is this merchant's dispute operation right now?"

It aggregates signals from all oracle layers into a single health score
and surfaced recommendations, acting as the top-level triage that feeds
the merchant dashboard.

Health dimensions (weighted):
  1. HDR Safety (30%) — distance from PayPal's punishment thresholds
  2. Win Rate (25%)   — are disputes being fought effectively?
  3. Fraud Defense (20%) — how well is fraud being detected and blocked?
  4. Cash Flow Risk (15%) — amount at risk in open disputes
  5. Operational Efficiency (10%) — autopilot ratio, human minutes/dispute

Composite score 0–100:
  85–100: EXCELLENT — no action needed
  70–84:  GOOD — monitor, minor improvements available
  55–69:  FAIR — specific areas need attention
  40–54:  POOR — multiple weak points, remediation needed
  0–39:   CRITICAL — urgent intervention required

This is the single number a merchant dashboard should show prominently.
Everything else in the oracle layer feeds into it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class HealthGrade(str, Enum):
    EXCELLENT = "EXCELLENT"   # 85-100
    GOOD      = "GOOD"        # 70-84
    FAIR      = "FAIR"        # 55-69
    POOR      = "POOR"        # 40-54
    CRITICAL  = "CRITICAL"    # 0-39


@dataclass
class MerchantHealthInput:
    """
    Current operational state of the merchant's dispute management.
    All fields correspond to metrics available from other oracle modules.
    """
    # HDR Safety signals
    current_hdr_rate: float          # E.g. 0.012 = 1.2%
    hdr_trajectory: str = "STABLE"  # "WORSENING" / "STABLE" / "IMPROVING"

    # Win rate signals
    dispute_win_rate_90d: float = 0.65    # Last 90 days
    evidence_quality_avg: str = "MODERATE"  # "WEAK" / "MODERATE" / "STRONG"

    # Volume signals
    total_open_disputes: int = 0
    total_at_risk_amount: float = 0.0
    monthly_gmv: float = 100000.0

    # Fraud defense
    fraud_detection_rate: float = 0.70   # % of fraud caught before order ships
    blocked_buyers_count: int = 0

    # Operational efficiency
    autopilot_ratio: float = 0.70        # % of disputes handled without human review
    avg_human_minutes_per_dispute: float = 25.0
    response_time_compliance: float = 0.95  # % of disputes responded within deadline

    # Optional enrichment
    has_carrier_strategy: bool = True    # Using PLATINUM carriers for high-value
    has_buyer_vetting: bool = True       # Pre-order dispute prediction active


@dataclass
class HealthDimension:
    name: str
    score: float            # 0–100
    weight: float           # Contribution to composite (sum = 1.0)
    grade: str              # EXCELLENT / GOOD / FAIR / POOR / CRITICAL
    top_issue: Optional[str] = None
    recommendation: Optional[str] = None


@dataclass
class MerchantHealthReport:
    """
    Consolidated merchant health assessment.
    Top-level output for the Anchor dashboard.
    """
    composite_score: float
    grade: HealthGrade

    # Dimension breakdown
    dimensions: list[HealthDimension]

    # Key metrics
    current_hdr_rate: float
    dispute_win_rate: float
    autopilot_ratio: float
    at_risk_amount: float

    # Trend
    trend_direction: str    # "IMPROVING" / "STABLE" / "DECLINING"
    trend_note: str

    # Action items
    urgent_actions: list[str]        # Must fix now
    improvement_actions: list[str]   # Would improve score

    summary: str
    dashboard_headline: str          # One line for top of dashboard


def _hdr_dimension(inp: MerchantHealthInput) -> HealthDimension:
    """Score HDR safety (30% weight)."""
    rate = inp.current_hdr_rate

    # Score based on distance from thresholds
    if rate < 0.003:          # < 0.3% — excellent buffer
        score = 100.0
        issue = None
        rec = None
    elif rate < 0.005:        # 0.3–0.5% — good buffer
        score = 90.0
        issue = None
        rec = "Monitor monthly — approaching ELEVATED threshold"
    elif rate < 0.008:        # 0.5–0.8% — ELEVATED
        score = 72.0
        issue = f"HDR at {rate:.2%} — in ELEVATED zone"
        rec = "Increase transaction volume and prevent new disputes"
    elif rate < 0.010:        # 0.8–1.0% — approaching LIMITED
        score = 55.0
        issue = f"HDR at {rate:.2%} — {(0.010 - rate) * 10000:.0f} bps from LIMITED restrictions"
        rec = "Activate HDR war room protocol immediately"
    elif rate < 0.015:        # 1.0–1.5% — LIMITED
        score = 35.0
        issue = f"HDR at {rate:.2%} — LIMITED zone (processing restrictions active)"
        rec = "Emergency: preemptive refunds + pause high-risk products + contact PayPal"
    elif rate < 0.030:        # 1.5–3.0% — FEE TIER
        score = 20.0
        issue = f"HDR at {rate:.2%} — FEE TIER ($30/dispute above 1.5%)"
        rec = "Critical: implement all HDR war room actions, freeze new high-risk orders"
    else:                     # > 3.0% — SEVERE/TERMINAL
        score = 5.0
        issue = f"HDR at {rate:.2%} — SEVERE/TERMINAL zone, account suspension risk"
        rec = "Executive escalation required — contact PayPal Merchant Relations today"

    # Trajectory penalty
    if inp.hdr_trajectory == "WORSENING":
        score = max(0, score - 15)
    elif inp.hdr_trajectory == "IMPROVING":
        score = min(100, score + 10)

    grade = _score_to_grade(score)
    return HealthDimension("HDR Safety", round(score, 1), 0.30, grade, issue, rec)


def _win_rate_dimension(inp: MerchantHealthInput) -> HealthDimension:
    """Score win rate effectiveness (25% weight)."""
    wr = inp.dispute_win_rate_90d

    if wr >= 0.80:
        score = 100.0
        issue = None
        rec = "Excellent win rate — maintain evidence standards"
    elif wr >= 0.70:
        score = 85.0
        issue = None
        rec = "Good win rate — upgrade carrier strategy to push above 80%"
    elif wr >= 0.60:
        score = 65.0
        issue = f"Win rate {wr:.0%} — below 65% target"
        rec = "Invest in evidence quality: delivery confirmation + signature for high-value orders"
    elif wr >= 0.50:
        score = 45.0
        issue = f"Win rate {wr:.0%} — losing nearly half of disputes"
        rec = "Review top loss reasons: missing tracking, no delivery confirmation, no photos (SNAD)"
    else:
        score = 20.0
        issue = f"Win rate {wr:.0%} — critically low"
        rec = "Stop fighting unwinnable disputes; focus on only HIGH_CONFIDENCE fights"

    if inp.evidence_quality_avg == "STRONG":
        score = min(100, score + 10)
    elif inp.evidence_quality_avg == "WEAK":
        score = max(0, score - 10)

    grade = _score_to_grade(score)
    return HealthDimension("Win Rate", round(score, 1), 0.25, grade, issue, rec)


def _fraud_defense_dimension(inp: MerchantHealthInput) -> HealthDimension:
    """Score fraud defense (20% weight)."""
    fdr = inp.fraud_detection_rate

    if fdr >= 0.85:
        score = 100.0
        issue = None
        rec = None
    elif fdr >= 0.70:
        score = 80.0
        issue = f"Fraud detection {fdr:.0%} — some fraudulent orders slipping through"
        rec = "Enable buyer dispute prediction on all orders; block repeat disputers"
    elif fdr >= 0.50:
        score = 55.0
        issue = f"Fraud detection {fdr:.0%} — significant fraud exposure"
        rec = "Implement network intelligence; require review for new accounts and high-value orders"
    else:
        score = 25.0
        issue = f"Fraud detection {fdr:.0%} — most fraud is not being caught pre-order"
        rec = "Deploy full fraud prevention stack: predictor + network intel + buyer vetting"

    if inp.has_buyer_vetting:
        score = min(100, score + 5)
    if not inp.has_carrier_strategy:
        score = max(0, score - 5)

    grade = _score_to_grade(score)
    return HealthDimension("Fraud Defense", round(score, 1), 0.20, grade, issue, rec)


def _cash_flow_dimension(inp: MerchantHealthInput) -> HealthDimension:
    """Score cash flow risk (15% weight)."""
    if inp.monthly_gmv <= 0:
        pct = 0.0
    else:
        pct = inp.total_at_risk_amount / inp.monthly_gmv

    if pct < 0.01:
        score = 100.0
        issue = None
        rec = None
    elif pct < 0.03:
        score = 80.0
        issue = f"{pct:.1%} of monthly GMV in open disputes (${inp.total_at_risk_amount:.0f})"
        rec = "Normal — ensure disputes are progressing toward resolution"
    elif pct < 0.06:
        score = 60.0
        issue = f"{pct:.1%} of monthly GMV at risk in disputes"
        rec = "Accelerate dispute resolution; prioritize high-value disputes"
    elif pct < 0.10:
        score = 40.0
        issue = f"{pct:.1%} of monthly GMV in dispute limbo"
        rec = "Critical: too much capital tied up — accelerate settlement on borderline cases"
    else:
        score = 20.0
        issue = f"{pct:.1%} of monthly GMV in open disputes — cash flow emergency"
        rec = "Immediate settlement outreach on all non-critical disputes; fight only clear wins"

    grade = _score_to_grade(score)
    return HealthDimension("Cash Flow Risk", round(score, 1), 0.15, grade, issue, rec)


def _operational_dimension(inp: MerchantHealthInput) -> HealthDimension:
    """Score operational efficiency (10% weight)."""
    score = 70.0  # Base

    if inp.autopilot_ratio >= 0.80:
        score += 20.0
    elif inp.autopilot_ratio >= 0.60:
        score += 10.0
    elif inp.autopilot_ratio < 0.40:
        score -= 20.0

    if inp.response_time_compliance >= 0.98:
        score += 10.0
    elif inp.response_time_compliance < 0.90:
        score -= 15.0

    if inp.avg_human_minutes_per_dispute <= 15:
        score += 5.0
    elif inp.avg_human_minutes_per_dispute >= 45:
        score -= 10.0

    score = max(0.0, min(100.0, score))

    issue = None
    rec = None
    if inp.autopilot_ratio < 0.50:
        issue = f"Only {inp.autopilot_ratio:.0%} of disputes handled by autopilot — high human cost"
        rec = "Review AUTO_FIGHT threshold; ensure claim_scorer signals are populated"
    if inp.response_time_compliance < 0.95:
        issue = f"Response time compliance at {inp.response_time_compliance:.0%}"
        rec = "Enable deadline alerts; use batch_dispatcher for daily prioritization"

    grade = _score_to_grade(score)
    return HealthDimension("Operational Efficiency", round(score, 1), 0.10, grade, issue, rec)


def _score_to_grade(score: float) -> str:
    if score >= 85:
        return "EXCELLENT"
    elif score >= 70:
        return "GOOD"
    elif score >= 55:
        return "FAIR"
    elif score >= 40:
        return "POOR"
    else:
        return "CRITICAL"


def _composite_grade(score: float) -> HealthGrade:
    if score >= 85:
        return HealthGrade.EXCELLENT
    elif score >= 70:
        return HealthGrade.GOOD
    elif score >= 55:
        return HealthGrade.FAIR
    elif score >= 40:
        return HealthGrade.POOR
    else:
        return HealthGrade.CRITICAL


def assess_merchant_health(inp: MerchantHealthInput) -> MerchantHealthReport:
    """
    Compute composite merchant health score from all oracle signals.
    """
    hdr_dim   = _hdr_dimension(inp)
    wr_dim    = _win_rate_dimension(inp)
    fraud_dim = _fraud_defense_dimension(inp)
    cash_dim  = _cash_flow_dimension(inp)
    ops_dim   = _operational_dimension(inp)

    dimensions = [hdr_dim, wr_dim, fraud_dim, cash_dim, ops_dim]

    composite = sum(d.score * d.weight for d in dimensions)
    grade = _composite_grade(composite)

    # Trend
    if inp.hdr_trajectory == "IMPROVING" and inp.dispute_win_rate_90d >= 0.65:
        trend = "IMPROVING"
        trend_note = "HDR improving and win rate healthy — positive momentum"
    elif inp.hdr_trajectory == "WORSENING" or inp.dispute_win_rate_90d < 0.50:
        trend = "DECLINING"
        trend_note = "HDR worsening or win rate declining — intervention needed"
    else:
        trend = "STABLE"
        trend_note = "Metrics stable — focus on incremental improvements"

    # Urgent actions (POOR or CRITICAL dimensions)
    urgent: list[str] = []
    improvements: list[str] = []
    for d in dimensions:
        if d.top_issue and d.grade in ("CRITICAL", "POOR"):
            urgent.append(f"[{d.name}] {d.recommendation}")
        elif d.recommendation and d.grade in ("FAIR",):
            improvements.append(f"[{d.name}] {d.recommendation}")

    # Dashboard headline
    if grade == HealthGrade.EXCELLENT:
        headline = f"Merchant Health: EXCELLENT ({composite:.0f}/100) — operating at peak efficiency"
    elif grade == HealthGrade.GOOD:
        headline = f"Merchant Health: GOOD ({composite:.0f}/100) — {len(improvements)} improvement opportunity{'ies' if len(improvements) != 1 else 'y'}"
    elif grade == HealthGrade.FAIR:
        headline = f"Merchant Health: FAIR ({composite:.0f}/100) — {len(improvements) + len(urgent)} areas need attention"
    elif grade == HealthGrade.POOR:
        headline = f"Merchant Health: POOR ({composite:.0f}/100) — {len(urgent)} urgent issue{'s' if len(urgent) != 1 else ''}"
    else:
        headline = f"MERCHANT HEALTH CRITICAL ({composite:.0f}/100) — immediate intervention required"

    summary = (
        f"Score: {composite:.0f}/100 ({grade.value}) | "
        f"HDR: {inp.current_hdr_rate:.2%} ({inp.hdr_trajectory}) | "
        f"Win rate: {inp.dispute_win_rate_90d:.0%} | "
        f"Autopilot: {inp.autopilot_ratio:.0%} | "
        f"At risk: ${inp.total_at_risk_amount:,.0f}"
    )

    return MerchantHealthReport(
        composite_score=round(composite, 1),
        grade=grade,
        dimensions=dimensions,
        current_hdr_rate=inp.current_hdr_rate,
        dispute_win_rate=inp.dispute_win_rate_90d,
        autopilot_ratio=inp.autopilot_ratio,
        at_risk_amount=inp.total_at_risk_amount,
        trend_direction=trend,
        trend_note=trend_note,
        urgent_actions=urgent,
        improvement_actions=improvements,
        summary=summary,
        dashboard_headline=headline,
    )


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
from enum import Enum as _Enum
from dataclasses import dataclass as _dataclass, field as _field


class AccountStatus(str, _Enum):
    HEALTHY = "HEALTHY"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"
    EXCESSIVE = "EXCESSIVE"
    CRITICAL = "CRITICAL"
    TERMINAL = "TERMINAL"


@_dataclass
class DisputeWindow:
    dispute_rate: float = 0.0
    chargeback_rate: float = 0.0
    total_transactions: int = 100
    total_disputes: int = 0
    win_rate: float = 0.5
    merchant_id: str = "default"
    period_days: int = 30
    dispute_count: int = 0
    transaction_count: int = 100
    dispute_volume: float = 0.0
    revenue: float = 0.0
