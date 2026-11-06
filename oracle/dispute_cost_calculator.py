"""
Dispute Cost Calculator — compute the true total cost of a dispute
across all cost categories: chargeback fee, HDR penalty, operational
processing cost, fight cost, and opportunity cost.

Cost categories:
  1. Base loss = disputed_amount (if we lose)
  2. Chargeback fee = $20 PayPal, $15–25 card network
  3. HDR fee = $30/dispute once merchant crosses 1.5% threshold
  4. Operational cost = staff time for case handling ($/hour × hours)
  5. Fight cost = legal/evidence gathering cost
  6. Opportunity cost = customer lifetime value × churn probability

The calculator produces:
  - cost_if_fight: total if we fight (and may win or lose)
  - cost_if_accept: total if we immediately refund
  - cost_difference: how much fighting costs MORE than accepting
  - break_even_win_rate: p_win needed for fighting to be EV-neutral

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeNetwork(str, Enum):
    PAYPAL      = "PAYPAL"
    VISA        = "VISA"
    MASTERCARD  = "MASTERCARD"
    AMEX        = "AMEX"
    DISCOVER    = "DISCOVER"


class MerchantHDRTier(str, Enum):
    NORMAL   = "NORMAL"    # < 0.5%  — no penalty
    ELEVATED = "ELEVATED"  # 0.5–1.0% — monitoring, no fee
    LIMITED  = "LIMITED"   # 1.0–1.5% — some restrictions
    FEE_TIER = "FEE_TIER"  # 1.5–3.0% — $30 fee per dispute
    SEVERE   = "SEVERE"    # ≥ 3.0%   — may be terminated


# Default chargeback fees by network
_CHARGEBACK_FEES: dict[str, float] = {
    "PAYPAL":     20.0,
    "VISA":       25.0,
    "MASTERCARD": 25.0,
    "AMEX":       20.0,
    "DISCOVER":   20.0,
}

# Minutes of staff time for typical dispute handling
_HANDLING_MINUTES = {
    "ACCEPT": 10,    # Quick refund, minimal work
    "FIGHT":  45,    # Evidence gathering, submission
    "ESCALATE": 90,  # Appeals, legal review
}

_HDR_FEE_PER_DISPUTE = 30.0
_DEFAULT_HOURLY_COST = 35.0   # Typical ops staff cost per hour


@dataclass
class DisputeCostInput:
    """Input for dispute cost calculation."""
    dispute_id: str
    network: DisputeNetwork
    disputed_amount: float

    # Win probability (0–1)
    p_win: float

    # HDR status
    hdr_tier: MerchantHDRTier = MerchantHDRTier.NORMAL

    # Fight costs
    fight_cost: float = 0.0        # Out-of-pocket legal / evidence cost
    hourly_staff_cost: float = _DEFAULT_HOURLY_COST

    # Opportunity cost inputs
    customer_ltv: float = 0.0
    churn_probability_if_lost: float = 0.3   # Prob buyer churns if dispute goes badly

    # Override chargeback fee
    custom_chargeback_fee: Optional[float] = None


@dataclass
class DisputeCostBreakdown:
    """Detailed cost breakdown for one scenario."""
    disputed_amount: float
    chargeback_fee: float
    hdr_fee: float
    operational_cost: float
    fight_cost: float
    opportunity_cost: float
    total: float


@dataclass
class DisputeCostReport:
    """Full dispute cost analysis."""
    dispute_id: str

    cost_if_accept: DisputeCostBreakdown
    cost_if_fight: DisputeCostBreakdown    # EV-weighted expected cost

    # EV analysis
    ev_fight: float              # EV(fight) — negative means expected loss
    ev_accept: float             # EV(accept) — always = -disputed_amount (before opportunity)
    ev_difference: float         # ev_fight - ev_accept (positive = fight is better EV)

    break_even_win_rate: float   # p_win needed for fight EV ≥ accept EV
    recommendation: str          # FIGHT / ACCEPT / BORDERLINE

    chargeback_fee: float
    hdr_fee: float
    summary: str


def _chargeback_fee(inp: DisputeCostInput) -> float:
    if inp.custom_chargeback_fee is not None:
        return inp.custom_chargeback_fee
    return _CHARGEBACK_FEES.get(inp.network.value, 20.0)


def _hdr_fee(inp: DisputeCostInput) -> float:
    if inp.hdr_tier == MerchantHDRTier.FEE_TIER:
        return _HDR_FEE_PER_DISPUTE
    if inp.hdr_tier == MerchantHDRTier.SEVERE:
        return _HDR_FEE_PER_DISPUTE  # Still charged until terminated
    return 0.0


def _operational_cost(inp: DisputeCostInput, scenario: str) -> float:
    minutes = _HANDLING_MINUTES.get(scenario, 30)
    return (minutes / 60.0) * inp.hourly_staff_cost


def _opportunity_cost(inp: DisputeCostInput) -> float:
    return inp.customer_ltv * inp.churn_probability_if_lost


def calculate_dispute_cost(inp: DisputeCostInput) -> DisputeCostReport:
    """Compute full cost analysis for fight vs. accept scenarios."""
    cb_fee = _chargeback_fee(inp)
    hdr_fee = _hdr_fee(inp)
    opp_cost = _opportunity_cost(inp)

    # Accept scenario: refund immediately — no chargeback fee, no HDR fee,
    # minimal operational cost, but still risk some churn
    op_accept = _operational_cost(inp, "ACCEPT")
    accept_breakdown = DisputeCostBreakdown(
        disputed_amount=inp.disputed_amount,
        chargeback_fee=0.0,          # No fee if we refund before chargeback
        hdr_fee=0.0,                  # No HDR hit if we refund
        operational_cost=op_accept,
        fight_cost=0.0,
        opportunity_cost=opp_cost * 0.5,  # Churn less likely with proactive refund
        total=inp.disputed_amount + op_accept + opp_cost * 0.5,
    )

    # Fight scenario: EV-weighted cost
    # If we win: pay fight_cost + operational
    # If we lose: pay disputed_amount + cb_fee + hdr_fee + fight_cost + operational + opp
    op_fight = _operational_cost(inp, "FIGHT")

    cost_win  = inp.fight_cost + op_fight
    cost_lose = (inp.disputed_amount + cb_fee + hdr_fee
                 + inp.fight_cost + op_fight + opp_cost)

    ev_fight_cost = inp.p_win * cost_win + (1 - inp.p_win) * cost_lose

    fight_breakdown = DisputeCostBreakdown(
        disputed_amount=inp.disputed_amount * (1 - inp.p_win),  # EV-weighted
        chargeback_fee=cb_fee * (1 - inp.p_win),
        hdr_fee=hdr_fee * (1 - inp.p_win),
        operational_cost=op_fight,
        fight_cost=inp.fight_cost,
        opportunity_cost=opp_cost * (1 - inp.p_win),
        total=ev_fight_cost,
    )

    # EV in profit terms (negative = expected loss)
    ev_fight  = -ev_fight_cost
    ev_accept = -(inp.disputed_amount + op_accept + opp_cost * 0.5)
    ev_diff   = ev_fight - ev_accept   # positive = fighting has better EV

    # Break-even: p_win where EV(fight) == EV(accept)
    # p * cost_win + (1-p) * cost_lose = accept_total
    # Solve for p:
    #   p * cost_win + cost_lose - p * cost_lose = accept_total
    #   p * (cost_win - cost_lose) = accept_total - cost_lose
    #   p = (accept_total - cost_lose) / (cost_win - cost_lose)
    accept_total = accept_breakdown.total
    denom = cost_win - cost_lose
    if abs(denom) < 0.01:
        break_even = 0.5
    else:
        be = (accept_total - cost_lose) / denom
        break_even = max(0.0, min(1.0, be))

    if ev_diff > 5.0:
        rec = "FIGHT"
    elif ev_diff < -5.0:
        rec = "ACCEPT"
    else:
        rec = "BORDERLINE"

    summary = (
        f"Cost {inp.dispute_id}: "
        f"accept=${accept_breakdown.total:.2f} | "
        f"fight_ev=${ev_fight_cost:.2f} | "
        f"break_even={break_even:.0%} | rec={rec}"
    )

    return DisputeCostReport(
        dispute_id=inp.dispute_id,
        cost_if_accept=accept_breakdown,
        cost_if_fight=fight_breakdown,
        ev_fight=ev_fight,
        ev_accept=ev_accept,
        ev_difference=ev_diff,
        break_even_win_rate=round(break_even, 4),
        recommendation=rec,
        chargeback_fee=cb_fee,
        hdr_fee=hdr_fee,
        summary=summary,
    )
