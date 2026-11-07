"""
Settlement Offer Evaluator — decides whether to accept, counter, or reject
a PayPal settlement offer based on expected-value analysis.

When PayPal mediates a dispute, either party may propose a partial settlement.
This oracle computes whether the offer beats the EV of fighting on and returns
a recommended response with reasoning and a counter-offer amount if warranted.

Decision logic:
  EV(fight) = p_win × amount − (1−p_win) × (amount + chargeback_fee) − response_cost
  EV(settle) = amount − offer_amount − response_cost

  → ACCEPT when EV(settle) ≥ EV(fight) OR when EV(fight) < 0
  → COUNTER when EV(fight) > EV(settle) AND a fair split exists
  → REJECT  when EV(fight) >> EV(settle) AND evidence is strong

Counter-offer formula:
  counter = amount × (1 − p_win × (1 − _COUNTER_DISCOUNT))
  where _COUNTER_DISCOUNT=0.10 leaves 10% room to close at ACCEPT.

Chargeback fee default: $20 (typical PayPal dispute fee)
Response cost: time+labour cost of fighting, default $10

PayPal benefit:
  - Faster settlement resolution reduces PayPal mediation queue
  - EV-rational decisions prevent merchants from rejecting good offers
  - Counter-offer guidance narrows the gap for faster bilateral agreement
  - Transparent reasoning improves merchant trust in the platform
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_CHARGEBACK_FEE = 20.0   # USD
_DEFAULT_RESPONSE_COST  = 10.0   # USD — opportunity cost of preparing a response
_COUNTER_DISCOUNT       = 0.10   # 10% below the EV-neutral point → room to close


# ── Enums ─────────────────────────────────────────────────────────────────────

class SettlementDecision(str, Enum):
    ACCEPT  = "ACCEPT"   # offer beats or ties EV(fight)
    COUNTER = "COUNTER"  # offer is below EV but a counter makes sense
    REJECT  = "REJECT"   # offer is far below EV; fight with strong evidence


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class SettlementOfferInput:
    dispute_id: str
    claim_type: str              # INR | SNAD | UNAUTH | OTHER
    original_amount_usd: float   # full transaction amount
    offer_amount_usd: float      # what the buyer is asking for in settlement
    win_probability: float       # oracle win-probability estimate
    chargeback_fee_usd: float = _DEFAULT_CHARGEBACK_FEE
    response_cost_usd: float  = _DEFAULT_RESPONSE_COST
    days_until_deadline: int  = 7
    evidence_strength: float  = 0.5   # 0=none, 1=perfect (from wave 87 coverage score)


@dataclass
class SettlementEvaluationReport:
    dispute_id: str
    decision: SettlementDecision
    ev_fight: float
    ev_settle: float
    counter_offer_usd: float       # 0 if decision is ACCEPT or REJECT
    accept_threshold_usd: float    # maximum offer at which ACCEPT is EV-optimal
    reasoning: str
    factors: List[str] = field(default_factory=list)


# ── Core logic ────────────────────────────────────────────────────────────────

def _ev_fight(amount: float, p_win: float, cb_fee: float, resp_cost: float) -> float:
    """EV of fighting the dispute to PayPal resolution."""
    return p_win * amount - (1 - p_win) * (amount + cb_fee) - resp_cost


def _ev_settle(amount: float, offer: float, resp_cost: float) -> float:
    """EV of accepting the settlement offer (net recovery)."""
    return amount - offer - resp_cost


def _counter_offer(amount: float, p_win: float) -> float:
    """Counter-offer: EV-neutral split minus COUNTER_DISCOUNT for negotiation room."""
    # EV-neutral offer: amount × (1 − p_win) = breakeven offer
    breakeven = amount * (1 - p_win)
    return max(0.0, round(breakeven * (1 - _COUNTER_DISCOUNT), 2))


def evaluate_settlement(inp: SettlementOfferInput) -> SettlementEvaluationReport:
    """Evaluate a settlement offer and return a recommended decision."""
    ev_f = round(_ev_fight(
        inp.original_amount_usd,
        inp.win_probability,
        inp.chargeback_fee_usd,
        inp.response_cost_usd,
    ), 4)

    ev_s = round(_ev_settle(
        inp.original_amount_usd,
        inp.offer_amount_usd,
        inp.response_cost_usd,
    ), 4)

    # Accept threshold: maximum offer such that EV(settle) ≥ EV(fight)
    # EV(settle) = amount - threshold - cost ≥ EV(fight)
    # threshold = amount - cost - EV(fight)
    accept_threshold = round(
        inp.original_amount_usd - inp.response_cost_usd - ev_f, 2
    )

    factors: List[str] = []

    # Deadline urgency
    if inp.days_until_deadline <= 2:
        factors.append(f"Deadline pressure: only {inp.days_until_deadline} day(s) remaining.")

    # Evidence
    if inp.evidence_strength >= 0.80:
        factors.append(f"Strong evidence (coverage {inp.evidence_strength:.0%}) supports fighting.")
    elif inp.evidence_strength < 0.40:
        factors.append(f"Weak evidence (coverage {inp.evidence_strength:.0%}) reduces fight EV.")

    # Win probability
    if inp.win_probability >= 0.70:
        factors.append(f"High win probability ({inp.win_probability:.0%}) favours rejection.")
    elif inp.win_probability < 0.40:
        factors.append(f"Low win probability ({inp.win_probability:.0%}) favours acceptance.")

    # Decide
    if ev_s >= ev_f or ev_f < 0:
        decision = SettlementDecision.ACCEPT
        counter  = 0.0
        reasoning = (
            f"ACCEPT: Settlement offer (net +${ev_s:.2f}) meets or beats the EV of fighting "
            f"(EV(fight)={ev_f:.2f}). Settling saves time and avoids the {inp.chargeback_fee_usd:.0f} "
            f"chargeback fee if the dispute is lost."
        )
    else:
        gap = ev_f - ev_s
        # Counter if the gap is closable; reject if evidence is strong and EV(fight) is clearly better
        if inp.evidence_strength >= 0.70 and ev_f > 0 and gap > inp.original_amount_usd * 0.20:
            decision = SettlementDecision.REJECT
            counter  = 0.0
            reasoning = (
                f"REJECT: EV(fight)={ev_f:.2f} substantially exceeds EV(settle)={ev_s:.2f} "
                f"and evidence is strong ({inp.evidence_strength:.0%}). "
                f"Submit evidence and fight the dispute."
            )
        else:
            decision = SettlementDecision.COUNTER
            counter  = _counter_offer(inp.original_amount_usd, inp.win_probability)
            reasoning = (
                f"COUNTER: Offer of ${inp.offer_amount_usd:.2f} is below the EV-optimal threshold "
                f"(${accept_threshold:.2f}). Counter-offer ${counter:.2f} reflects the EV-neutral "
                f"split with a 10% negotiation buffer."
            )
        # Deadline override: if deadline is very soon and evidence is weak, accept anyway
        if inp.days_until_deadline <= 1 and inp.evidence_strength < 0.50:
            decision = SettlementDecision.ACCEPT
            counter  = 0.0
            reasoning = (
                f"ACCEPT (deadline override): Only {inp.days_until_deadline} day(s) remain and "
                f"evidence coverage is {inp.evidence_strength:.0%}. Accepting the settlement avoids "
                f"an auto-loss. Original recommendation was {decision.value}."
            )
            factors.append("Deadline override: insufficient time to gather evidence.")

    return SettlementEvaluationReport(
        dispute_id=inp.dispute_id,
        decision=decision,
        ev_fight=ev_f,
        ev_settle=ev_s,
        counter_offer_usd=counter,
        accept_threshold_usd=accept_threshold,
        reasoning=reasoning,
        factors=factors,
    )
