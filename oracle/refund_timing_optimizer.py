"""
Refund Timing Optimizer — optimal timing for a proactive refund offer.

After a dispute is filed, a merchant has a window to issue a proactive refund
before the dispute escalates to a formal chargeback. The key trade-off:

  Refund NOW:
    - Avoids chargeback fee ($20) if the dispute would have escalated
    - Closes the dispute immediately (no response cost)
    - Costs the full transaction amount (or partial)

  Wait and fight:
    - If win: net = +amount − response_cost
    - If lose: net = −(amount + cb_fee + response_cost)
    - If buyer escalates before you respond: forced to pay even more

The optimal timing window depends on:
  - p_escalation: probability the dispute escalates to chargeback if unresolved
  - p_win: probability of winning the dispute on its merits
  - hours_elapsed: time since dispute was filed (cost of waiting grows)
  - deadline_hours_total: total response window
  - transaction_amount: stake
  - cb_fee: chargeback processing fee (default $20)
  - response_cost: estimated cost of drafting + submitting a response

Decision logic:
  EV(refund_now)   = −amount
  EV(wait_and_win) = p_win×(amount−rc) − (1−p_win)×(amount+cb_fee+rc)
                     where the cb_fee applies only when dispute escalates:
  EV(wait)         = EV(fight) adjusted for escalation probability:
                     (1−p_esc)×fight_ev + p_esc×(cb_loss)

Recommendation:
  REFUND_NOW        — refund EV dominates or deadline < 24h
  PARTIAL_REFUND    — partial concession may close the dispute cheaper
  WAIT_AND_FIGHT    — fight EV dominates
  CONDITIONAL_REFUND — close to breakeven; refund if p_esc > 0.70

PayPal benefit:
  - Merchants who refund before escalation save PayPal the chargeback processing cost
  - Optimal timing guidance reduces the number of disputes that escalate to formal
    chargebacks, lowering PayPal's operational workload by an estimated $12/case
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

_CB_FEE_USD = 20.0
_PAYPAL_SAVINGS_PER_PREVENTED_CB = 12.0


class TimingDecision(str, Enum):
    REFUND_NOW         = "REFUND_NOW"
    PARTIAL_REFUND     = "PARTIAL_REFUND"
    WAIT_AND_FIGHT     = "WAIT_AND_FIGHT"
    CONDITIONAL_REFUND = "CONDITIONAL_REFUND"


@dataclass
class RefundTimingInput:
    dispute_id: str
    transaction_amount: float
    p_win: float                        # 0.0–1.0 probability of winning dispute
    p_escalation: float                 # 0.0–1.0 probability of escalating to chargeback
    hours_elapsed: float                # hours since dispute was filed
    deadline_hours_total: float         # total response window
    response_cost_usd: float = 15.0
    cb_fee_usd: float = _CB_FEE_USD
    partial_refund_rate: float = 0.50   # fraction offered in partial refund


@dataclass
class RefundTimingReport:
    dispute_id: str
    ev_refund_now: float
    ev_wait_and_fight: float
    ev_partial_refund: float
    recommended_decision: TimingDecision
    hours_remaining: float
    deadline_urgency: str               # URGENT / APPROACHING / COMFORTABLE
    escalation_adjusted_loss: float     # expected loss if dispute escalates
    paypal_savings_usd: float           # PayPal saves this if merchant refunds now
    recommendation: str
    paypal_signal: str


def _fight_ev(amount: float, p_win: float, rc: float, cb_fee: float) -> float:
    """Base fight EV without escalation adjustment."""
    win_outcome  =  amount - rc
    lose_outcome = -(amount + cb_fee + rc)
    return p_win * win_outcome + (1.0 - p_win) * lose_outcome


def calculate_refund_timing(inp: RefundTimingInput) -> RefundTimingReport:
    amount = inp.transaction_amount
    rc = inp.response_cost_usd
    cb_fee = inp.cb_fee_usd
    p_win = max(0.0, min(1.0, inp.p_win))
    p_esc = max(0.0, min(1.0, inp.p_escalation))

    hours_remaining = max(0.0, inp.deadline_hours_total - inp.hours_elapsed)

    # EV(refund now)
    ev_refund = -amount

    # EV(partial refund)
    partial_amount = amount * inp.partial_refund_rate
    ev_partial = -partial_amount

    # EV(wait and fight) — adjusted for escalation probability
    base_fight_ev = _fight_ev(amount, p_win, rc, cb_fee)
    # If the dispute escalates before merchant responds, they lose everything + cb_fee
    escalation_loss = -(amount + cb_fee)
    # Weighted: (1−p_esc)×fight normally + p_esc×forced_loss
    ev_wait = (1.0 - p_esc) * base_fight_ev + p_esc * escalation_loss
    ev_wait = round(ev_wait, 4)

    ev_refund = round(ev_refund, 4)
    ev_partial = round(ev_partial, 4)

    escalation_adj_loss = round(p_esc * escalation_loss, 4)

    # Deadline urgency
    if hours_remaining <= 24:
        urgency = "URGENT"
    elif hours_remaining <= 72:
        urgency = "APPROACHING"
    else:
        urgency = "COMFORTABLE"

    # Decision
    ev_diff = ev_wait - ev_refund  # positive = fight is better
    partial_diff = ev_wait - ev_partial  # positive = fight is better than partial

    if hours_remaining <= 24:
        # Almost no time left — refund or partial to avoid default
        decision = TimingDecision.REFUND_NOW if ev_refund >= ev_partial else TimingDecision.PARTIAL_REFUND
    elif ev_wait > ev_partial and ev_wait > ev_refund and ev_diff > 5.0:
        decision = TimingDecision.WAIT_AND_FIGHT
    elif ev_partial > ev_refund and abs(partial_diff) <= 5.0:
        decision = TimingDecision.PARTIAL_REFUND
    elif abs(ev_diff) <= 5.0:
        # EV close — depends on escalation risk
        if p_esc >= 0.70:
            decision = TimingDecision.CONDITIONAL_REFUND
        else:
            decision = TimingDecision.WAIT_AND_FIGHT
    elif ev_wait > ev_refund:
        decision = TimingDecision.WAIT_AND_FIGHT
    else:
        decision = TimingDecision.REFUND_NOW

    # PayPal savings if merchant refunds before escalation
    # PayPal saves processing cost only if dispute would have escalated
    pp_savings = round(p_esc * _PAYPAL_SAVINGS_PER_PREVENTED_CB, 2) if decision in (
        TimingDecision.REFUND_NOW, TimingDecision.PARTIAL_REFUND, TimingDecision.CONDITIONAL_REFUND
    ) else 0.0

    # Recommendation text
    if decision == TimingDecision.REFUND_NOW:
        rec = (
            f"Issue a full refund of ${amount:.2f} now. "
            f"EV(refund)=${ev_refund:.2f} vs EV(fight)=${ev_wait:.2f}. "
            + (f"Only {hours_remaining:.0f}h remaining — time risk too high. " if urgency == "URGENT" else "")
            + f"Escalation risk: {p_esc:.0%}."
        )
    elif decision == TimingDecision.PARTIAL_REFUND:
        rec = (
            f"Offer a partial refund of ${partial_amount:.2f} ({inp.partial_refund_rate:.0%}). "
            f"EV(partial)=${ev_partial:.2f} vs EV(fight)=${ev_wait:.2f}. "
            f"This may close the dispute without a full concession."
        )
    elif decision == TimingDecision.WAIT_AND_FIGHT:
        rec = (
            f"Fight the dispute. EV(fight)=${ev_wait:.2f} vs EV(refund)=${ev_refund:.2f} "
            f"(+${ev_diff:.2f} advantage). Win probability: {p_win:.0%}."
        )
    else:
        rec = (
            f"Conditional: refund if escalation probability exceeds 70% "
            f"(currently {p_esc:.0%}). EV(fight)=${ev_wait:.2f} ≈ EV(refund)=${ev_refund:.2f}."
        )

    paypal_signal = (
        f"Dispute {inp.dispute_id}: decision={decision.value}, "
        f"ev_wait={ev_wait:.2f}, ev_refund={ev_refund:.2f}, "
        f"p_esc={p_esc:.0%}, urgency={urgency}, pp_savings=${pp_savings:.2f}."
    )

    return RefundTimingReport(
        dispute_id=inp.dispute_id,
        ev_refund_now=ev_refund,
        ev_wait_and_fight=ev_wait,
        ev_partial_refund=ev_partial,
        recommended_decision=decision,
        hours_remaining=round(hours_remaining, 1),
        deadline_urgency=urgency,
        escalation_adjusted_loss=escalation_adj_loss,
        paypal_savings_usd=pp_savings,
        recommendation=rec,
        paypal_signal=paypal_signal,
    )
