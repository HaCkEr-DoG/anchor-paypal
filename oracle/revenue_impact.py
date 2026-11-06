"""
Revenue Impact Calculator — full financial impact of disputes on merchant.

Most merchants know they "lost a chargeback." They don't know the full cost:

Full dispute cost = (amount lost if merchant loses)
                 + (cost to fight, win or lose)
                 + (HDR fee if above 1.5% threshold)
                 + (opportunity cost of capital held during dispute)
                 + (operational cost of handling)
                 + (second-order effects: HDR restriction reduces revenue)

This module quantifies every layer:

  1. DIRECT: Amount at risk × (1 − win_rate)
  2. FIGHT_COST: Processing cost per dispute regardless of outcome
  3. HDR_FEE: $30/dispute above the 1.5% fee tier (if applicable)
  4. CAPITAL: Interest cost of float during dispute resolution (avg 45 days)
  5. OPERATIONAL: Staff time per dispute × hourly rate
  6. RECOVERY: Second-order revenue loss from HDR processing restrictions

And the flipside: the VALUE of fighting (expected recovery per fight).

Competitor gap:
  - Chargebacks911 and Midigator report win rates but not full cost impact
  - Merchants think "winning" saves 100% of the dispute amount
  - Reality: net save after fight cost = amount × win_rate − fight_cost
  - For a $50 dispute with 65% win rate and $25 fight cost:
    Net save = $50 × 0.65 − $25 = $7.50 (not $32.50)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ImpactCategory(str, Enum):
    DIRECT_LOSS        = "DIRECT_LOSS"         # Amount lost when merchant loses
    FIGHT_COST         = "FIGHT_COST"          # Cost to process/fight each dispute
    HDR_FEE            = "HDR_FEE"             # $30/dispute above 1.5% threshold
    CAPITAL_COST       = "CAPITAL_COST"        # Interest on held funds
    OPERATIONAL_COST   = "OPERATIONAL_COST"    # Staff time
    REVENUE_RESTRICTION = "REVENUE_RESTRICTION" # Lost sales from HDR restrictions
    TOTAL              = "TOTAL"


@dataclass
class DisputePortfolioInput:
    """
    Input describing a merchant's dispute portfolio for a time period.
    """
    period_days: int = 90

    # Transaction volume
    total_transactions: int = 1000
    total_gmv: float = 100000.0

    # Dispute stats
    total_disputes: int = 20
    disputes_won_by_merchant: int = 13
    disputes_lost_by_merchant: int = 7

    # Average financials
    avg_dispute_amount: float = 150.0
    avg_fight_cost: float = 25.0           # Cost to process/submit evidence per dispute
    hourly_staff_rate: float = 35.0        # Staff hourly rate for dispute work
    minutes_per_dispute: float = 30.0      # Average time per dispute

    # HDR context
    current_hdr_rate: float = 0.020        # Current dispute rate (disputes/transactions)
    is_in_fee_tier: bool = False           # True if HDR > 1.5%

    # Capital
    annual_cost_of_capital: float = 0.08   # 8% (bank rate / opportunity cost)
    avg_days_to_resolve: int = 45

    # Revenue impact of HDR restrictions
    monthly_revenue: float = 50000.0
    hdr_revenue_restriction_pct: float = 0.0  # % of revenue blocked by payment restrictions


@dataclass
class ImpactLineItem:
    category: ImpactCategory
    amount: float
    per_dispute: float
    description: str


@dataclass
class RevenueImpactReport:
    """
    Full financial impact of disputes on merchant revenue.
    """
    period_days: int

    # Core metrics
    dispute_rate: float
    merchant_win_rate: float
    total_disputes: int
    total_at_risk: float          # Total GMV across all disputes

    # Broken down cost
    direct_loss: float            # Amount actually lost
    fight_cost_total: float       # Total processing cost
    hdr_fee_total: float          # PayPal HDR fees
    capital_cost_total: float     # Interest on held funds
    operational_cost_total: float # Staff time cost
    revenue_restriction_loss: float  # Lost revenue from restrictions

    # Totals
    total_cost: float
    cost_per_dispute: float       # Average all-in cost per dispute
    cost_as_pct_of_gmv: float    # Total cost / GMV

    # Fighting value
    gross_savings_from_fighting: float    # Amount recovered by winning
    net_savings_from_fighting: float      # Gross minus fight costs
    roi_of_fighting: float                # Net savings / fight cost

    # Line items
    line_items: list[ImpactLineItem] = field(default_factory=list)

    # Benchmarks
    industry_avg_cost_per_dispute: float = 90.0  # PayPal + chargeback911 estimates
    vs_industry_benchmark: float = 0.0           # Our cost vs benchmark

    summary: str = ""
    recommendations: list[str] = field(default_factory=list)


def calculate_revenue_impact(inp: DisputePortfolioInput) -> RevenueImpactReport:
    """
    Calculate the full financial impact of disputes for a merchant portfolio.

    Returns a complete cost breakdown with ROI analysis on fighting disputes.
    """
    # Core rates
    dispute_rate = inp.total_disputes / max(inp.total_transactions, 1)
    total_resolved = inp.disputes_won_by_merchant + inp.disputes_lost_by_merchant
    merchant_win_rate = inp.disputes_won_by_merchant / max(total_resolved, 1)
    total_at_risk = inp.total_disputes * inp.avg_dispute_amount

    # 1. Direct loss: amount lost when merchant loses
    direct_loss = inp.disputes_lost_by_merchant * inp.avg_dispute_amount

    # 2. Fight cost: cost to process every dispute
    fight_cost_total = inp.total_disputes * inp.avg_fight_cost

    # 3. HDR fee: $30 per dispute above 1.5% threshold
    if inp.is_in_fee_tier and inp.current_hdr_rate > 0.015:
        # Fee applies to disputes above the 1.5% threshold
        fee_tier_disputes = max(0, int(
            inp.total_transactions * (inp.current_hdr_rate - 0.015)
        ))
        hdr_fee_total = fee_tier_disputes * 30.0
    else:
        hdr_fee_total = 0.0

    # 4. Capital cost: interest on disputed amount during resolution
    # Amount held × rate × (days / 365)
    capital_per_dispute = (
        inp.avg_dispute_amount
        * inp.annual_cost_of_capital
        * (inp.avg_days_to_resolve / 365.0)
    )
    capital_cost_total = capital_per_dispute * inp.total_disputes

    # 5. Operational cost: staff time
    hours_per_dispute = inp.minutes_per_dispute / 60.0
    operational_per_dispute = hours_per_dispute * inp.hourly_staff_rate
    operational_cost_total = operational_per_dispute * inp.total_disputes

    # 6. Revenue restriction loss
    if inp.hdr_revenue_restriction_pct > 0:
        monthly_loss = inp.monthly_revenue * inp.hdr_revenue_restriction_pct
        revenue_restriction_loss = monthly_loss * (inp.period_days / 30.0)
    else:
        revenue_restriction_loss = 0.0

    # Total
    total_cost = (
        direct_loss
        + fight_cost_total
        + hdr_fee_total
        + capital_cost_total
        + operational_cost_total
        + revenue_restriction_loss
    )
    cost_per_dispute = total_cost / max(inp.total_disputes, 1)
    cost_as_pct_of_gmv = total_cost / max(inp.total_gmv, 1)

    # ROI of fighting
    gross_savings = inp.disputes_won_by_merchant * inp.avg_dispute_amount
    net_savings = gross_savings - fight_cost_total
    roi = net_savings / max(fight_cost_total, 1.0)

    # Build line items
    line_items = [
        ImpactLineItem(
            ImpactCategory.DIRECT_LOSS, direct_loss,
            direct_loss / max(inp.total_disputes, 1),
            f"{inp.disputes_lost_by_merchant} disputes lost × ${inp.avg_dispute_amount:.0f}",
        ),
        ImpactLineItem(
            ImpactCategory.FIGHT_COST, fight_cost_total,
            inp.avg_fight_cost,
            f"{inp.total_disputes} disputes × ${inp.avg_fight_cost:.0f} processing cost",
        ),
        ImpactLineItem(
            ImpactCategory.HDR_FEE, hdr_fee_total,
            hdr_fee_total / max(inp.total_disputes, 1),
            f"PayPal $30/dispute HDR fee above 1.5% threshold",
        ),
        ImpactLineItem(
            ImpactCategory.CAPITAL_COST, capital_cost_total,
            capital_per_dispute,
            f"${inp.avg_dispute_amount:.0f} held {inp.avg_days_to_resolve}d @ {inp.annual_cost_of_capital:.0%} cost of capital",
        ),
        ImpactLineItem(
            ImpactCategory.OPERATIONAL_COST, operational_cost_total,
            operational_per_dispute,
            f"{inp.minutes_per_dispute:.0f} min/dispute × ${inp.hourly_staff_rate:.0f}/hr staff",
        ),
        ImpactLineItem(
            ImpactCategory.REVENUE_RESTRICTION, revenue_restriction_loss,
            revenue_restriction_loss / max(inp.period_days / 30.0, 1),
            f"{inp.hdr_revenue_restriction_pct:.0%} revenue blocked by HDR restrictions",
        ),
        ImpactLineItem(
            ImpactCategory.TOTAL, total_cost,
            cost_per_dispute,
            f"Total all-in dispute cost = {cost_as_pct_of_gmv:.2%} of GMV",
        ),
    ]

    # Recommendations
    recommendations: list[str] = []
    if merchant_win_rate < 0.60:
        recommendations.append(
            "Win rate below 60%: invest in evidence quality and carrier upgrades — "
            "each 10% improvement in win rate saves "
            f"${inp.total_disputes * inp.avg_dispute_amount * 0.10:.0f}/period"
        )
    if hdr_fee_total > 0:
        recommendations.append(
            f"HDR fee tier active: ${hdr_fee_total:.0f} in fees this period. "
            f"Dropping below 1.5% saves the full fee next period."
        )
    if operational_cost_total > fight_cost_total:
        recommendations.append(
            "Staff time exceeds processing cost: invest in dispute automation — "
            "batch dispatcher can reduce human review time by 60-80%."
        )
    if net_savings > 0:
        recommendations.append(
            f"Fighting generates ROI: ${net_savings:.0f} net savings "
            f"(${gross_savings:.0f} recovered − ${fight_cost_total:.0f} cost). "
            f"ROI = {roi:.1f}x. Continue fighting all viable disputes."
        )
    else:
        recommendations.append(
            f"Fighting generates negative ROI at current win rate ({merchant_win_rate:.0%}). "
            f"Raise win rate above {fight_cost_total / max(total_at_risk, 1):.0%} for positive ROI."
        )

    # Benchmark
    vs_benchmark = cost_per_dispute - 90.0

    summary = (
        f"{inp.total_disputes} disputes in {inp.period_days}d "
        f"({dispute_rate:.2%} rate) | "
        f"win rate: {merchant_win_rate:.0%} | "
        f"total cost: ${total_cost:.0f} ({cost_as_pct_of_gmv:.2%} of GMV) | "
        f"cost/dispute: ${cost_per_dispute:.0f} vs ${90:.0f} industry avg | "
        f"net fight ROI: {roi:.1f}x"
    )

    return RevenueImpactReport(
        period_days=inp.period_days,
        dispute_rate=round(dispute_rate, 4),
        merchant_win_rate=round(merchant_win_rate, 3),
        total_disputes=inp.total_disputes,
        total_at_risk=round(total_at_risk, 2),
        direct_loss=round(direct_loss, 2),
        fight_cost_total=round(fight_cost_total, 2),
        hdr_fee_total=round(hdr_fee_total, 2),
        capital_cost_total=round(capital_cost_total, 2),
        operational_cost_total=round(operational_cost_total, 2),
        revenue_restriction_loss=round(revenue_restriction_loss, 2),
        total_cost=round(total_cost, 2),
        cost_per_dispute=round(cost_per_dispute, 2),
        cost_as_pct_of_gmv=round(cost_as_pct_of_gmv, 4),
        gross_savings_from_fighting=round(gross_savings, 2),
        net_savings_from_fighting=round(net_savings, 2),
        roi_of_fighting=round(roi, 2),
        line_items=line_items,
        vs_industry_benchmark=round(vs_benchmark, 2),
        summary=summary,
        recommendations=recommendations,
    )
