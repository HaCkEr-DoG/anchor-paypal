"""
Dispute Portfolio Analyzer — aggregate metrics across a merchant's open
dispute portfolio to surface systemic risk, prioritization opportunities,
and revenue-at-risk summaries.

Given a list of disputes, this oracle computes:
  - Total revenue at risk and expected recovery
  - Breakdown by reason code, network, urgency
  - HDR-impact disputes (cases that move the merchant's dispute rate)
  - Top 5 disputes by EV impact
  - Portfolio win rate and EV estimates

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PortfolioRiskLevel(str, Enum):
    LOW      = "LOW"      # < 5 open disputes or low revenue exposure
    MODERATE = "MODERATE" # 5–20 disputes or moderate exposure
    HIGH     = "HIGH"     # 21–50 or high exposure
    CRITICAL = "CRITICAL" # > 50 or severe HDR risk


class UrgencyBucket(str, Enum):
    CRITICAL = "CRITICAL"   # ≤ 2 days
    HIGH     = "HIGH"       # 3–5 days
    MODERATE = "MODERATE"   # 6–14 days
    LOW      = "LOW"        # > 14 days
    EXPIRED  = "EXPIRED"    # Past deadline


@dataclass
class PortfolioDispute:
    """Lightweight descriptor of one dispute for portfolio analysis."""
    dispute_id: str
    reason_code: str
    network: str
    amount: float
    days_remaining: int
    win_probability: float
    response_cost: float = 15.0
    chargeback_fee: float = 20.0
    is_hdr_counting: bool = True   # Counts toward 90-day dispute rate
    is_in_sp: bool = False         # Under Seller Protection


@dataclass
class ReasonCodeBreakdown:
    reason_code: str
    count: int
    total_amount: float
    avg_win_prob: float
    expected_recovery: float


@dataclass
class NetworkBreakdown:
    network: str
    count: int
    total_amount: float
    avg_win_prob: float


@dataclass
class UrgencyBreakdown:
    bucket: UrgencyBucket
    count: int
    total_amount: float


@dataclass
class PrioritizedDispute:
    """A dispute ranked by its EV impact in the portfolio."""
    dispute_id: str
    reason_code: str
    amount: float
    days_remaining: int
    win_probability: float
    ev_fight: float
    ev_accept: float
    priority_rank: int
    priority_rationale: str


@dataclass
class PortfolioAnalysisInput:
    """Input: list of open disputes for one merchant."""
    merchant_id: str
    disputes: list[PortfolioDispute] = field(default_factory=list)
    total_transactions_90d: int = 1000
    total_transaction_value_90d: float = 50000.0


@dataclass
class DisputePortfolioReport:
    """Full portfolio analytics report."""
    merchant_id: str
    total_disputes: int

    # Revenue at risk
    total_amount_at_risk: float
    sp_covered_amount: float          # Amount under Seller Protection
    unprotected_amount: float         # Amount not under SP

    # EV summary
    total_ev_if_all_fought: float
    total_ev_if_all_accepted: float
    expected_net_recovery: float      # EV sum across portfolio

    # Win rate estimates
    portfolio_avg_win_prob: float
    hdr_dispute_count: int            # Disputes counting toward dispute rate
    hdr_impact_rate: float            # hdr_disputes / total_transactions_90d

    # Breakdowns
    by_reason_code: list[ReasonCodeBreakdown]
    by_network: list[NetworkBreakdown]
    by_urgency: list[UrgencyBreakdown]

    # Priority queue
    top_disputes: list[PrioritizedDispute]  # Up to 5, ranked by EV impact

    # Risk
    portfolio_risk_level: PortfolioRiskLevel
    risk_flags: list[str]

    summary: str


def _urgency(days_remaining: int) -> UrgencyBucket:
    if days_remaining < 0:
        return UrgencyBucket.EXPIRED
    elif days_remaining <= 2:
        return UrgencyBucket.CRITICAL
    elif days_remaining <= 5:
        return UrgencyBucket.HIGH
    elif days_remaining <= 14:
        return UrgencyBucket.MODERATE
    else:
        return UrgencyBucket.LOW


def _ev_fight(d: PortfolioDispute) -> float:
    p = d.win_probability
    rc = d.response_cost
    cb = d.chargeback_fee
    return round(p * (d.amount - rc) - (1 - p) * (d.amount + cb + rc), 2)


def _ev_accept(d: PortfolioDispute) -> float:
    return round(-(d.amount + d.chargeback_fee), 2)


def analyze_dispute_portfolio(inp: PortfolioAnalysisInput) -> DisputePortfolioReport:
    """Compute portfolio-level analytics across all open disputes."""
    disputes = inp.disputes
    n = len(disputes)

    if n == 0:
        return DisputePortfolioReport(
            merchant_id=inp.merchant_id,
            total_disputes=0,
            total_amount_at_risk=0.0, sp_covered_amount=0.0, unprotected_amount=0.0,
            total_ev_if_all_fought=0.0, total_ev_if_all_accepted=0.0,
            expected_net_recovery=0.0, portfolio_avg_win_prob=0.0,
            hdr_dispute_count=0, hdr_impact_rate=0.0,
            by_reason_code=[], by_network=[], by_urgency=[],
            top_disputes=[], portfolio_risk_level=PortfolioRiskLevel.LOW,
            risk_flags=[],
            summary=f"Portfolio {inp.merchant_id}: 0 open disputes",
        )

    # ── Revenue at risk ───────────────────────────────────────────────────────
    total_amount = round(sum(d.amount for d in disputes), 2)
    sp_covered   = round(sum(d.amount for d in disputes if d.is_in_sp), 2)
    unprotected  = round(total_amount - sp_covered, 2)

    # ── EV sums ───────────────────────────────────────────────────────────────
    ev_fought   = round(sum(_ev_fight(d) for d in disputes), 2)
    ev_accepted = round(sum(_ev_accept(d) for d in disputes), 2)

    # Expected recovery: for each dispute, use max(EV fight, EV accept)
    expected_net = round(sum(max(_ev_fight(d), _ev_accept(d)) for d in disputes), 2)

    # ── Win prob average ──────────────────────────────────────────────────────
    avg_wp = round(sum(d.win_probability for d in disputes) / n, 4)

    # ── HDR impact ────────────────────────────────────────────────────────────
    hdr_count = sum(1 for d in disputes if d.is_hdr_counting)
    hdr_rate  = round(hdr_count / max(inp.total_transactions_90d, 1), 6)

    # ── Reason code breakdown ─────────────────────────────────────────────────
    rc_groups: dict[str, list[PortfolioDispute]] = {}
    for d in disputes:
        rc_groups.setdefault(d.reason_code, []).append(d)

    by_rc: list[ReasonCodeBreakdown] = []
    for rc, grp in sorted(rc_groups.items()):
        amt   = round(sum(d.amount for d in grp), 2)
        awp   = round(sum(d.win_probability for d in grp) / len(grp), 4)
        recov = round(sum(max(_ev_fight(d), 0) for d in grp), 2)
        by_rc.append(ReasonCodeBreakdown(rc, len(grp), amt, awp, recov))
    by_rc.sort(key=lambda x: x.total_amount, reverse=True)

    # ── Network breakdown ─────────────────────────────────────────────────────
    net_groups: dict[str, list[PortfolioDispute]] = {}
    for d in disputes:
        net_groups.setdefault(d.network.upper(), []).append(d)

    by_net: list[NetworkBreakdown] = []
    for net, grp in sorted(net_groups.items()):
        amt = round(sum(d.amount for d in grp), 2)
        awp = round(sum(d.win_probability for d in grp) / len(grp), 4)
        by_net.append(NetworkBreakdown(net, len(grp), amt, awp))
    by_net.sort(key=lambda x: x.total_amount, reverse=True)

    # ── Urgency breakdown ─────────────────────────────────────────────────────
    urg_groups: dict[UrgencyBucket, list[PortfolioDispute]] = {}
    for d in disputes:
        ub = _urgency(d.days_remaining)
        urg_groups.setdefault(ub, []).append(d)

    by_urg: list[UrgencyBreakdown] = []
    for ub in UrgencyBucket:
        grp = urg_groups.get(ub, [])
        if grp:
            by_urg.append(UrgencyBreakdown(ub, len(grp), round(sum(d.amount for d in grp), 2)))

    # ── Prioritized disputes ──────────────────────────────────────────────────
    # Rank by absolute EV delta: |EV(fight) - EV(accept)| — biggest decision impact
    def priority_score(d: PortfolioDispute) -> float:
        urgency_bonus = {UrgencyBucket.CRITICAL: 1000, UrgencyBucket.HIGH: 100, UrgencyBucket.MODERATE: 10,
                         UrgencyBucket.LOW: 0, UrgencyBucket.EXPIRED: -1000}
        return abs(_ev_fight(d) - _ev_accept(d)) + urgency_bonus.get(_urgency(d.days_remaining), 0)

    ranked = sorted(disputes, key=priority_score, reverse=True)[:5]
    top: list[PrioritizedDispute] = []
    for rank, d in enumerate(ranked, 1):
        ef = _ev_fight(d)
        ea = _ev_accept(d)
        rationale = (
            f"EV delta ${abs(ef - ea):.0f}; "
            f"{'FIGHT' if ef > ea else 'ACCEPT'}; "
            f"urgency={_urgency(d.days_remaining).value}"
        )
        top.append(PrioritizedDispute(
            d.dispute_id, d.reason_code, d.amount,
            d.days_remaining, d.win_probability, ef, ea, rank, rationale,
        ))

    # ── Risk level ────────────────────────────────────────────────────────────
    if n > 50 or hdr_rate >= 0.03:
        risk_level = PortfolioRiskLevel.CRITICAL
    elif n > 20 or hdr_rate >= 0.015 or unprotected > 10000:
        risk_level = PortfolioRiskLevel.HIGH
    elif n > 5 or unprotected > 2000:
        risk_level = PortfolioRiskLevel.MODERATE
    else:
        risk_level = PortfolioRiskLevel.LOW

    flags: list[str] = []
    if hdr_rate >= 0.015:
        flags.append(f"HDR rate {hdr_rate:.2%} at or above FEE_TIER threshold")
    critical_count = sum(1 for d in disputes if _urgency(d.days_remaining) == UrgencyBucket.CRITICAL)
    if critical_count >= 1:
        flags.append(f"{critical_count} dispute(s) expire in ≤ 2 days — immediate action required")
    if avg_wp < 0.40:
        flags.append(f"Portfolio avg win probability {avg_wp:.0%} is low — review evidence standards")

    summary = (
        f"Portfolio {inp.merchant_id}: "
        f"{n} disputes | "
        f"${total_amount:.0f} at risk | "
        f"ev_fought=${ev_fought:.0f} ev_accepted=${ev_accepted:.0f} | "
        f"hdr_rate={hdr_rate:.2%} | risk={risk_level.value}"
    )

    return DisputePortfolioReport(
        merchant_id=inp.merchant_id,
        total_disputes=n,
        total_amount_at_risk=total_amount,
        sp_covered_amount=sp_covered,
        unprotected_amount=unprotected,
        total_ev_if_all_fought=ev_fought,
        total_ev_if_all_accepted=ev_accepted,
        expected_net_recovery=expected_net,
        portfolio_avg_win_prob=avg_wp,
        hdr_dispute_count=hdr_count,
        hdr_impact_rate=hdr_rate,
        by_reason_code=by_rc,
        by_network=by_net,
        by_urgency=by_urg,
        top_disputes=top,
        portfolio_risk_level=risk_level,
        risk_flags=flags,
        summary=summary,
    )
