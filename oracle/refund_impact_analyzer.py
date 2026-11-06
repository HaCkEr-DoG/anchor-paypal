"""
Refund Impact Analyzer — compute the full financial impact of issuing a
refund vs. fighting a dispute, accounting for COGS, margins, fees, LTV,
and HDR rate effects.

This module answers: "What does this refund ACTUALLY cost us?"
  - Direct cost: refund amount + fees
  - Opportunity cost: lost margin on the transaction
  - HDR impact: how this dispute affects 90-day rate
  - LTV consideration: cost of losing a good customer
  - Net recommendation: refund, fight, or partial settle

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RefundRecommendation(str, Enum):
    FULL_REFUND    = "FULL_REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    FIGHT          = "FIGHT"
    CLOSE_NO_REFUND = "CLOSE_NO_REFUND"


@dataclass
class RefundImpactInput:
    """Input for refund impact analysis."""
    dispute_id: str
    transaction_amount: float

    # Cost structure
    cogs: float = 0.0                   # Cost of goods sold (0 if not known)
    gross_margin_pct: float = 0.40      # Gross margin (default 40%)

    # Fee structure
    paypal_processing_fee: float = 0.0  # Usually non-refundable
    chargeback_fee: float = 20.0        # Fee if dispute escalates to chargeback
    hdr_fee_per_dispute: float = 0.0    # $30 if merchant is in PENALIZED tier

    # Win probability
    p_win: float = 0.50
    fight_cost: float = 10.0            # Internal labor to respond

    # LTV consideration
    customer_ltv: float = 0.0           # Lifetime value of this customer
    is_repeat_customer: bool = False
    is_vip: bool = False

    # Settlement option
    partial_refund_pct: float = 0.60    # Default partial offer = 60% of amount

    # Context
    claim_type: str = "INR"
    is_fraud_risk: bool = False


def _compute_cogs(inp: RefundImpactInput) -> float:
    """Return COGS — from explicit field or derived from margin."""
    if inp.cogs > 0:
        return inp.cogs
    return inp.transaction_amount * (1.0 - inp.gross_margin_pct)


def _cost_of_full_refund(inp: RefundImpactInput) -> float:
    """
    Full cost to issue a refund:
      refund_amount + non_refundable_processing_fee
    """
    return inp.transaction_amount + inp.paypal_processing_fee


def _cost_of_losing_fight(inp: RefundImpactInput) -> float:
    """
    Cost if we fight and LOSE:
      refund_amount + chargeback_fee + hdr_fee + fight_cost + processing_fee
    """
    return (inp.transaction_amount
            + inp.chargeback_fee
            + inp.hdr_fee_per_dispute
            + inp.fight_cost
            + inp.paypal_processing_fee)


def _cost_of_winning_fight(inp: RefundImpactInput) -> float:
    """
    Cost if we fight and WIN:
      fight_cost only (keep the transaction revenue)
    """
    return inp.fight_cost


def _ev_of_fighting(inp: RefundImpactInput) -> float:
    """
    Expected value of fighting:
      EV = p_win × cost_win + (1-p_win) × cost_loss
    But framed as a cost (lower = better).
    """
    cost_win  = _cost_of_winning_fight(inp)
    cost_lose = _cost_of_losing_fight(inp)
    return inp.p_win * cost_win + (1 - inp.p_win) * cost_lose


def _ltv_weight(inp: RefundImpactInput) -> float:
    """Return an LTV factor that reduces the effective cost of refunding."""
    if inp.is_vip or inp.customer_ltv > 500:
        return 0.30 * inp.transaction_amount
    if inp.is_repeat_customer or inp.customer_ltv > 100:
        return 0.15 * inp.transaction_amount
    return 0.0


@dataclass
class CostBreakdown:
    """Detailed cost breakdown for one scenario."""
    scenario: str
    gross_cost: float          # Raw financial outflow
    net_cost: float            # After LTV consideration
    cogs_recovered: bool       # Did we keep the goods?
    description: str


@dataclass
class RefundImpactReport:
    """Full refund impact analysis."""
    dispute_id: str
    transaction_amount: float

    # Key cost figures
    cogs: float
    gross_margin: float                 # Revenue minus COGS

    # Scenario costs
    cost_full_refund: float             # Pay it and move on
    cost_partial_refund: float          # Partial settlement
    cost_fight_win: float               # We fight and win
    cost_fight_lose: float              # We fight and lose
    ev_fight: float                     # EV-weighted fight cost

    # LTV-adjusted
    ltv_adjustment: float
    cost_full_refund_ltv_adj: float
    ev_fight_ltv_adj: float

    # Recommendation
    recommendation: RefundRecommendation
    partial_refund_amount: float
    net_savings_vs_full_refund: float   # How much cheaper is the recommendation?

    # HDR considerations
    hdr_fee_applicable: bool
    hdr_note: Optional[str]

    # Detailed breakdowns
    breakdowns: list[CostBreakdown]

    # Guidance
    rationale: str
    immediate_actions: list[str]
    summary: str


def analyze_refund_impact(inp: RefundImpactInput) -> RefundImpactReport:
    """
    Compute the full financial impact of issuing a refund vs. fighting.
    """
    cogs = _compute_cogs(inp)
    gross_margin = inp.transaction_amount - cogs

    cost_full    = _cost_of_full_refund(inp)
    cost_partial = inp.partial_refund_pct * inp.transaction_amount + inp.paypal_processing_fee
    cost_win     = _cost_of_winning_fight(inp)
    cost_lose    = _cost_of_losing_fight(inp)
    ev_fight     = _ev_of_fighting(inp)

    ltv_adj = _ltv_weight(inp)
    cost_full_adj = max(0, cost_full - ltv_adj)
    ev_fight_adj  = max(0, ev_fight - ltv_adj)

    # Fraud: if fraud risk, don't offer refund (claimant is lying)
    if inp.is_fraud_risk:
        recommendation = RefundRecommendation.FIGHT
        rationale = "Fraud signals detected — fight to avoid incentivizing false claims"
    elif cost_win < cost_full_adj and inp.p_win >= 0.55:
        # High win probability and cheap to fight
        recommendation = RefundRecommendation.FIGHT
        rationale = f"High win probability ({inp.p_win:.0%}) makes fighting cheaper than refund"
    elif cost_partial < cost_full_adj and cost_partial < ev_fight_adj:
        # Partial cheaper than both
        recommendation = RefundRecommendation.PARTIAL_REFUND
        rationale = f"Partial refund (${cost_partial:.2f}) cheaper than fight EV (${ev_fight_adj:.2f})"
    elif inp.transaction_amount < inp.fight_cost + inp.chargeback_fee:
        # Amount too small to bother fighting
        recommendation = RefundRecommendation.FULL_REFUND
        rationale = "Transaction amount smaller than cost to fight — refund immediately"
    else:
        recommendation = RefundRecommendation.FULL_REFUND
        rationale = "Full refund is least costly overall option"

    partial_amount = round(inp.partial_refund_pct * inp.transaction_amount, 2)
    net_savings = max(0, cost_full_adj - {
        RefundRecommendation.FULL_REFUND:    cost_full_adj,
        RefundRecommendation.PARTIAL_REFUND: cost_partial,
        RefundRecommendation.FIGHT:          ev_fight_adj,
        RefundRecommendation.CLOSE_NO_REFUND: 0.0,
    }[recommendation])

    hdr_note = None
    if inp.hdr_fee_per_dispute > 0:
        hdr_note = f"HDR penalty active: extra ${inp.hdr_fee_per_dispute:.0f}/dispute if this escalates"

    breakdowns = [
        CostBreakdown(
            scenario="Full Refund",
            gross_cost=cost_full,
            net_cost=cost_full_adj,
            cogs_recovered=False,
            description=f"Refund ${inp.transaction_amount:.2f} + fees",
        ),
        CostBreakdown(
            scenario=f"Partial Refund ({inp.partial_refund_pct:.0%})",
            gross_cost=cost_partial,
            net_cost=cost_partial,
            cogs_recovered=False,
            description=f"Offer ${partial_amount:.2f} to settle",
        ),
        CostBreakdown(
            scenario="Fight & Win",
            gross_cost=cost_win,
            net_cost=cost_win,
            cogs_recovered=True,
            description=f"Pay only fight cost: ${cost_win:.2f}",
        ),
        CostBreakdown(
            scenario="Fight & Lose",
            gross_cost=cost_lose,
            net_cost=cost_lose,
            cogs_recovered=False,
            description=f"Refund + fees + chargebacks: ${cost_lose:.2f}",
        ),
        CostBreakdown(
            scenario="Fight (Expected Value)",
            gross_cost=ev_fight,
            net_cost=ev_fight_adj,
            cogs_recovered=False,
            description=f"EV at {inp.p_win:.0%} win rate: ${ev_fight:.2f}",
        ),
    ]

    # Immediate actions
    actions: list[str] = []
    if recommendation == RefundRecommendation.FIGHT:
        actions.append(f"Fight: submit evidence within deadline (expected win: {inp.p_win:.0%})")
    elif recommendation == RefundRecommendation.PARTIAL_REFUND:
        actions.append(f"Offer partial settlement of ${partial_amount:.2f} ({inp.partial_refund_pct:.0%})")
    elif recommendation == RefundRecommendation.FULL_REFUND:
        actions.append(f"Issue full refund of ${inp.transaction_amount:.2f} to close dispute")

    if inp.hdr_fee_per_dispute > 0:
        actions.append(f"HDR fee applies: extra ${inp.hdr_fee_per_dispute:.0f} cost if escalated")

    if ltv_adj > 0:
        actions.append(f"LTV consideration: ${ltv_adj:.2f} adjustment for valued customer")

    summary = (
        f"RefundImpact {inp.dispute_id}: "
        f"amount=${inp.transaction_amount:.2f} | "
        f"ev_fight=${ev_fight:.2f} | cost_refund=${cost_full:.2f} | "
        f"rec={recommendation.value}"
    )

    return RefundImpactReport(
        dispute_id=inp.dispute_id,
        transaction_amount=inp.transaction_amount,
        cogs=round(cogs, 2),
        gross_margin=round(gross_margin, 2),
        cost_full_refund=round(cost_full, 2),
        cost_partial_refund=round(cost_partial, 2),
        cost_fight_win=round(cost_win, 2),
        cost_fight_lose=round(cost_lose, 2),
        ev_fight=round(ev_fight, 2),
        ltv_adjustment=round(ltv_adj, 2),
        cost_full_refund_ltv_adj=round(cost_full_adj, 2),
        ev_fight_ltv_adj=round(ev_fight_adj, 2),
        recommendation=recommendation,
        partial_refund_amount=partial_amount,
        net_savings_vs_full_refund=round(net_savings, 2),
        hdr_fee_applicable=inp.hdr_fee_per_dispute > 0,
        hdr_note=hdr_note,
        breakdowns=breakdowns,
        rationale=rationale,
        immediate_actions=actions,
        summary=summary,
    )
