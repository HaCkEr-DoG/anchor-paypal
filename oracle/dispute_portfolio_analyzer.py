"""
Dispute Portfolio Analyzer — portfolio-level risk and prioritization view
across all open disputes for a single merchant.

Computes:
  - Total amount at risk, SP-covered vs unprotected
  - Expected value if all disputes are fought or all accepted
  - Breakdown by reason code (sorted by total amount)
  - Urgency buckets (CRITICAL / HIGH / MODERATE / LOW)
  - Top 5 priority disputes with priority_rank
  - HDR impact count and rate
  - Portfolio risk level (LOW / MODERATE / HIGH / CRITICAL)
  - Portfolio average win probability

EV formulas:
  fought:   win_prob * amount - 15 - (1 - win_prob) * 20
  accepted: -amount

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PortfolioRiskLevel(str, Enum):
    LOW      = "LOW"
    MODERATE = "MODERATE"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"


class UrgencyBucket(str, Enum):
    CRITICAL = "CRITICAL"   # days_remaining <= 1
    HIGH     = "HIGH"       # <= 3
    MODERATE = "MODERATE"   # <= 7
    LOW      = "LOW"        # > 7


_RESPONSE_COST = 15.0
_CB_FEE        = 20.0


def _urgency(days: int) -> UrgencyBucket:
    if days <= 1:
        return UrgencyBucket.CRITICAL
    if days <= 3:
        return UrgencyBucket.HIGH
    if days <= 7:
        return UrgencyBucket.MODERATE
    return UrgencyBucket.LOW


@dataclass
class PortfolioDispute:
    dispute_id: str
    reason_code: str
    network: str
    amount: float
    days_remaining: int
    win_probability: float
    is_in_sp: bool = False
    is_hdr_counting: bool = False
    response_cost: float = 15.0
    chargeback_fee: float = 20.0


@dataclass
class PortfolioAnalysisInput:
    merchant_id: str
    disputes: list[PortfolioDispute] = field(default_factory=list)
    total_transactions_90d: int = 1
    total_transaction_value_90d: float = 0.0


@dataclass
class ReasonCodeBreakdown:
    reason_code: str
    count: int
    total_amount: float
    avg_win_probability: float


@dataclass
class UrgencyBreakdown:
    bucket: UrgencyBucket
    count: int
    total_amount: float


@dataclass
class TopDispute:
    dispute_id: str
    priority_rank: int
    amount: float
    days_remaining: int
    win_probability: float
    reason_code: str
    ev_fight: float = 0.0
    ev_accept: float = 0.0
    priority_rationale: str = ""


@dataclass
class PortfolioAnalysisReport:
    merchant_id: str

    total_disputes: int
    total_amount_at_risk: float
    sp_covered_amount: float
    unprotected_amount: float

    total_ev_if_all_fought: float
    total_ev_if_all_accepted: float

    by_reason_code: list[ReasonCodeBreakdown]
    by_urgency: list[UrgencyBreakdown]
    top_disputes: list[TopDispute]

    hdr_dispute_count: int
    hdr_impact_rate: float

    portfolio_risk_level: PortfolioRiskLevel
    portfolio_avg_win_prob: float

    expected_net_recovery: float = 0.0
    risk_flags: list = field(default_factory=list)

    summary: str = ""


def analyze_dispute_portfolio(inp: PortfolioAnalysisInput) -> PortfolioAnalysisReport:
    disputes = inp.disputes
    n = len(disputes)

    if n == 0:
        return PortfolioAnalysisReport(
            merchant_id=inp.merchant_id,
            total_disputes=0,
            total_amount_at_risk=0.0,
            sp_covered_amount=0.0,
            unprotected_amount=0.0,
            total_ev_if_all_fought=0.0,
            total_ev_if_all_accepted=0.0,
            by_reason_code=[],
            by_urgency=[],
            top_disputes=[],
            hdr_dispute_count=0,
            hdr_impact_rate=0.0,
            portfolio_risk_level=PortfolioRiskLevel.LOW,
            portfolio_avg_win_prob=0.0,
            summary=f"Portfolio {inp.merchant_id}: 0 disputes",
        )

    total_amount = sum(d.amount for d in disputes)
    sp_amount = sum(d.amount for d in disputes if d.is_in_sp)
    unprotected = total_amount - sp_amount

    ev_fought = sum(
        d.win_probability * d.amount - _RESPONSE_COST - (1 - d.win_probability) * _CB_FEE
        for d in disputes
    )
    ev_accepted = -total_amount

    # ── Reason code breakdown ─────────────────────────────────────────────────
    rc_data: dict[str, dict] = {}
    for d in disputes:
        rc = d.reason_code
        if rc not in rc_data:
            rc_data[rc] = {"count": 0, "total_amount": 0.0, "win_probs": []}
        rc_data[rc]["count"] += 1
        rc_data[rc]["total_amount"] += d.amount
        rc_data[rc]["win_probs"].append(d.win_probability)

    by_rc = sorted([
        ReasonCodeBreakdown(
            reason_code=rc,
            count=v["count"],
            total_amount=round(v["total_amount"], 2),
            avg_win_probability=round(sum(v["win_probs"]) / len(v["win_probs"]), 4),
        )
        for rc, v in rc_data.items()
    ], key=lambda x: x.total_amount, reverse=True)

    # ── Urgency breakdown ─────────────────────────────────────────────────────
    urg_data: dict[UrgencyBucket, dict] = {}
    for d in disputes:
        b = _urgency(d.days_remaining)
        if b not in urg_data:
            urg_data[b] = {"count": 0, "total_amount": 0.0}
        urg_data[b]["count"] += 1
        urg_data[b]["total_amount"] += d.amount

    by_urgency = [
        UrgencyBreakdown(bucket=b, count=v["count"], total_amount=round(v["total_amount"], 2))
        for b, v in urg_data.items()
    ]

    # ── Top disputes (by amount × win_probability, at most 5) ─────────────────
    scored = sorted(disputes, key=lambda d: d.amount * d.win_probability, reverse=True)
    top_disputes = [
        TopDispute(
            dispute_id=d.dispute_id,
            priority_rank=rank,
            amount=d.amount,
            days_remaining=d.days_remaining,
            win_probability=d.win_probability,
            reason_code=d.reason_code,
            ev_fight=round(d.win_probability * d.amount - _RESPONSE_COST - (1 - d.win_probability) * _CB_FEE, 2),
            ev_accept=round(-d.amount, 2),
            priority_rationale=f"rank={rank} amount=${d.amount:.0f} p_win={d.win_probability:.0%}",
        )
        for rank, d in enumerate(scored[:5], start=1)
    ]

    # ── HDR ───────────────────────────────────────────────────────────────────
    hdr_count = sum(1 for d in disputes if d.is_hdr_counting)
    hdr_rate = round(hdr_count / max(inp.total_transactions_90d, 1), 6)

    # ── Portfolio risk level ──────────────────────────────────────────────────
    dispute_rate = n / max(inp.total_transactions_90d, 1)
    if dispute_rate >= 0.06 or n >= 60:
        risk_level = PortfolioRiskLevel.CRITICAL
    elif dispute_rate >= 0.03 or n >= 30:
        risk_level = PortfolioRiskLevel.HIGH
    elif dispute_rate >= 0.01 or n >= 10:
        risk_level = PortfolioRiskLevel.MODERATE
    else:
        risk_level = PortfolioRiskLevel.LOW

    avg_win_prob = round(sum(d.win_probability for d in disputes) / n, 4)

    summary = (
        f"Portfolio {inp.merchant_id}: {n} disputes | "
        f"${total_amount:,.0f} at risk | "
        f"ev_fought=${ev_fought:+,.0f} | "
        f"risk={risk_level.value}"
    )

    # Build risk flags
    risk_flags = []
    if risk_level in (PortfolioRiskLevel.CRITICAL, PortfolioRiskLevel.HIGH):
        risk_flags.append(f"Portfolio risk: {risk_level.value}")
    critical_disputes = sum(1 for d in disputes if d.days_remaining <= 1)
    if critical_disputes:
        risk_flags.append(f"{critical_disputes} dispute(s) have ≤1 day remaining")
    if hdr_rate >= 0.010:
        risk_flags.append(f"HDR rate {hdr_rate:.2%} at or above threshold")

    expected_net = round(ev_fought + ev_accepted, 2)  # net if fighting all vs accepting all

    return PortfolioAnalysisReport(
        merchant_id=inp.merchant_id,
        total_disputes=n,
        total_amount_at_risk=round(total_amount, 2),
        sp_covered_amount=round(sp_amount, 2),
        unprotected_amount=round(unprotected, 2),
        total_ev_if_all_fought=round(ev_fought, 2),
        total_ev_if_all_accepted=round(ev_accepted, 2),
        by_reason_code=by_rc,
        by_urgency=by_urgency,
        top_disputes=top_disputes,
        hdr_dispute_count=hdr_count,
        hdr_impact_rate=hdr_rate,
        portfolio_risk_level=risk_level,
        portfolio_avg_win_prob=avg_win_prob,
        expected_net_recovery=expected_net,
        risk_flags=risk_flags,
        summary=summary,
    )
