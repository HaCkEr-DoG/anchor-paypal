"""
Chargeback Cost Calculator — compute the full cost of a dispute across
every possible outcome path: respond and win, respond and lose, accept,
escalate to pre-arb, escalate to arbitration.

For each dispute this oracle computes:
  - Total cost for each outcome path
  - Break-even win probability (below which accepting is cheaper)
  - Optimal path based on expected value
  - Per-network fee schedule (chargeback fee, pre-arb fee, arb fee)
  - Opportunity cost of response time

Networks: PAYPAL, VISA, MC, AMEX, DISCOVER

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Network(str, Enum):
    PAYPAL   = "PAYPAL"
    VISA     = "VISA"
    MC       = "MC"
    AMEX     = "AMEX"
    DISCOVER = "DISCOVER"


class OutcomePath(str, Enum):
    ACCEPT_NOW        = "ACCEPT_NOW"
    RESPOND_WIN       = "RESPOND_WIN"
    RESPOND_LOSE      = "RESPOND_LOSE"
    PRE_ARB_WIN       = "PRE_ARB_WIN"
    PRE_ARB_LOSE      = "PRE_ARB_LOSE"
    ARBITRATION_WIN   = "ARBITRATION_WIN"
    ARBITRATION_LOSE  = "ARBITRATION_LOSE"


@dataclass
class PathCost:
    """Cost breakdown for one outcome path."""
    path: OutcomePath
    dispute_amount: float
    chargeback_fee: float
    response_cost: float        # Internal operational cost to respond
    pre_arb_fee: float
    arbitration_fee: float
    total_cost: float            # Sum of all applicable costs
    net_recovery: float          # Positive = recovered, negative = loss
    description: str


@dataclass
class ChargebackCostInput:
    """Input for chargeback cost calculation."""
    dispute_id: str
    transaction_amount: float
    network: str

    # Win probabilities
    win_prob_response: float = 0.50        # Prob of winning at chargeback stage
    win_prob_pre_arb: float = 0.40         # Prob of winning at pre-arb
    win_prob_arbitration: float = 0.35     # Prob of winning at arbitration

    # Internal costs
    response_cost: float = 15.0            # Staff/ops cost to file a response
    pre_arb_response_cost: float = 25.0    # Higher due to complexity
    arb_response_cost: float = 50.0        # Legal/expert cost for arb

    # Optional overrides (if None, use network defaults)
    chargeback_fee_override: Optional[float] = None
    pre_arb_fee_override: Optional[float] = None
    arbitration_fee_override: Optional[float] = None


@dataclass
class ChargebackCostReport:
    """Full cost analysis for a dispute."""
    dispute_id: str
    transaction_amount: float
    network: str

    # Fee schedule used
    chargeback_fee: float
    pre_arb_fee: float
    arbitration_fee: float

    # Path costs
    paths: list[PathCost]

    # Key analytics
    cost_to_accept: float
    ev_respond: float              # EV of responding at chargeback stage
    ev_escalate_pre_arb: float     # EV of escalating further (after losing chargeback)
    ev_arbitration: float          # EV of going to arb (after losing pre-arb)

    break_even_win_prob: float     # Win prob where EV(fight) == EV(accept)
    optimal_path: OutcomePath
    optimal_path_ev: float

    should_fight: bool
    should_escalate: bool          # Pre-arb escalation worthwhile?
    should_arbitrate: bool

    recommendation: str
    summary: str


# Network-specific fee defaults
_CB_FEES: dict[str, float]  = {
    "PAYPAL": 20.0, "VISA": 15.0, "MC": 15.0, "AMEX": 25.0, "DISCOVER": 15.0,
}
_PRE_ARB_FEES: dict[str, float] = {
    "PAYPAL": 0.0, "VISA": 150.0, "MC": 150.0, "AMEX": 0.0, "DISCOVER": 100.0,
}
_ARB_FEES: dict[str, float] = {
    "PAYPAL": 0.0, "VISA": 500.0, "MC": 500.0, "AMEX": 0.0, "DISCOVER": 250.0,
}


def calculate_chargeback_cost(inp: ChargebackCostInput) -> ChargebackCostReport:
    """Calculate full cost landscape for a dispute decision."""
    net = inp.network.upper()
    cb_fee  = inp.chargeback_fee_override  if inp.chargeback_fee_override  is not None else _CB_FEES.get(net, 20.0)
    pa_fee  = inp.pre_arb_fee_override     if inp.pre_arb_fee_override     is not None else _PRE_ARB_FEES.get(net, 0.0)
    arb_fee = inp.arbitration_fee_override if inp.arbitration_fee_override is not None else _ARB_FEES.get(net, 0.0)

    amount = inp.transaction_amount

    # ── Build all path costs ──────────────────────────────────────

    # Accept: pay full amount + chargeback fee
    cost_accept = amount + cb_fee

    # Respond and win: pay only response cost
    cost_respond_win = inp.response_cost

    # Respond and lose: pay amount + cb fee + response cost
    cost_respond_lose = amount + cb_fee + inp.response_cost

    # Pre-arb win: pay response + pre-arb response cost + pa_fee
    cost_pre_arb_win = inp.response_cost + inp.pre_arb_response_cost + pa_fee

    # Pre-arb lose: same as above plus the amount lost
    cost_pre_arb_lose = amount + cb_fee + inp.response_cost + inp.pre_arb_response_cost + pa_fee

    # Arbitration win
    cost_arb_win = inp.response_cost + inp.pre_arb_response_cost + inp.arb_response_cost + pa_fee + arb_fee

    # Arbitration lose
    cost_arb_lose = (amount + cb_fee + inp.response_cost + inp.pre_arb_response_cost +
                     inp.arb_response_cost + pa_fee + arb_fee)

    paths = [
        PathCost(
            OutcomePath.ACCEPT_NOW, amount, cb_fee, 0, 0, 0,
            round(cost_accept, 2),
            round(-cost_accept, 2),
            "Pay chargeback immediately without responding",
        ),
        PathCost(
            OutcomePath.RESPOND_WIN, amount, cb_fee, inp.response_cost, 0, 0,
            round(cost_respond_win, 2),
            round(amount - inp.response_cost, 2),
            "Respond to chargeback, win: recover amount minus response cost",
        ),
        PathCost(
            OutcomePath.RESPOND_LOSE, amount, cb_fee, inp.response_cost, 0, 0,
            round(cost_respond_lose, 2),
            round(-cost_respond_lose, 2),
            "Respond to chargeback, lose: pay amount + fee + response cost",
        ),
        PathCost(
            OutcomePath.PRE_ARB_WIN, amount, cb_fee, inp.response_cost,
            pa_fee, 0,
            round(cost_pre_arb_win, 2),
            round(amount - cost_pre_arb_win, 2),
            "Escalate to pre-arb, win: recover amount minus fees",
        ),
        PathCost(
            OutcomePath.PRE_ARB_LOSE, amount, cb_fee, inp.response_cost,
            pa_fee, 0,
            round(cost_pre_arb_lose, 2),
            round(-cost_pre_arb_lose, 2),
            "Escalate to pre-arb, lose: total loss including fees",
        ),
        PathCost(
            OutcomePath.ARBITRATION_WIN, amount, cb_fee, inp.response_cost,
            pa_fee, arb_fee,
            round(cost_arb_win, 2),
            round(amount - cost_arb_win, 2),
            "Go to arbitration, win: recover amount minus all fees",
        ),
        PathCost(
            OutcomePath.ARBITRATION_LOSE, amount, cb_fee, inp.response_cost,
            pa_fee, arb_fee,
            round(cost_arb_lose, 2),
            round(-cost_arb_lose, 2),
            "Go to arbitration, lose: total loss including all fees",
        ),
    ]

    # ── Expected values ───────────────────────────────────────────
    p = inp.win_prob_response
    ev_respond = (p * (amount - inp.response_cost)
                  - (1 - p) * cost_respond_lose)

    p_pa = inp.win_prob_pre_arb
    ev_pre_arb = (p_pa * (amount - cost_pre_arb_win)
                  - (1 - p_pa) * cost_pre_arb_lose)

    p_arb = inp.win_prob_arbitration
    ev_arb = (p_arb * (amount - cost_arb_win)
              - (1 - p_arb) * cost_arb_lose)

    # Break-even: EV(fight) = EV(accept) → p* where both are equal
    # p*(amount - rc) - (1-p*)*(amount+cb+rc) = -(amount+cb)
    # p*A - rc - (amount+cb+rc) + p*(amount+cb+rc) = -(amount+cb)
    # p*(A + amount+cb+rc) = amount+cb+rc - amount - cb = rc … simplified:
    # Solve: p*(amount-rc) - (1-p)*(amount+cb+rc) = -(amount+cb)
    denom = amount - inp.response_cost + amount + cb_fee + inp.response_cost
    if denom > 0:
        break_even = (amount + cb_fee) / denom
        break_even = max(0.0, min(1.0, round(break_even, 4)))
    else:
        break_even = 1.0

    # Optimal path
    should_fight     = ev_respond > -cost_accept
    should_escalate  = pa_fee > 0 and ev_pre_arb > ev_respond
    should_arbitrate = arb_fee > 0 and ev_arb > ev_pre_arb and arb_fee < amount * 0.30

    if should_fight and should_escalate:
        optimal = OutcomePath.PRE_ARB_WIN
        optimal_ev = ev_pre_arb
    elif should_fight:
        optimal = OutcomePath.RESPOND_WIN
        optimal_ev = ev_respond
    else:
        optimal = OutcomePath.ACCEPT_NOW
        optimal_ev = -cost_accept

    if should_fight:
        rec = f"Fight dispute (EV ${ev_respond:.0f} vs accept -${cost_accept:.0f})"
        if should_escalate:
            rec += f"; escalate to pre-arb if lost (EV ${ev_pre_arb:.0f})"
    else:
        rec = f"Accept (fight EV ${ev_respond:.0f} < accept cost -${cost_accept:.0f}); break-even win prob = {break_even:.0%}"

    summary = (
        f"Cost {inp.dispute_id} [{net}]: "
        f"amount=${amount:.0f} | "
        f"ev_fight=${ev_respond:.0f} ev_accept=-${cost_accept:.0f} | "
        f"break_even={break_even:.0%}"
    )

    return ChargebackCostReport(
        dispute_id=inp.dispute_id,
        transaction_amount=amount,
        network=net,
        chargeback_fee=cb_fee,
        pre_arb_fee=pa_fee,
        arbitration_fee=arb_fee,
        paths=paths,
        cost_to_accept=round(cost_accept, 2),
        ev_respond=round(ev_respond, 2),
        ev_escalate_pre_arb=round(ev_pre_arb, 2),
        ev_arbitration=round(ev_arb, 2),
        break_even_win_prob=break_even,
        optimal_path=optimal,
        optimal_path_ev=round(optimal_ev, 2),
        should_fight=should_fight,
        should_escalate=should_escalate,
        should_arbitrate=should_arbitrate,
        recommendation=rec,
        summary=summary,
    )
