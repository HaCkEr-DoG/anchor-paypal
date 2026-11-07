"""
Dispute Portfolio Risk Scorer — aggregate risk analysis of a merchant's
full open dispute portfolio.

Where individual dispute oracles score one dispute at a time, this module
looks at the whole portfolio and identifies:
  1. Total financial exposure (worst-case + expected-case losses)
  2. Concentration risk — dominance of a single claim type or amount tier
  3. Win-probability distribution — spread of easy vs hard disputes
  4. Portfolio risk level: LOW / MODERATE / HIGH / SEVERE
  5. Actionable recommendations for the top risk driver

Portfolio EV formula (expected case loss):
  expected_loss = Σ (1 − p_win_i) × amount_i   for each dispute i

Concentration risk:
  If any single claim type exceeds 50% of dispute count → concentration_flag
  If a single dispute > 30% of total exposure → size_concentration_flag

PayPal benefit:
  - Merchants see risk holistically, not just one-at-a-time
  - Concentration risk signals fraud campaigns before they escalate
  - Portfolio EV helps merchants triage limited response bandwidth
  - Aligns merchant-level risk with PayPal's chargeback monitoring view
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


# ── Risk level thresholds ─────────────────────────────────────────────────────

_RISK_THRESHOLDS = {
    "LOW":      (0.0,   500.0),     # expected loss < $500
    "MODERATE": (500.0, 2000.0),    # $500–$2000
    "HIGH":     (2000.0, 5000.0),   # $2000–$5000
    "SEVERE":   (5000.0, float("inf")),
}

_CONCENTRATION_THRESHOLD = 0.50   # >50% same claim type → concentration risk
_SIZE_CONCENTRATION_THRESHOLD = 0.30  # single dispute >30% of total amount


# ── Enums ─────────────────────────────────────────────────────────────────────

class PortfolioRiskLevel(str, Enum):
    LOW      = "LOW"
    MODERATE = "MODERATE"
    HIGH     = "HIGH"
    SEVERE   = "SEVERE"


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class DisputeSnapshot:
    dispute_id: str
    claim_type: str              # INR | SNAD | UNAUTH | OTHER
    amount_usd: float
    win_probability: float
    days_until_deadline: int


@dataclass
class ConcentrationRisk:
    flag: bool
    dominant_claim_type: str     # e.g. "UNAUTH"
    dominant_fraction: float     # e.g. 0.65 = 65% of disputes are UNAUTH
    description: str


@dataclass
class PortfolioRiskReport:
    merchant_id: str
    total_disputes: int
    total_exposure_usd: float          # worst-case: sum of all amounts
    expected_loss_usd: float           # Σ (1−p) × amount
    expected_recovery_usd: float       # Σ p × amount
    portfolio_risk_level: PortfolioRiskLevel
    concentration: ConcentrationRisk
    critical_disputes: List[str]       # dispute_ids with deadline ≤ 2 days
    high_value_disputes: List[str]     # dispute_ids in top 20% by amount
    avg_win_probability: float
    recommendation: str = ""
    risk_factors: List[str] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _risk_level(expected_loss: float) -> PortfolioRiskLevel:
    for level_name, (lo, hi) in _RISK_THRESHOLDS.items():
        if lo <= expected_loss < hi:
            return PortfolioRiskLevel(level_name)
    return PortfolioRiskLevel.SEVERE


def _concentration(disputes: List[DisputeSnapshot]) -> ConcentrationRisk:
    if not disputes:
        return ConcentrationRisk(False, "", 0.0, "No disputes in portfolio.")
    counts: dict[str, int] = {}
    for d in disputes:
        ct = d.claim_type.upper()
        counts[ct] = counts.get(ct, 0) + 1
    dominant = max(counts, key=lambda k: counts[k])
    fraction = counts[dominant] / len(disputes)
    flag = fraction > _CONCENTRATION_THRESHOLD
    desc = (
        f"{dominant} disputes are {fraction:.0%} of the portfolio"
        + (" — concentration risk." if flag else " — balanced mix.")
    )
    return ConcentrationRisk(flag, dominant, round(fraction, 4), desc)


def _top_pct_by_amount(disputes: List[DisputeSnapshot], pct: float = 0.20) -> List[str]:
    """Return dispute_ids in the top `pct` fraction by amount."""
    if not disputes:
        return []
    cutoff = max(1, int(len(disputes) * pct))
    sorted_d = sorted(disputes, key=lambda d: -d.amount_usd)
    return [d.dispute_id for d in sorted_d[:cutoff]]


# ── Public API ────────────────────────────────────────────────────────────────

def score_portfolio(
    merchant_id: str,
    disputes: List[DisputeSnapshot],
) -> PortfolioRiskReport:
    """Score the full dispute portfolio and return a risk report."""
    if not disputes:
        return PortfolioRiskReport(
            merchant_id=merchant_id,
            total_disputes=0,
            total_exposure_usd=0.0,
            expected_loss_usd=0.0,
            expected_recovery_usd=0.0,
            portfolio_risk_level=PortfolioRiskLevel.LOW,
            concentration=ConcentrationRisk(False, "", 0.0, "No open disputes."),
            critical_disputes=[],
            high_value_disputes=[],
            avg_win_probability=0.0,
            recommendation="No open disputes. Portfolio is clean.",
        )

    total_exposure = round(sum(d.amount_usd for d in disputes), 2)
    expected_loss = round(sum((1 - d.win_probability) * d.amount_usd for d in disputes), 2)
    expected_recovery = round(sum(d.win_probability * d.amount_usd for d in disputes), 2)
    avg_wp = round(sum(d.win_probability for d in disputes) / len(disputes), 4)

    risk_level = _risk_level(expected_loss)
    conc = _concentration(disputes)
    critical = [d.dispute_id for d in disputes if d.days_until_deadline <= 2]
    high_value = _top_pct_by_amount(disputes)

    # Size concentration check
    if total_exposure > 0:
        max_single = max(d.amount_usd for d in disputes)
        size_conc_flag = max_single / total_exposure > _SIZE_CONCENTRATION_THRESHOLD
    else:
        size_conc_flag = False

    # Risk factors
    risk_factors: List[str] = []
    if critical:
        risk_factors.append(f"{len(critical)} dispute(s) have deadline ≤ 2 days — respond NOW.")
    if conc.flag:
        risk_factors.append(
            f"Claim type concentration: {conc.dominant_fraction:.0%} are {conc.dominant_claim_type}. "
            "Investigate for fraud patterns or systemic issue."
        )
    if size_conc_flag:
        risk_factors.append(
            "Single large dispute dominates portfolio exposure (>30%). Prioritise that dispute."
        )
    if avg_wp < 0.40:
        risk_factors.append(
            f"Low average win probability ({avg_wp:.0%}) across portfolio — review evidence quality."
        )

    # Recommendation
    if risk_level == PortfolioRiskLevel.SEVERE:
        rec = (
            f"SEVERE: Expected loss ${expected_loss:.2f} across {len(disputes)} disputes. "
            f"{'Address ' + str(len(critical)) + ' critical deadline(s) immediately. ' if critical else ''}"
            "Activate triage mode: prioritise CRITICAL deadlines, then highest-EV disputes."
        )
    elif risk_level == PortfolioRiskLevel.HIGH:
        rec = (
            f"HIGH: ${expected_loss:.2f} at risk. "
            "Focus on the top 20% highest-value disputes and any with deadlines ≤ 2 days."
        )
    elif risk_level == PortfolioRiskLevel.MODERATE:
        rec = (
            f"MODERATE: ${expected_loss:.2f} expected loss across {len(disputes)} disputes. "
            "Review claim mix and ensure evidence is assembled for HIGH-priority disputes."
        )
    else:
        rec = (
            f"LOW: Expected loss ${expected_loss:.2f}. "
            "Portfolio is under control. Maintain response quality to keep win rate high."
        )

    return PortfolioRiskReport(
        merchant_id=merchant_id,
        total_disputes=len(disputes),
        total_exposure_usd=total_exposure,
        expected_loss_usd=expected_loss,
        expected_recovery_usd=expected_recovery,
        portfolio_risk_level=risk_level,
        concentration=conc,
        critical_disputes=critical,
        high_value_disputes=high_value,
        avg_win_probability=avg_wp,
        recommendation=rec,
        risk_factors=risk_factors,
    )
