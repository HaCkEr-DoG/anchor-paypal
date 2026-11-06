"""
Refund Calculator — compute the net financial impact of full or
partial refunds across multiple dispute resolution strategies.

Answers:
  1. Full refund cost breakdown (revenue loss, gateway fee recovery, etc.)
  2. Partial refund scenarios and their EV impact vs. fighting
  3. Pro-rated refund for subscription disputes
  4. Best refund strategy given buyer LTV and HDR budget

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RefundType(str, Enum):
    FULL        = "FULL"
    PARTIAL     = "PARTIAL"
    PRORATED    = "PRORATED"
    NONE        = "NONE"


class RefundStrategy(str, Enum):
    REFUND_IMMEDIATELY  = "REFUND_IMMEDIATELY"   # Pre-dispute refund to avoid chargeback
    FIGHT_AND_WIN       = "FIGHT_AND_WIN"
    FIGHT_AND_LOSE      = "FIGHT_AND_LOSE"
    NEGOTIATE_PARTIAL   = "NEGOTIATE_PARTIAL"
    ACCEPT_CHARGEBACK   = "ACCEPT_CHARGEBACK"


class RefundRecommendation(str, Enum):
    REFUND_PROACTIVELY  = "REFUND_PROACTIVELY"   # Cheapest path
    NEGOTIATE_PARTIAL   = "NEGOTIATE_PARTIAL"
    FIGHT               = "FIGHT"
    ACCEPT_CHARGEBACK   = "ACCEPT_CHARGEBACK"


@dataclass
class RefundCalculatorInput:
    """Input for refund cost calculation."""
    dispute_id: str
    transaction_amount: float
    reason_code: str

    # Payment processing context
    gateway_fee_pct: float = 0.029     # e.g. 2.9%
    gateway_fee_flat: float = 0.30     # e.g. $0.30 flat
    gateway_refunds_processing_fee: bool = False  # Most don't refund tx fee

    # Dispute/chargeback fees
    chargeback_fee: float = 20.0       # Charged by PayPal/bank on CB
    fight_cost: float = 15.0           # Staff cost to respond

    # Partial refund option
    can_offer_partial: bool = False
    partial_refund_pct: float = 0.50   # e.g. 50% partial refund offer
    partial_refund_fixed: float = 0.0  # Override — exact amount (0 = use pct)

    # Win probability
    win_probability: float = 0.50

    # Subscription context
    is_subscription: bool = False
    subscription_months_used: int = 0
    subscription_total_months: int = 1

    # Buyer LTV
    buyer_ltv: float = 0.0
    is_vip: bool = False

    # HDR context
    hdr_at_risk: bool = False          # True if another dispute tips the tier
    hdr_fee_per_dispute: float = 0.0   # Additional tier fee if HDR triggered


@dataclass
class RefundScenario:
    """Cost breakdown for a single resolution path."""
    strategy: RefundStrategy
    description: str
    gross_loss: float        # Revenue given back
    fees_lost: float         # Processing fees not recovered
    dispute_fees: float      # Chargeback / fight fees
    hdr_fees: float          # Additional HDR tier fees
    ltv_churn_risk: float    # Estimated churn cost
    total_cost: float        # Sum of all costs
    probability: float       # Likelihood of this path (0–1 for EV)
    ev_contribution: float   # probability × total_cost (for EV blend)


@dataclass
class RefundCalculatorReport:
    """Full refund cost analysis."""
    dispute_id: str
    transaction_amount: float

    scenarios: list[RefundScenario]

    cheapest_strategy: RefundStrategy
    recommended_refund_type: RefundType
    recommendation: RefundRecommendation
    recommended_amount: float

    # EV comparison
    ev_fight: float
    ev_refund_full: float
    ev_refund_partial: float
    ev_accept_chargeback: float

    # Partial refund details
    partial_refund_amount: float
    partial_saves_vs_chargeback: float   # Savings vs. losing chargeback

    summary: str


def _gateway_fees(amount: float, pct: float, flat: float) -> float:
    return round(amount * pct + flat, 2)


def _ltv_churn(ltv: float, is_vip: bool) -> float:
    if is_vip:
        return round(ltv * 0.15, 2)
    if ltv > 200:
        return round(ltv * 0.05, 2)
    return 0.0


def calculate_refund(inp: RefundCalculatorInput) -> RefundCalculatorReport:
    """Calculate refund costs across all resolution strategies."""
    amount = inp.transaction_amount
    gw_fee = _gateway_fees(amount, inp.gateway_fee_pct, inp.gateway_fee_flat)
    fees_lost = gw_fee if not inp.gateway_refunds_processing_fee else 0.0
    churn = _ltv_churn(inp.buyer_ltv, inp.is_vip)
    hdr = inp.hdr_fee_per_dispute if inp.hdr_at_risk else 0.0
    p = inp.win_probability

    # ── Scenario A: Refund immediately (pre-dispute) ──────────────
    # Avoid chargeback fee; still lose revenue + gateway fee
    cost_a = round(amount + fees_lost, 2)
    s_a = RefundScenario(
        strategy=RefundStrategy.REFUND_IMMEDIATELY,
        description="Proactive full refund before chargeback is filed",
        gross_loss=amount,
        fees_lost=fees_lost,
        dispute_fees=0.0,
        hdr_fees=hdr,
        ltv_churn_risk=0.0,
        total_cost=round(cost_a + hdr, 2),
        probability=1.0,
        ev_contribution=round(cost_a + hdr, 2),
    )

    # ── Scenario B: Fight and win ─────────────────────────────────
    # Pay fight cost but recover revenue; still lose gateway fee
    cost_b_win = round(fees_lost + inp.fight_cost, 2)
    s_b_win = RefundScenario(
        strategy=RefundStrategy.FIGHT_AND_WIN,
        description="Fight the dispute and win — recover transaction amount",
        gross_loss=0.0,
        fees_lost=fees_lost,
        dispute_fees=inp.fight_cost,
        hdr_fees=hdr,
        ltv_churn_risk=0.0,
        total_cost=round(cost_b_win + hdr, 2),
        probability=p,
        ev_contribution=round(p * (cost_b_win + hdr), 2),
    )

    # ── Scenario C: Fight and lose ────────────────────────────────
    # Lose revenue + gateway fee + fight cost + chargeback fee + churn
    cost_c = round(amount + fees_lost + inp.fight_cost + inp.chargeback_fee + churn, 2)
    s_c_lose = RefundScenario(
        strategy=RefundStrategy.FIGHT_AND_LOSE,
        description="Fight the dispute and lose — pay all fees",
        gross_loss=amount,
        fees_lost=fees_lost,
        dispute_fees=round(inp.fight_cost + inp.chargeback_fee, 2),
        hdr_fees=hdr,
        ltv_churn_risk=churn,
        total_cost=round(cost_c + hdr, 2),
        probability=1 - p,
        ev_contribution=round((1 - p) * (cost_c + hdr), 2),
    )

    # ── Scenario D: Negotiate partial ────────────────────────────
    partial_amount = (inp.partial_refund_fixed if inp.partial_refund_fixed > 0
                      else round(amount * inp.partial_refund_pct, 2))
    partial_fees = _gateway_fees(partial_amount,
                                  inp.gateway_fee_pct, inp.gateway_fee_flat) \
                   if not inp.gateway_refunds_processing_fee else 0.0
    cost_d = round(partial_amount + partial_fees, 2)
    s_d = RefundScenario(
        strategy=RefundStrategy.NEGOTIATE_PARTIAL,
        description=f"Negotiate partial refund of ${partial_amount:.2f}",
        gross_loss=partial_amount,
        fees_lost=partial_fees,
        dispute_fees=0.0,
        hdr_fees=hdr,
        ltv_churn_risk=0.0,
        total_cost=round(cost_d + hdr, 2),
        probability=1.0,
        ev_contribution=round(cost_d + hdr, 2),
    )

    # ── Scenario E: Accept chargeback ────────────────────────────
    cost_e = round(amount + fees_lost + inp.chargeback_fee + churn, 2)
    s_e = RefundScenario(
        strategy=RefundStrategy.ACCEPT_CHARGEBACK,
        description="Accept chargeback — lose all + CB fee",
        gross_loss=amount,
        fees_lost=fees_lost,
        dispute_fees=inp.chargeback_fee,
        hdr_fees=hdr,
        ltv_churn_risk=churn,
        total_cost=round(cost_e + hdr, 2),
        probability=1.0,
        ev_contribution=round(cost_e + hdr, 2),
    )

    # ── EV calculations ───────────────────────────────────────────
    # EV(fight) = p × win_cost + (1-p) × lose_cost
    ev_fight = round(s_b_win.ev_contribution + s_c_lose.ev_contribution, 2)
    ev_refund_full = s_a.total_cost
    ev_refund_partial = s_d.total_cost
    ev_accept_cb = s_e.total_cost

    # Subscriptions: pro-rated partial refund
    if inp.is_subscription and inp.subscription_total_months > 0:
        months_remaining = max(0, inp.subscription_total_months - inp.subscription_months_used)
        prorated_pct = months_remaining / inp.subscription_total_months
        partial_amount = round(amount * prorated_pct, 2)

    partial_saves = round(ev_accept_cb - ev_refund_partial, 2)

    # ── Cheapest / recommended ────────────────────────────────────
    ev_map = {
        RefundStrategy.REFUND_IMMEDIATELY:  ev_refund_full,
        RefundStrategy.FIGHT_AND_WIN:       ev_fight,     # EV blend of B+C
        RefundStrategy.NEGOTIATE_PARTIAL:   ev_refund_partial if inp.can_offer_partial else 9999,
        RefundStrategy.ACCEPT_CHARGEBACK:   ev_accept_cb,
    }

    cheapest = min(ev_map, key=lambda k: ev_map[k])

    # Recommendation logic
    if ev_fight < ev_refund_full and ev_fight < ev_refund_partial:
        rec = RefundRecommendation.FIGHT
        refund_type = RefundType.NONE
        rec_amount = 0.0
    elif inp.can_offer_partial and ev_refund_partial < ev_refund_full:
        rec = RefundRecommendation.NEGOTIATE_PARTIAL
        refund_type = RefundType.PARTIAL if not inp.is_subscription else RefundType.PRORATED
        rec_amount = partial_amount
    elif ev_refund_full < ev_accept_cb:
        rec = RefundRecommendation.REFUND_PROACTIVELY
        refund_type = RefundType.FULL
        rec_amount = amount
    else:
        rec = RefundRecommendation.ACCEPT_CHARGEBACK
        refund_type = RefundType.NONE
        rec_amount = 0.0

    scenarios = [s_a, s_b_win, s_c_lose, s_d, s_e]

    summary = (
        f"Refund {inp.dispute_id}: "
        f"ev_fight=${ev_fight:.0f} | ev_full=${ev_refund_full:.0f} | "
        f"ev_partial=${ev_refund_partial:.0f} | rec={rec.value}"
    )

    return RefundCalculatorReport(
        dispute_id=inp.dispute_id,
        transaction_amount=amount,
        scenarios=scenarios,
        cheapest_strategy=cheapest,
        recommended_refund_type=refund_type,
        recommendation=rec,
        recommended_amount=rec_amount,
        ev_fight=ev_fight,
        ev_refund_full=ev_refund_full,
        ev_refund_partial=ev_refund_partial,
        ev_accept_chargeback=ev_accept_cb,
        partial_refund_amount=partial_amount,
        partial_saves_vs_chargeback=partial_saves,
        summary=summary,
    )
