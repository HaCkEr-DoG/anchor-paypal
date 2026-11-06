"""
Merchant Risk Scorer — assess a merchant's overall PayPal dispute risk profile.

A merchant's dispute risk is a composite of their dispute rate history,
HDR zone status, Seller Protection coverage, claim type mix, win rate,
and operational hygiene signals. This module produces:

  1. A composite risk score (0–100, lower = riskier)
  2. A risk tier: HEALTHY / CAUTION / ELEVATED / CRITICAL
  3. Per-dimension sub-scores explaining the risk
  4. Actionable recommendations to reduce risk

Risk dimensions (each scored 0–100):
  DISPUTE_RATE      — Current rolling 90-day dispute rate vs. HDR thresholds
  WIN_RATE          — Historical win rate vs. expected baseline
  CLAIM_MIX         — Proportion of hard-to-beat claim types (SNAD, UNAUTHORIZED)
  RESPONSE_HYGIENE  — % of disputes responded to on time
  SP_COVERAGE       — % of disputes covered by Seller Protection
  EVIDENCE_QUALITY  — Average evidence quality score across disputes

Composite score = weighted average of dimensions
Weights: dispute_rate×30, win_rate×20, claim_mix×15,
         response_hygiene×20, sp_coverage×10, evidence_quality×5

Risk tiers:
  HEALTHY    — Score ≥ 75 (all clear)
  CAUTION    — Score 55–74 (monitor closely)
  ELEVATED   — Score 35–54 (immediate action required)
  CRITICAL   — Score < 35 (high risk of PayPal restrictions)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskTier(str, Enum):
    HEALTHY  = "HEALTHY"
    CAUTION  = "CAUTION"
    ELEVATED = "ELEVATED"
    CRITICAL = "CRITICAL"


class RiskDimension(str, Enum):
    DISPUTE_RATE      = "DISPUTE_RATE"
    WIN_RATE          = "WIN_RATE"
    CLAIM_MIX         = "CLAIM_MIX"
    RESPONSE_HYGIENE  = "RESPONSE_HYGIENE"
    SP_COVERAGE       = "SP_COVERAGE"
    EVIDENCE_QUALITY  = "EVIDENCE_QUALITY"


@dataclass
class MerchantRiskInput:
    """Input for merchant risk scoring."""
    merchant_id: str

    # Dispute rate (rolling 90d)
    disputes_last_90d: int
    transactions_last_90d: int

    # Historical performance
    total_disputes_ever: int
    disputes_won: int            # Total disputes ever won
    disputes_responded_on_time: int   # Of total disputes
    disputes_responded_late_or_missed: int

    # Claim mix (proportions of recent disputes)
    pct_inr: float = 0.50        # 0.0–1.0
    pct_snad: float = 0.30
    pct_unauthorized: float = 0.20

    # SP and evidence signals
    pct_sp_covered: float = 0.60    # % of disputes with SP eligible
    avg_evidence_score: float = 65.0  # 0–100 average evidence quality

    # Context
    months_on_paypal: int = 24
    is_in_hdr_zone: bool = False


@dataclass
class DimensionScore:
    """Score for one risk dimension."""
    dimension: RiskDimension
    score: float            # 0–100
    weight: float           # 0.0–1.0
    weighted_contribution: float
    description: str
    flag: str               # "OK" / "WARN" / "ALERT"


@dataclass
class MerchantRiskReport:
    """
    Complete merchant risk assessment.
    """
    merchant_id: str

    # Overall
    composite_score: float         # 0–100 (higher = safer)
    risk_tier: RiskTier

    # Per-dimension breakdown
    dimensions: list[DimensionScore]

    # Metrics
    dispute_rate_pct: float
    historical_win_rate: float
    response_rate_pct: float

    # Recommendations
    top_risks: list[str]
    recommendations: list[str]

    summary: str


# ─────────────────────────────────────────────────────────────
# Dimension scorers
# ─────────────────────────────────────────────────────────────

def _score_dispute_rate(inp: MerchantRiskInput) -> tuple[float, str, str]:
    if inp.transactions_last_90d == 0:
        return 50.0, "No transaction data", "WARN"
    rate = inp.disputes_last_90d / inp.transactions_last_90d

    if rate < 0.003:    # <0.30% — healthy
        return 100.0, f"Dispute rate {rate:.2%} — well below 0.50% threshold", "OK"
    if rate < 0.005:    # 0.30–0.50%
        return 80.0, f"Dispute rate {rate:.2%} — healthy, approaching 0.50%", "OK"
    if rate < 0.010:    # 0.50–1.00% elevated
        return 55.0, f"Dispute rate {rate:.2%} — in ELEVATED zone (0.50%–1.00%)", "WARN"
    if rate < 0.015:    # 1.00–1.50% limited
        return 35.0, f"Dispute rate {rate:.2%} — in LIMITED zone (1.00%–1.50%)", "ALERT"
    if rate < 0.030:    # 1.50–3.00% surcharge
        return 20.0, f"Dispute rate {rate:.2%} — SURCHARGE zone, $30/dispute fee active", "ALERT"
    return 5.0, f"Dispute rate {rate:.2%} — SEVERE/TERMINAL zone", "ALERT"


def _score_win_rate(inp: MerchantRiskInput) -> tuple[float, str, str]:
    if inp.total_disputes_ever == 0:
        return 60.0, "No dispute history", "OK"
    win_rate = inp.disputes_won / inp.total_disputes_ever

    if win_rate >= 0.65:
        return 100.0, f"Win rate {win_rate:.0%} — excellent (baseline ~55%)", "OK"
    if win_rate >= 0.50:
        return 80.0, f"Win rate {win_rate:.0%} — good", "OK"
    if win_rate >= 0.35:
        return 55.0, f"Win rate {win_rate:.0%} — below baseline — evidence issues likely", "WARN"
    if win_rate >= 0.20:
        return 30.0, f"Win rate {win_rate:.0%} — poor — systemic evidence or compliance issue", "ALERT"
    return 10.0, f"Win rate {win_rate:.0%} — very poor — requires immediate review", "ALERT"


def _score_claim_mix(inp: MerchantRiskInput) -> tuple[float, str, str]:
    # Hard claims: SNAD and UNAUTHORIZED are hardest to win
    hard_pct = inp.pct_snad + inp.pct_unauthorized

    if hard_pct <= 0.20:
        return 100.0, f"Hard claim mix {hard_pct:.0%} — low", "OK"
    if hard_pct <= 0.35:
        return 75.0, f"Hard claim mix {hard_pct:.0%} — moderate", "OK"
    if hard_pct <= 0.55:
        return 50.0, f"Hard claim mix {hard_pct:.0%} — elevated — review listing accuracy", "WARN"
    return 20.0, f"Hard claim mix {hard_pct:.0%} — dominant — systemic product/fraud issue", "ALERT"


def _score_response_hygiene(inp: MerchantRiskInput) -> tuple[float, str, str]:
    total = inp.disputes_responded_on_time + inp.disputes_responded_late_or_missed
    if total == 0:
        return 60.0, "No response data", "WARN"
    on_time_pct = inp.disputes_responded_on_time / total

    if on_time_pct >= 0.95:
        return 100.0, f"On-time response rate {on_time_pct:.0%} — excellent", "OK"
    if on_time_pct >= 0.85:
        return 75.0, f"On-time response rate {on_time_pct:.0%} — good", "OK"
    if on_time_pct >= 0.70:
        return 45.0, f"On-time response rate {on_time_pct:.0%} — missing deadlines", "WARN"
    return 15.0, f"On-time response rate {on_time_pct:.0%} — critical — many missed deadlines", "ALERT"


def _score_sp_coverage(inp: MerchantRiskInput) -> tuple[float, str, str]:
    if inp.pct_sp_covered >= 0.80:
        return 100.0, f"SP coverage {inp.pct_sp_covered:.0%} — excellent", "OK"
    if inp.pct_sp_covered >= 0.60:
        return 75.0, f"SP coverage {inp.pct_sp_covered:.0%} — good", "OK"
    if inp.pct_sp_covered >= 0.40:
        return 50.0, f"SP coverage {inp.pct_sp_covered:.0%} — moderate — shipping improvements needed", "WARN"
    return 25.0, f"SP coverage {inp.pct_sp_covered:.0%} — low — most disputes unprotected", "ALERT"


def _score_evidence_quality(inp: MerchantRiskInput) -> tuple[float, str, str]:
    s = inp.avg_evidence_score
    if s >= 80:
        return 100.0, f"Avg evidence score {s:.0f}/100 — strong", "OK"
    if s >= 65:
        return 75.0, f"Avg evidence score {s:.0f}/100 — adequate", "OK"
    if s >= 50:
        return 45.0, f"Avg evidence score {s:.0f}/100 — weak — improve documentation", "WARN"
    return 20.0, f"Avg evidence score {s:.0f}/100 — poor — systematic evidence issue", "ALERT"


_WEIGHTS = {
    RiskDimension.DISPUTE_RATE:      0.30,
    RiskDimension.WIN_RATE:          0.20,
    RiskDimension.CLAIM_MIX:         0.15,
    RiskDimension.RESPONSE_HYGIENE:  0.20,
    RiskDimension.SP_COVERAGE:       0.10,
    RiskDimension.EVIDENCE_QUALITY:  0.05,
}


def score_merchant_risk(inp: MerchantRiskInput) -> MerchantRiskReport:
    """
    Compute a composite merchant risk score and generate recommendations.
    """
    dim_data = {
        RiskDimension.DISPUTE_RATE:     _score_dispute_rate(inp),
        RiskDimension.WIN_RATE:         _score_win_rate(inp),
        RiskDimension.CLAIM_MIX:        _score_claim_mix(inp),
        RiskDimension.RESPONSE_HYGIENE: _score_response_hygiene(inp),
        RiskDimension.SP_COVERAGE:      _score_sp_coverage(inp),
        RiskDimension.EVIDENCE_QUALITY: _score_evidence_quality(inp),
    }

    dimensions: list[DimensionScore] = []
    composite = 0.0

    for dim, (score, desc, flag) in dim_data.items():
        w = _WEIGHTS[dim]
        contrib = score * w
        composite += contrib
        dimensions.append(DimensionScore(
            dimension=dim,
            score=round(score, 1),
            weight=w,
            weighted_contribution=round(contrib, 2),
            description=desc,
            flag=flag,
        ))

    composite = round(composite, 1)

    tier = (RiskTier.HEALTHY  if composite >= 75
            else RiskTier.CAUTION  if composite >= 55
            else RiskTier.ELEVATED if composite >= 35
            else RiskTier.CRITICAL)

    # Metrics
    dr_pct = (inp.disputes_last_90d / inp.transactions_last_90d
              if inp.transactions_last_90d > 0 else 0.0)
    win_rate = (inp.disputes_won / inp.total_disputes_ever
                if inp.total_disputes_ever > 0 else 0.0)
    total_resp = inp.disputes_responded_on_time + inp.disputes_responded_late_or_missed
    resp_rate = (inp.disputes_responded_on_time / total_resp if total_resp > 0 else 1.0)

    # Top risks
    alert_dims = [d for d in dimensions if d.flag == "ALERT"]
    warn_dims  = [d for d in dimensions if d.flag == "WARN"]
    top_risks  = [d.description for d in sorted(alert_dims + warn_dims,
                                                  key=lambda d: d.score)][:4]

    # Recommendations
    recs: list[str] = []
    if tier in (RiskTier.CRITICAL, RiskTier.ELEVATED):
        recs.append("Immediate action required — dispute rate or win rate is critically low")
    if inp.is_in_hdr_zone:
        recs.append("HDR zone active — prioritize resolving open disputes to lower rate")
    dr_score = dim_data[RiskDimension.DISPUTE_RATE][0]
    if dr_score < 55:
        recs.append("Review product listings and customer communication to reduce dispute volume")
    if dim_data[RiskDimension.RESPONSE_HYGIENE][0] < 75:
        recs.append("Set up automated dispute alerts — missed deadlines cost wins")
    if dim_data[RiskDimension.SP_COVERAGE][0] < 75:
        recs.append("Always ship to PayPal-confirmed address with tracked carrier to maximize SP")
    if dim_data[RiskDimension.WIN_RATE][0] < 55:
        recs.append("Improve evidence collection: tracking, delivery photos, signature for ≥$750")
    if dim_data[RiskDimension.CLAIM_MIX][0] < 55:
        recs.append("SNAD claims dominate — audit product descriptions for accuracy")

    summary = (
        f"Risk {inp.merchant_id}: score={composite:.0f}/100 | tier={tier.value} | "
        f"dr={dr_pct:.2%} | win={win_rate:.0%} | resp={resp_rate:.0%} | "
        f"sp={inp.pct_sp_covered:.0%}"
    )

    return MerchantRiskReport(
        merchant_id=inp.merchant_id,
        composite_score=composite,
        risk_tier=tier,
        dimensions=dimensions,
        dispute_rate_pct=round(dr_pct * 100, 4),
        historical_win_rate=round(win_rate, 4),
        response_rate_pct=round(resp_rate * 100, 2),
        top_risks=top_risks,
        recommendations=recs,
        summary=summary,
    )
