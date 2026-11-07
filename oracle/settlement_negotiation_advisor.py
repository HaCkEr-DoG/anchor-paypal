"""
Settlement Negotiation Advisor — model the EV of accepting vs. rejecting a settlement.

When a dispute enters negotiation, PayPal or the buyer may propose a partial
settlement (e.g., "pay the buyer $X out of $Y claimed"). This oracle models:

  EV(accept)   = −settlement_amount
  EV(reject)   = p_win×(0) − (1−p_win)×(amount + cb_fee + response_cost)
                 = −(1−p_win)×(total_loss_if_lose)

  Accept if:  settlement_amount < (1−p_win) × total_loss_if_lose
  i.e., the settlement is cheaper than the expected loss from fighting

Counteroffer logic:
  Optimal counteroffer = (1−p_win) × total_loss_if_lose × discount_factor
  where discount_factor = 0.80 (20% negotiation margin)
  → If the optimal counteroffer is less than 10% of the original amount, skip it
  → Round to nearest $5 for cleaner negotiation

PayPal benefit:
  - Settlements avoid formal chargebacks (~$30/case PayPal processing cost)
  - Mediated settlements through PayPal's Resolution Center are faster
  - Merchants with optimized settlement behavior have lower chargeback rates
  - PayPal earns trust when both sides feel advised fairly
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

_CB_FEE_USD = 20.0
_PAYPAL_CB_PROCESSING_COST = 30.0
_NEGOTIATION_MARGIN = 0.80       # offer 80% of what we'd accept at max
_MIN_COUNTEROFFER_PCT = 0.10     # don't bother if < 10% of claimed amount


class SettlementDecision(str, Enum):
    ACCEPT           = "ACCEPT"
    REJECT_AND_FIGHT = "REJECT_AND_FIGHT"
    COUNTEROFFER     = "COUNTEROFFER"


@dataclass
class SettlementNegotiationInput:
    dispute_id: str
    claimed_amount: float               # what the buyer is claiming
    settlement_offer_amount: float      # what the buyer/PayPal is proposing
    p_win: float                        # probability merchant wins if dispute goes to decision
    response_cost_usd: float = 15.0
    cb_fee_usd: float = _CB_FEE_USD
    already_responded: bool = False     # True if merchant already submitted a response


@dataclass
class SettlementNegotiationReport:
    dispute_id: str
    ev_accept: float
    ev_reject: float
    settlement_threshold: float         # max amount it's worth paying to settle
    recommended_decision: SettlementDecision
    optimal_counteroffer: Optional[float]   # None if ACCEPT or REJECT
    total_savings_vs_worst_case: float  # how much merchant saves vs. losing outright
    paypal_cb_savings_usd: float        # PayPal saves this if settled (avoids CB)
    recommendation: str
    paypal_signal: str


def _round_to_five(amount: float) -> float:
    """Round to nearest $5 for cleaner negotiation."""
    return round(amount / 5.0) * 5.0


def advise_settlement(inp: SettlementNegotiationInput) -> SettlementNegotiationReport:
    amount = inp.claimed_amount
    offer = inp.settlement_offer_amount
    p_win = max(0.0, min(1.0, inp.p_win))
    rc = inp.response_cost_usd if not inp.already_responded else 0.0
    cb_fee = inp.cb_fee_usd

    # EV(accept): just pay the settlement
    ev_accept = round(-offer, 4)

    # EV(reject): fight the dispute
    # If win: save response_cost (already spent), net = −rc (sunk if already responded)
    # If lose: pay amount + cb_fee + rc
    win_outcome  = -rc          # sunk cost of responding (0 if already responded)
    lose_outcome = -(amount + cb_fee + rc)
    ev_reject = round(p_win * win_outcome + (1.0 - p_win) * lose_outcome, 4)

    # Settlement threshold: amount where EV(accept) = EV(reject)
    # -threshold = ev_reject → threshold = -ev_reject
    threshold = round(-ev_reject, 4)
    threshold = max(0.0, threshold)

    # Decision
    if offer <= threshold:
        # Settling is at least as good as fighting
        decision = SettlementDecision.ACCEPT
        counteroffer = None
    else:
        # Offer is too high — but can we counteroffer?
        optimal_raw = threshold * _NEGOTIATION_MARGIN
        if optimal_raw < amount * _MIN_COUNTEROFFER_PCT or optimal_raw <= 0:
            # Counteroffer would be trivially small — just fight
            decision = SettlementDecision.REJECT_AND_FIGHT
            counteroffer = None
        else:
            counteroffer = _round_to_five(optimal_raw)
            # If counteroffer is within $5 of original offer, just accept
            if abs(counteroffer - offer) < 5.0:
                decision = SettlementDecision.ACCEPT
                counteroffer = None
            else:
                decision = SettlementDecision.COUNTEROFFER

    # Savings vs. worst case (losing outright)
    worst_case = abs(lose_outcome)
    if decision == SettlementDecision.ACCEPT:
        paid = offer
    elif decision == SettlementDecision.COUNTEROFFER and counteroffer:
        paid = counteroffer
    else:
        paid = abs(ev_reject)  # expected pay if fighting

    total_savings = round(worst_case - paid, 2)

    # PayPal saves chargeback processing cost if settled
    pp_savings = round(_PAYPAL_CB_PROCESSING_COST * (1.0 - p_win), 2)

    # Recommendation text
    if decision == SettlementDecision.ACCEPT:
        rec = (
            f"Accept the ${offer:.2f} settlement. "
            f"Threshold is ${threshold:.2f} — offer is at or below threshold. "
            f"EV(accept)=${ev_accept:.2f} vs EV(fight)=${ev_reject:.2f}."
        )
    elif decision == SettlementDecision.COUNTEROFFER:
        rec = (
            f"Counteroffer ${counteroffer:.2f} (down from ${offer:.2f}). "
            f"Optimal threshold=${threshold:.2f}; counteroffer is at 80% of threshold. "
            f"EV(fight)=${ev_reject:.2f} provides a floor."
        )
    else:
        rec = (
            f"Reject settlement and fight. "
            f"Offer ${offer:.2f} exceeds acceptable threshold ${threshold:.2f} "
            f"and counteroffer would be too small to propose. "
            f"Win probability: {p_win:.0%}."
        )

    paypal_signal = (
        f"Dispute {inp.dispute_id}: decision={decision.value}, "
        f"offer=${offer:.2f}, threshold=${threshold:.2f}, "
        + (f"counteroffer=${counteroffer:.2f}, " if counteroffer else "")
        + f"pp_savings=${pp_savings:.2f}."
    )

    return SettlementNegotiationReport(
        dispute_id=inp.dispute_id,
        ev_accept=ev_accept,
        ev_reject=ev_reject,
        settlement_threshold=threshold,
        recommended_decision=decision,
        optimal_counteroffer=counteroffer,
        total_savings_vs_worst_case=total_savings,
        paypal_cb_savings_usd=pp_savings,
        recommendation=rec,
        paypal_signal=paypal_signal,
    )
