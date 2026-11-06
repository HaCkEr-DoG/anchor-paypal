"""
Settlement Optimizer — determine the optimal settlement offer when
fighting a dispute in full is not advisable.

Settlement logic:
  EV(fight)   = p_win × amount − fight_cost − (1−p_win) × cb_fee
  EV(settle)  = −offer_amount   (we pay offer_amount, done)
  EV(accept)  = −amount          (we lose everything)

We want: offer_amount < EV(losing_fight) but > 0
Optimal offer = amount × (1 − p_win) × settlement_factor
where settlement_factor accounts for LTV and dispute type.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SettlementStrategy(str, Enum):
    FULL_FIGHT       = "FULL_FIGHT"       # EV of fighting > any settlement
    PARTIAL_SETTLE   = "PARTIAL_SETTLE"   # Offer < full amount, avoid chargeback
    FULL_SETTLE      = "FULL_SETTLE"      # Offer = full amount (avoid fee risk)
    ACCEPT_LOSS      = "ACCEPT_LOSS"      # Accept dispute, no fight needed


class LTVTier(str, Enum):
    VIP         = "VIP"
    HIGH        = "HIGH"
    MEDIUM      = "MEDIUM"
    LOW         = "LOW"
    NEGLIGIBLE  = "NEGLIGIBLE"


@dataclass
class SettlementInput:
    """Input for settlement optimization."""
    dispute_id: str
    disputed_amount: float
    p_win: float                    # Calibrated win probability
    fight_cost: float = 10.0
    chargeback_fee: float = 20.0
    hdr_fee_per_dispute: float = 0.0  # Extra fee if merchant in PENALIZED tier

    ltv_tier: LTVTier = LTVTier.MEDIUM
    claim_type: str = "INR"

    # Seller Protection coverage
    sp_eligible: bool = False

    # Partial settlement context
    allow_partial: bool = True      # Can we offer less than full amount?
    min_offer_pct: float = 0.10     # Minimum we'd offer as a % of disputed amount


def _ev_fight(inp: SettlementInput) -> float:
    """Expected value of fighting (positive = we save money by fighting)."""
    cost_win  = inp.fight_cost
    cost_lose = inp.disputed_amount + inp.chargeback_fee + inp.hdr_fee_per_dispute + inp.fight_cost
    return inp.p_win * (-cost_win) + (1 - inp.p_win) * (-cost_lose)


def _ev_accept(inp: SettlementInput) -> float:
    """EV of simply accepting the dispute (refund immediately)."""
    return -inp.disputed_amount


def _ev_settle(offer: float) -> float:
    """EV of settling at a given offer amount."""
    return -offer


def _optimal_offer(inp: SettlementInput) -> float:
    """
    Compute the offer that is EV-neutral vs. fighting.
    Anything below this is better than fighting.
    """
    # At what settlement amount are we indifferent?
    # EV(settle) = EV(fight) → -offer = EV(fight) → offer = -EV(fight)
    ev_f = _ev_fight(inp)
    indifference_offer = -ev_f  # positive number

    # Apply LTV discount: high-LTV customers get more generous offers
    ltv_factor = {
        LTVTier.VIP:        1.00,  # Willing to pay up to 100% to retain
        LTVTier.HIGH:       0.90,
        LTVTier.MEDIUM:     0.75,
        LTVTier.LOW:        0.60,
        LTVTier.NEGLIGIBLE: 0.40,
    }[inp.ltv_tier]

    offer = indifference_offer * ltv_factor
    # Cap at disputed amount
    offer = min(offer, inp.disputed_amount)
    # Floor at minimum
    min_offer = inp.disputed_amount * inp.min_offer_pct
    offer = max(offer, min_offer)

    return round(offer, 2)


def _strategy(inp: SettlementInput, offer: float) -> SettlementStrategy:
    ev_f = _ev_fight(inp)
    ev_a = _ev_accept(inp)
    ev_s = _ev_settle(offer)

    if inp.sp_eligible and inp.p_win >= 0.65:
        return SettlementStrategy.FULL_FIGHT  # SP covers us, fight aggressively

    if ev_f >= ev_s and ev_f >= ev_a:
        return SettlementStrategy.FULL_FIGHT

    if abs(offer - inp.disputed_amount) < 0.01:
        return SettlementStrategy.FULL_SETTLE

    if offer < inp.disputed_amount * 0.95 and inp.allow_partial:
        return SettlementStrategy.PARTIAL_SETTLE

    return SettlementStrategy.FULL_SETTLE


@dataclass
class SettlementReport:
    """Optimal settlement recommendation."""
    dispute_id: str
    strategy: SettlementStrategy

    optimal_offer: float            # Recommended offer amount
    offer_pct_of_dispute: float     # Offer as % of disputed amount

    ev_fight: float
    ev_settle: float                # EV at the optimal offer
    ev_accept: float

    savings_vs_losing: float        # How much we save vs. losing outright
    is_ev_positive_to_fight: bool

    reasoning: str
    summary: str


def optimize_settlement(inp: SettlementInput) -> SettlementReport:
    """Compute the optimal settlement offer for a dispute."""
    offer = _optimal_offer(inp)
    strategy = _strategy(inp, offer)

    ev_f = _ev_fight(inp)
    ev_s = _ev_settle(offer)
    ev_a = _ev_accept(inp)

    offer_pct = round(offer / inp.disputed_amount * 100, 1) if inp.disputed_amount > 0 else 0.0

    # Savings: settling vs. losing fight outright
    cost_of_losing = inp.disputed_amount + inp.chargeback_fee + inp.hdr_fee_per_dispute + inp.fight_cost
    savings = round(cost_of_losing - offer, 2)

    is_ev_positive = ev_f > ev_a  # Fighting is better than accepting

    # Reasoning
    if strategy == SettlementStrategy.FULL_FIGHT:
        reasoning = (
            f"EV(fight)=${ev_f:.2f} > EV(settle)=${ev_s:.2f}. "
            f"With p_win={inp.p_win:.0%} and fight_cost=${inp.fight_cost:.0f}, "
            f"fighting is the better choice."
        )
    elif strategy == SettlementStrategy.PARTIAL_SETTLE:
        reasoning = (
            f"Offer ${offer:.2f} ({offer_pct:.0f}% of ${inp.disputed_amount:.2f}) "
            f"saves ${savings:.2f} vs. losing outright. "
            f"EV(fight)=${ev_f:.2f}, EV(settle@offer)=${ev_s:.2f}."
        )
    elif strategy == SettlementStrategy.FULL_SETTLE:
        reasoning = (
            f"Full refund avoids ${inp.chargeback_fee:.0f} chargeback fee and "
            f"${inp.hdr_fee_per_dispute:.0f} HDR risk. "
            f"p_win={inp.p_win:.0%} too low to justify fighting."
        )
    else:
        reasoning = "Accept loss — no beneficial settlement path."

    summary = (
        f"Settlement {inp.dispute_id}: "
        f"strategy={strategy.value} | offer=${offer:.2f} ({offer_pct:.0f}%) | "
        f"ev_fight=${ev_f:.2f} | ev_settle=${ev_s:.2f}"
    )

    return SettlementReport(
        dispute_id=inp.dispute_id,
        strategy=strategy,
        optimal_offer=offer,
        offer_pct_of_dispute=offer_pct,
        ev_fight=round(ev_f, 2),
        ev_settle=round(ev_s, 2),
        ev_accept=round(ev_a, 2),
        savings_vs_losing=savings,
        is_ev_positive_to_fight=is_ev_positive,
        reasoning=reasoning,
        summary=summary,
    )
