"""
Merchant Risk Profiler — build a composite risk profile for a merchant
based on their dispute history, chargeback rates, and industry norms.

The risk profile informs:
  - Whether the merchant is approaching HDR thresholds
  - Which dispute types are disproportionate for their category
  - Whether their dispute patterns suggest operational issues
  - Recommended monitoring frequency and escalation triggers

Risk dimensions:
  1. HDR proximity (how close to 1.0% / 1.5% thresholds)
  2. Dispute concentration (% of disputes from one claim type)
  3. Win rate trend (improving / stable / degrading)
  4. Category benchmark deviation
  5. Repeat buyer dispute rate

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskDimension(str, Enum):
    HDR_PROXIMITY      = "HDR_PROXIMITY"
    CONCENTRATION      = "CONCENTRATION"
    WIN_RATE_TREND     = "WIN_RATE_TREND"
    CATEGORY_DEVIATION = "CATEGORY_DEVIATION"
    REPEAT_BUYER_RATE  = "REPEAT_BUYER_RATE"


class RiskSeverity(str, Enum):
    CRITICAL = "CRITICAL"   # Immediate action required
    HIGH     = "HIGH"       # Action required this week
    MEDIUM   = "MEDIUM"     # Monitor closely
    LOW      = "LOW"        # Normal operations
    HEALTHY  = "HEALTHY"    # Below-average risk


class RiskTrend(str, Enum):
    WORSENING = "WORSENING"
    STABLE    = "STABLE"
    IMPROVING = "IMPROVING"


@dataclass
class DisputeTypeSplit:
    """Breakdown of disputes by type."""
    claim_type: str
    count: int
    pct: float          # 0.0–1.0
    industry_avg_pct: float


@dataclass
class MerchantRiskInput:
    """Input for merchant risk profiling."""
    merchant_id: str
    merchant_category: str      # e.g., "ELECTRONICS", "APPAREL", "DIGITAL"

    # 90-day transaction volume
    transactions_90d: int
    disputes_90d: int

    # Prior period (previous 90 days)
    transactions_prior_90d: int = 0
    disputes_prior_90d: int = 0

    # Dispute outcomes
    disputes_won: int = 0
    disputes_lost: int = 0

    # Prior period outcomes
    disputes_won_prior: int = 0
    disputes_lost_prior: int = 0

    # Dispute type breakdown
    type_splits: list[DisputeTypeSplit] = field(default_factory=list)

    # Buyer repeat patterns
    repeat_buyer_dispute_count: int = 0  # Disputes from repeat buyers
    total_repeat_buyer_disputes: int = 0  # Denominator for repeat rate

    # Industry benchmarks
    industry_dispute_rate_pct: float = 0.50   # % for this category
    industry_win_rate: float = 0.55            # Win rate for this category


# Industry dispute rate benchmarks (approximate)
_INDUSTRY_DISPUTE_RATES: dict[str, float] = {
    "ELECTRONICS":    0.65,
    "APPAREL":        0.55,
    "DIGITAL":        0.80,
    "TRAVEL":         0.90,
    "FOOD":           0.30,
    "SERVICES":       0.60,
    "MARKETPLACE":    0.70,
    "SUBSCRIPTION":   0.75,
    "DEFAULT":        0.60,
}


def _current_rate(inp: MerchantRiskInput) -> float:
    if inp.transactions_90d == 0:
        return 0.0
    return inp.disputes_90d / inp.transactions_90d * 100


def _prior_rate(inp: MerchantRiskInput) -> float:
    if inp.transactions_prior_90d == 0:
        return 0.0
    return inp.disputes_prior_90d / inp.transactions_prior_90d * 100


def _win_rate(inp: MerchantRiskInput) -> float:
    total = inp.disputes_won + inp.disputes_lost
    if total == 0:
        return 0.5
    return inp.disputes_won / total


def _prior_win_rate(inp: MerchantRiskInput) -> float:
    total = inp.disputes_won_prior + inp.disputes_lost_prior
    if total == 0:
        return 0.5
    return inp.disputes_won_prior / total


def _hdr_risk_score(rate: float) -> tuple[RiskSeverity, str]:
    if rate >= 3.0:
        return RiskSeverity.CRITICAL, f"Rate {rate:.2f}% is at SEVERE tier (≥3.0%)"
    if rate >= 1.5:
        return RiskSeverity.CRITICAL, f"Rate {rate:.2f}% triggers $30/dispute fee (≥1.5%)"
    if rate >= 1.0:
        return RiskSeverity.HIGH, f"Rate {rate:.2f}% at LIMITED tier (≥1.0%)"
    if rate >= 0.75:
        return RiskSeverity.HIGH, f"Rate {rate:.2f}% approaching 1.0% threshold"
    if rate >= 0.50:
        return RiskSeverity.MEDIUM, f"Rate {rate:.2f}% at ELEVATED tier (≥0.5%)"
    return RiskSeverity.LOW, f"Rate {rate:.2f}% — below threshold"


def _concentration_risk(type_splits: list[DisputeTypeSplit]) -> tuple[RiskSeverity, str]:
    if not type_splits:
        return RiskSeverity.LOW, "No type split data"
    top = max(type_splits, key=lambda x: x.pct)
    deviation = top.pct - top.industry_avg_pct
    if top.pct >= 0.70:
        return RiskSeverity.HIGH, f"{top.claim_type} is {top.pct:.0%} of disputes (industry: {top.industry_avg_pct:.0%})"
    if top.pct >= 0.50 and deviation > 0.15:
        return RiskSeverity.MEDIUM, f"{top.claim_type} at {top.pct:.0%} is {deviation:.0%} above industry avg"
    return RiskSeverity.LOW, f"Dispute type distribution is within industry norms"


def _win_rate_trend(current: float, prior: float) -> tuple[RiskTrend, RiskSeverity, str]:
    delta = current - prior
    if delta < -0.10:
        return RiskTrend.WORSENING, RiskSeverity.HIGH, f"Win rate dropped {abs(delta):.0%} vs. prior period"
    if delta < -0.05:
        return RiskTrend.WORSENING, RiskSeverity.MEDIUM, f"Win rate declined {abs(delta):.0%}"
    if delta > 0.05:
        return RiskTrend.IMPROVING, RiskSeverity.HEALTHY, f"Win rate improved {delta:.0%}"
    return RiskTrend.STABLE, RiskSeverity.LOW, "Win rate stable vs. prior period"


@dataclass
class RiskFinding:
    dimension: RiskDimension
    severity: RiskSeverity
    description: str


@dataclass
class MerchantRiskReport:
    """Full merchant risk profile."""
    merchant_id: str
    merchant_category: str

    overall_severity: RiskSeverity
    overall_trend: RiskTrend

    current_dispute_rate_pct: float
    industry_dispute_rate_pct: float
    current_win_rate: float
    industry_win_rate: float

    findings: list[RiskFinding]
    top_recommendations: list[str]

    monitoring_frequency: str    # DAILY / WEEKLY / MONTHLY
    alert_thresholds: dict[str, float]

    summary: str


def _overall_severity(findings: list[RiskFinding]) -> RiskSeverity:
    severities = [f.severity for f in findings]
    for level in (RiskSeverity.CRITICAL, RiskSeverity.HIGH, RiskSeverity.MEDIUM,
                  RiskSeverity.LOW, RiskSeverity.HEALTHY):
        if level in severities:
            return level
    return RiskSeverity.HEALTHY


def profile_merchant_risk(inp: MerchantRiskInput) -> MerchantRiskReport:
    """Build a comprehensive merchant risk profile."""
    current_rate = _current_rate(inp)
    prior_rate = _prior_rate(inp)
    win_rate = _win_rate(inp)
    prior_win = _prior_win_rate(inp)

    findings: list[RiskFinding] = []

    # 1. HDR proximity
    hdr_sev, hdr_msg = _hdr_risk_score(current_rate)
    findings.append(RiskFinding(RiskDimension.HDR_PROXIMITY, hdr_sev, hdr_msg))

    # 2. Concentration
    conc_sev, conc_msg = _concentration_risk(inp.type_splits)
    findings.append(RiskFinding(RiskDimension.CONCENTRATION, conc_sev, conc_msg))

    # 3. Win rate trend
    trend, trend_sev, trend_msg = _win_rate_trend(win_rate, prior_win)
    findings.append(RiskFinding(RiskDimension.WIN_RATE_TREND, trend_sev, trend_msg))

    # 4. Category deviation
    industry_rate = _INDUSTRY_DISPUTE_RATES.get(inp.merchant_category.upper(),
                                                  inp.industry_dispute_rate_pct)
    deviation = current_rate - industry_rate
    if deviation > 0.50:
        cat_sev = RiskSeverity.HIGH
        cat_msg = f"Dispute rate {deviation:+.2f}% above industry avg for {inp.merchant_category}"
    elif deviation > 0.20:
        cat_sev = RiskSeverity.MEDIUM
        cat_msg = f"Slightly above industry avg ({deviation:+.2f}%)"
    else:
        cat_sev = RiskSeverity.LOW
        cat_msg = f"Within normal range for {inp.merchant_category}"
    findings.append(RiskFinding(RiskDimension.CATEGORY_DEVIATION, cat_sev, cat_msg))

    # 5. Repeat buyer dispute rate
    if inp.total_repeat_buyer_disputes > 0:
        repeat_rate = inp.repeat_buyer_dispute_count / inp.total_repeat_buyer_disputes
        if repeat_rate > 0.20:
            rb_sev = RiskSeverity.HIGH
            rb_msg = f"Repeat buyer dispute rate {repeat_rate:.0%} — possible fraud pattern"
        elif repeat_rate > 0.10:
            rb_sev = RiskSeverity.MEDIUM
            rb_msg = f"Repeat buyer dispute rate {repeat_rate:.0%} — monitor"
        else:
            rb_sev = RiskSeverity.LOW
            rb_msg = f"Repeat buyer dispute rate {repeat_rate:.0%} — normal"
        findings.append(RiskFinding(RiskDimension.REPEAT_BUYER_RATE, rb_sev, rb_msg))

    overall = _overall_severity(findings)

    # Monitoring frequency
    if overall == RiskSeverity.CRITICAL:
        freq = "DAILY"
    elif overall == RiskSeverity.HIGH:
        freq = "DAILY"
    elif overall == RiskSeverity.MEDIUM:
        freq = "WEEKLY"
    else:
        freq = "MONTHLY"

    # Alert thresholds
    alerts = {
        "dispute_rate_pct": 1.0,
        "fee_tier_pct": 1.5,
        "win_rate_drop": 0.10,
    }

    # Recommendations
    recs: list[str] = []
    if hdr_sev in (RiskSeverity.CRITICAL, RiskSeverity.HIGH):
        recs.append("Activate dispute surge response — reduce dispute rate immediately")
    if trend == RiskTrend.WORSENING:
        recs.append("Review recent dispute responses — win rate is declining")
    if conc_sev in (RiskSeverity.HIGH, RiskSeverity.MEDIUM):
        recs.append("Investigate root cause of dominant dispute type")
    if overall in (RiskSeverity.CRITICAL, RiskSeverity.HIGH):
        recs.append(f"Increase monitoring to {freq} cadence")
    if not recs:
        recs.append("Continue current dispute management practices")

    summary = (
        f"RiskProfile {inp.merchant_id}: "
        f"rate={current_rate:.2f}% | win={win_rate:.0%} | "
        f"severity={overall.value} | trend={trend.value}"
    )

    return MerchantRiskReport(
        merchant_id=inp.merchant_id,
        merchant_category=inp.merchant_category,
        overall_severity=overall,
        overall_trend=trend,
        current_dispute_rate_pct=round(current_rate, 2),
        industry_dispute_rate_pct=round(industry_rate, 2),
        current_win_rate=round(win_rate, 2),
        industry_win_rate=round(inp.industry_win_rate, 2),
        findings=findings,
        top_recommendations=recs,
        monitoring_frequency=freq,
        alert_thresholds=alerts,
        summary=summary,
    )
