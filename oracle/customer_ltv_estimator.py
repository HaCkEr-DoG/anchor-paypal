"""
Customer Lifetime Value Estimator — estimate whether a buyer is worth
accommodating proactively to avoid a dispute.

Before a dispute becomes formal, the merchant may choose to offer a refund
or concession proactively. This module helps decide if that's worthwhile
by estimating:

  1. Estimated customer LTV (remaining lifetime value)
  2. Acquisition cost already spent on this customer
  3. Net retention value (LTV - cost of concession)
  4. Whether proactive resolution is economically justified

LTV model:
  LTV = avg_order_value × purchase_frequency_per_year × years_remaining
  Discounted at annual_discount_rate

Risk adjustments (reduce LTV):
  - Buyer with dispute history (prior disputes reduce expected purchases)
  - Potential fraud signals
  - Single-purchase buyers (no repeat signal)

Retention factors:
  - Resolving a dispute proactively retains ~70% of buyers vs ~40% if lost
  - A buyer who loses a dispute rarely orders again from the same merchant

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RetentionOutcome(str, Enum):
    PROACTIVE_REFUND_JUSTIFIED  = "PROACTIVE_REFUND_JUSTIFIED"
    PARTIAL_CONCESSION_OPTIMAL  = "PARTIAL_CONCESSION_OPTIMAL"
    FIGHT_DISPUTE               = "FIGHT_DISPUTE"
    LOW_VALUE_IGNORE            = "LOW_VALUE_IGNORE"


@dataclass
class CustomerLTVInput:
    """Input for LTV estimation."""
    dispute_id: str
    buyer_id: str

    # Buyer history
    total_orders: int
    total_spend: float               # Total historical spend with this merchant
    months_as_customer: int
    dispute_count_lifetime: int      # Total disputes ever filed

    # Dispute context
    dispute_amount: float
    p_win_dispute: float             # Merchant's win probability if fighting
    fight_cost: float = 25.0

    # LTV parameters
    avg_order_value: float = 0.0     # If 0, computed from history
    annual_discount_rate: float = 0.10
    estimated_years_remaining: float = 3.0  # Expected remaining customer tenure

    # Retention assumptions
    retention_rate_proactive: float = 0.70  # P(buyer stays) if resolved proactively
    retention_rate_fight_win: float = 0.55  # P(buyer stays) if merchant wins dispute
    retention_rate_fight_lose: float = 0.20  # P(buyer stays) if merchant loses


@dataclass
class CustomerLTVReport:
    """LTV analysis and retention recommendation."""
    dispute_id: str
    buyer_id: str

    # LTV estimates
    raw_ltv: float                   # Undiscounted future value
    discounted_ltv: float            # NPV of future purchases
    avg_annual_value: float

    # Retention analysis
    ev_retain_proactive: float       # EV if proactive concession made
    ev_retain_fight: float           # EV-weighted expected retention if fighting
    net_benefit_proactive: float     # ev_retain_proactive − dispute_amount

    # Recommendation
    recommendation: RetentionOutcome
    concession_cap: float            # Max concession worth making

    # Buyer profile
    is_loyal_buyer: bool
    dispute_rate: float              # disputes / orders
    purchase_frequency_per_year: float

    # Guidance
    rationale: str
    summary: str


def _avg_order_value(inp: CustomerLTVInput) -> float:
    if inp.avg_order_value > 0:
        return inp.avg_order_value
    if inp.total_orders > 0:
        return round(inp.total_spend / inp.total_orders, 2)
    return inp.dispute_amount   # Fallback: treat dispute as typical order


def _purchase_frequency(inp: CustomerLTVInput) -> float:
    """Orders per year."""
    if inp.months_as_customer <= 0 or inp.total_orders == 0:
        return 1.0
    return round(inp.total_orders / (inp.months_as_customer / 12), 2)


def _dispute_rate(inp: CustomerLTVInput) -> float:
    if inp.total_orders == 0:
        return 0.0
    return round(inp.dispute_count_lifetime / inp.total_orders, 4)


def _raw_ltv(aov: float, freq: float, years: float) -> float:
    """Simple undiscounted LTV."""
    return round(aov * freq * years, 2)


def _discounted_ltv(aov: float, freq: float, years: float, rate: float) -> float:
    """
    NPV of annual cashflows: sum of (aov * freq) / (1+rate)^t for t=1..years
    """
    annual_cf = aov * freq
    if rate == 0:
        return round(annual_cf * years, 2)
    # Sum of geometric series
    n = years
    r = rate
    pv = annual_cf * (1 - (1 + r) ** (-n)) / r
    return round(pv, 2)


def _risk_adjust_ltv(ltv: float, dr: float, is_first: bool) -> float:
    """Reduce LTV for buyer risk signals."""
    factor = 1.0
    if dr >= 0.20:   # 20%+ dispute rate is very high
        factor *= 0.40
    elif dr >= 0.10:
        factor *= 0.65
    elif dr >= 0.05:
        factor *= 0.80
    if is_first:
        factor *= 0.70   # First-time buyer LTV is less certain
    return round(ltv * factor, 2)


def estimate_customer_ltv(inp: CustomerLTVInput) -> CustomerLTVReport:
    """
    Estimate buyer LTV and determine whether proactive concession is justified.
    """
    aov        = _avg_order_value(inp)
    freq       = _purchase_frequency(inp)
    dr         = _dispute_rate(inp)
    is_loyal   = inp.total_orders >= 3 and dr < 0.10
    is_first   = inp.total_orders <= 1

    raw        = _raw_ltv(aov, freq, inp.estimated_years_remaining)
    discounted = _discounted_ltv(aov, freq, inp.estimated_years_remaining, inp.annual_discount_rate)
    adj_ltv    = _risk_adjust_ltv(discounted, dr, is_first)

    annual_value = round(aov * freq, 2)

    # Retention EV analysis
    # If proactive refund: cost = dispute_amount; buyer likely stays (retention_rate_proactive)
    ev_proactive_retain = adj_ltv * inp.retention_rate_proactive
    ev_retain_proactive = round(ev_proactive_retain - inp.dispute_amount, 2)

    # If fight: EV-weighted outcome
    # Win: retain at fight_win_rate, cost = fight_cost
    # Lose: retain at fight_lose_rate, cost = dispute_amount + fight_cost
    ev_win_scenario  = (inp.p_win_dispute *
                        (adj_ltv * inp.retention_rate_fight_win - inp.fight_cost))
    ev_lose_scenario = ((1 - inp.p_win_dispute) *
                        (adj_ltv * inp.retention_rate_fight_lose - inp.dispute_amount - inp.fight_cost))
    ev_retain_fight  = round(ev_win_scenario + ev_lose_scenario, 2)

    # Net benefit of proactive refund vs fighting
    net_benefit_proactive = round(ev_retain_proactive - ev_retain_fight, 2)

    # Concession cap: maximum worth giving up
    concession_cap = round(adj_ltv * (inp.retention_rate_proactive - inp.retention_rate_fight_lose), 2)

    # Recommendation
    if dr >= 0.20 or (is_first and inp.dispute_amount > 50):
        # High dispute rate or suspicious first-time buyer
        recommendation = RetentionOutcome.FIGHT_DISPUTE
        rationale = "High dispute rate or suspicious first-time buyer — fight the dispute"
    elif adj_ltv < inp.dispute_amount * 0.5:
        # LTV too low to justify concession
        recommendation = RetentionOutcome.LOW_VALUE_IGNORE
        rationale = f"Adjusted LTV ${adj_ltv:.0f} < 50% of dispute — not worth concession"
    elif net_benefit_proactive > 0 and is_loyal:
        recommendation = RetentionOutcome.PROACTIVE_REFUND_JUSTIFIED
        rationale = f"Loyal buyer LTV ${adj_ltv:.0f} justifies proactive refund (net benefit ${net_benefit_proactive:.0f})"
    elif net_benefit_proactive > 0:
        recommendation = RetentionOutcome.PARTIAL_CONCESSION_OPTIMAL
        rationale = f"Partial concession up to ${concession_cap:.0f} maximizes EV"
    else:
        recommendation = RetentionOutcome.FIGHT_DISPUTE
        rationale = f"Fighting yields better EV (fight_ev=${ev_retain_fight:.0f} vs proactive_ev=${ev_retain_proactive:.0f})"

    summary = (
        f"LTV {inp.dispute_id} [{inp.buyer_id}]: "
        f"adj_ltv=${adj_ltv:.0f} | dr={dr:.0%} | loyal={is_loyal} | "
        f"ev_proactive=${ev_retain_proactive:.0f} | ev_fight=${ev_retain_fight:.0f} | "
        f"rec={recommendation.value}"
    )

    return CustomerLTVReport(
        dispute_id=inp.dispute_id,
        buyer_id=inp.buyer_id,
        raw_ltv=raw,
        discounted_ltv=adj_ltv,
        avg_annual_value=annual_value,
        ev_retain_proactive=ev_retain_proactive,
        ev_retain_fight=ev_retain_fight,
        net_benefit_proactive=net_benefit_proactive,
        recommendation=recommendation,
        concession_cap=max(0.0, concession_cap),
        is_loyal_buyer=is_loyal,
        dispute_rate=dr,
        purchase_frequency_per_year=freq,
        rationale=rationale,
        summary=summary,
    )
