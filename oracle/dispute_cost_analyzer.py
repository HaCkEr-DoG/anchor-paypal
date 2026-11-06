"""
Dispute Cost Analyzer — compute total cost of a dispute scenario.

A dispute is not just the disputed amount. Total cost includes:
  1. Direct loss: dispute_amount if lost
  2. Fight cost: staff time, evidence collection, response drafting
  3. HDR cost: surcharge fees if dispute pushes merchant into HDR zone
  4. Opportunity cost: revenue blocked while dispute is open
  5. Chargeback fee: $20 per chargeback (waived if SP protects)
  6. Shipping cost: cost of the original shipment (sunk cost but relevant)
  7. COGS: cost of goods sold (net loss if refunded vs. fought)

Cost models:
  FOUGHT_AND_WON:    fight_cost only
  FOUGHT_AND_LOST:   dispute_amount + fight_cost + chargeback_fee
  AUTO_REFUNDED:     refund_amount + processing_time_cost
  PARTIAL_REFUND:    partial_amount + processing_time_cost
  SETTLED:           settlement_amount + negotiation_cost

Break-even analysis:
  break_even_p_win = (fight_cost + chargeback_fee) / (dispute_amount + fight_cost + chargeback_fee - 0)
  = fight_cost / dispute_amount  (simplified)

ROI of fighting:
  ROI = EV(fight) / fight_cost
  = (p_win × amount - fight_cost) / fight_cost

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeOutcomeScenario(str, Enum):
    FIGHT_AND_WIN   = "FIGHT_AND_WIN"
    FIGHT_AND_LOSE  = "FIGHT_AND_LOSE"
    AUTO_REFUND     = "AUTO_REFUND"
    PARTIAL_REFUND  = "PARTIAL_REFUND"
    SETTLE          = "SETTLE"


@dataclass
class CostInput:
    """Input for dispute cost analysis."""
    dispute_id: str
    claim_type: str
    dispute_amount: float

    # Fight costs
    fight_cost_hours: float = 2.0        # Hours of staff time to respond
    hourly_rate: float = 50.0            # Staff cost per hour
    evidence_collection_cost: float = 0.0  # e.g., carrier affidavit fee

    # Fees
    chargeback_fee: float = 20.0         # PayPal chargeback fee
    hdr_surcharge_per_dispute: float = 0.0  # $30 if in HDR surcharge zone

    # SP coverage
    sp_covered: bool = False             # If SP covered, chargeback_fee = 0

    # Refund scenarios
    partial_refund_pct: float = 0.5      # 0-1.0 for partial refund scenario
    settlement_pct: float = 0.60         # Settlement as % of dispute amount

    # Win probability
    p_win: float = 0.65

    # COGS and revenue
    cogs_pct: float = 0.40               # Cost of goods as % of order value
    shipping_cost: float = 0.0           # Original shipping cost (already spent)

    # HDR context
    is_hdr_contributing: bool = True


@dataclass
class ScenarioCost:
    """Cost breakdown for one dispute outcome scenario."""
    scenario: DisputeOutcomeScenario
    total_cost: float
    dispute_loss: float          # Amount paid to buyer
    fight_cost: float            # Internal processing cost
    fees: float                  # PayPal fees
    cogs_recovered: float        # COGS avoided if goods returned
    net_loss: float              # total_cost − cogs_recovered
    description: str


@dataclass
class DisputeCostReport:
    """
    Complete cost analysis across all dispute outcome scenarios.
    """
    dispute_id: str
    claim_type: str
    dispute_amount: float

    # Scenarios
    scenarios: list[ScenarioCost]

    # Key metrics
    expected_cost: float          # EV-weighted expected cost
    break_even_p_win: float       # p_win at which fighting = refunding
    fight_roi: float              # ROI of fighting (EV / fight_cost)

    # Recommendation
    cheapest_scenario: DisputeOutcomeScenario
    recommended_scenario: DisputeOutcomeScenario  # Risk-adjusted

    # Guidance
    cost_summary: str
    summary: str


def _fight_cost(inp: CostInput) -> float:
    return round(inp.fight_cost_hours * inp.hourly_rate + inp.evidence_collection_cost, 2)


def _chargeback_fee(inp: CostInput) -> float:
    return 0.0 if inp.sp_covered else inp.chargeback_fee


def _hdr_add(inp: CostInput) -> float:
    return inp.hdr_surcharge_per_dispute if inp.is_hdr_contributing else 0.0


def _cogs(inp: CostInput) -> float:
    return round(inp.dispute_amount * inp.cogs_pct, 2)


def analyze_dispute_cost(inp: CostInput) -> DisputeCostReport:
    """
    Compute full cost analysis for a dispute across all outcome scenarios.
    """
    fc = _fight_cost(inp)
    cb_fee = _chargeback_fee(inp)
    hdr = _hdr_add(inp)
    cogs = _cogs(inp)

    # FIGHT_AND_WIN
    fight_win_total = fc + hdr
    fight_win = ScenarioCost(
        scenario=DisputeOutcomeScenario.FIGHT_AND_WIN,
        total_cost=round(fight_win_total, 2),
        dispute_loss=0.0,
        fight_cost=fc,
        fees=hdr,
        cogs_recovered=cogs,  # Goods not returned — keep them, avoid write-off
        net_loss=round(fight_win_total, 2),
        description=f"Fight and win: staff=${fc:.0f} + HDR_surcharge=${hdr:.0f}",
    )

    # FIGHT_AND_LOSE
    fight_lose_total = inp.dispute_amount + fc + cb_fee + hdr
    fight_lose = ScenarioCost(
        scenario=DisputeOutcomeScenario.FIGHT_AND_LOSE,
        total_cost=round(fight_lose_total, 2),
        dispute_loss=inp.dispute_amount,
        fight_cost=fc,
        fees=cb_fee + hdr,
        cogs_recovered=0.0,  # Goods gone, no recovery
        net_loss=round(fight_lose_total, 2),
        description=f"Fight and lose: amount=${inp.dispute_amount:.0f} + staff=${fc:.0f} + fees=${cb_fee + hdr:.0f}",
    )

    # AUTO_REFUND — refund immediately, no fight cost
    processing_cost = round(inp.fight_cost_hours * 0.5 * inp.hourly_rate, 2)  # Half time to process refund
    auto_refund_total = inp.dispute_amount + processing_cost + hdr
    auto_refund = ScenarioCost(
        scenario=DisputeOutcomeScenario.AUTO_REFUND,
        total_cost=round(auto_refund_total, 2),
        dispute_loss=inp.dispute_amount,
        fight_cost=processing_cost,
        fees=hdr,
        cogs_recovered=0.0,
        net_loss=round(auto_refund_total, 2),
        description=f"Auto-refund: amount=${inp.dispute_amount:.0f} + processing=${processing_cost:.0f}",
    )

    # PARTIAL_REFUND
    partial_amount = round(inp.dispute_amount * inp.partial_refund_pct, 2)
    partial_total = partial_amount + processing_cost + hdr
    partial_cogs = round(cogs * (1.0 - inp.partial_refund_pct), 2)
    partial_refund = ScenarioCost(
        scenario=DisputeOutcomeScenario.PARTIAL_REFUND,
        total_cost=round(partial_total, 2),
        dispute_loss=partial_amount,
        fight_cost=processing_cost,
        fees=hdr,
        cogs_recovered=partial_cogs,
        net_loss=round(partial_total - partial_cogs, 2),
        description=f"Partial refund ({inp.partial_refund_pct*100:.0f}%): ${partial_amount:.0f} + processing=${processing_cost:.0f}",
    )

    # SETTLE
    settle_amount = round(inp.dispute_amount * inp.settlement_pct, 2)
    settle_negotiation = round(fc * 0.75, 2)
    settle_total = settle_amount + settle_negotiation + hdr
    settle = ScenarioCost(
        scenario=DisputeOutcomeScenario.SETTLE,
        total_cost=round(settle_total, 2),
        dispute_loss=settle_amount,
        fight_cost=settle_negotiation,
        fees=hdr,
        cogs_recovered=0.0,
        net_loss=round(settle_total, 2),
        description=f"Settle at {inp.settlement_pct*100:.0f}%: ${settle_amount:.0f} + negotiation=${settle_negotiation:.0f}",
    )

    scenarios = [fight_win, fight_lose, auto_refund, partial_refund, settle]

    # EV-weighted cost
    ev_cost = round(
        inp.p_win * fight_win.total_cost + (1 - inp.p_win) * fight_lose.total_cost,
        2
    )

    # Break-even: fight_cost = p_win × amount → p_win = fight_cost / amount
    break_even = round(fc / inp.dispute_amount, 4) if inp.dispute_amount > 0 else 1.0

    # Fight ROI: (p_win × amount − fc) / fc
    ev_fight = inp.p_win * inp.dispute_amount - fc
    fight_roi = round(ev_fight / fc, 4) if fc > 0 else 0.0

    # Cheapest scenario
    cheapest = min(scenarios, key=lambda s: s.net_loss)

    # Recommended: cheapest unless it's risky; prefer fight if ROI > 1.0
    if fight_roi >= 1.0 and inp.p_win >= 0.65:
        recommended = DisputeOutcomeScenario.FIGHT_AND_WIN
    elif cheapest.scenario in (DisputeOutcomeScenario.FIGHT_AND_WIN, DisputeOutcomeScenario.PARTIAL_REFUND):
        recommended = cheapest.scenario
    else:
        recommended = DisputeOutcomeScenario.PARTIAL_REFUND

    cost_summary = (
        f"Fight expected cost: ${ev_cost:.0f} | "
        f"Auto-refund cost: ${auto_refund.total_cost:.0f} | "
        f"Break-even p_win: {break_even:.0%} | "
        f"Fight ROI: {fight_roi:.1f}x"
    )

    summary = (
        f"Costs {inp.dispute_id}: {inp.claim_type} ${inp.dispute_amount:.0f} | "
        f"ev_cost=${ev_cost:.0f} | break_even={break_even:.0%} | roi={fight_roi:.1f}x | "
        f"recommended={recommended.value}"
    )

    return DisputeCostReport(
        dispute_id=inp.dispute_id,
        claim_type=inp.claim_type,
        dispute_amount=inp.dispute_amount,
        scenarios=scenarios,
        expected_cost=ev_cost,
        break_even_p_win=break_even,
        fight_roi=fight_roi,
        cheapest_scenario=cheapest.scenario,
        recommended_scenario=recommended,
        cost_summary=cost_summary,
        summary=summary,
    )
