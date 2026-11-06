"""
Refund Optimizer — intelligent partial/full refund strategy engine.

When a merchant decides to refund (or is considering it), the question becomes:
  - How much to refund?
  - Full refund or partial?
  - Is a proactive refund (before formal dispute) better than waiting?
  - Does a refund close the dispute or only partially satisfy the buyer?

Refund math:
  Cost(full_refund)     = dispute_amount
  Cost(partial_refund)  = refund_amount
  Cost(fight_and_lose)  = dispute_amount + fight_cost (HDR impact + internal cost)
  Cost(fight_and_win)   = fight_cost only
  EV(fight)             = P(win) × fight_cost − (1−P(win)) × (amount + fight_cost)
                        = P(win) × amount − fight_cost  [simplified]

Proactive refund (before dispute escalates to PayPal formal review):
  - Closes dispute faster → lower HDR impact (removed from count sooner)
  - No fight cost incurred
  - Buyer more likely to accept and not re-dispute

Partial refund rationale (applicable for SNAD, digital goods, partial delivery):
  - When merchant can demonstrate partial delivery/value
  - Reduces payout while keeping buyer somewhat satisfied
  - Requires buyer agreement — if unresponsive, full refund may close faster

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RefundStrategy(str, Enum):
    FULL_PROACTIVE      = "FULL_PROACTIVE"    # Refund now before formal dispute
    FULL_REACTIVE       = "FULL_REACTIVE"     # Refund after dispute escalation
    PARTIAL_PROACTIVE   = "PARTIAL_PROACTIVE" # Partial now — negotiate with buyer
    PARTIAL_REACTIVE    = "PARTIAL_REACTIVE"  # Partial after dispute
    FIGHT_NO_REFUND     = "FIGHT_NO_REFUND"   # Fight — do not refund
    CONDITIONAL_REFUND  = "CONDITIONAL_REFUND"# Refund only if buyer re-contacts


class RefundTrigger(str, Enum):
    LOW_WIN_PROBABILITY = "LOW_WIN_PROBABILITY"
    HIGH_FIGHT_COST     = "HIGH_FIGHT_COST"
    HDR_PRESSURE        = "HDR_PRESSURE"
    TINY_AMOUNT         = "TINY_AMOUNT"
    PARTIAL_DELIVERY    = "PARTIAL_DELIVERY"
    REPEAT_BUYER        = "REPEAT_BUYER"
    NEAR_DEADLINE       = "NEAR_DEADLINE"
    STRONG_CASE         = "STRONG_CASE"       # Trigger for FIGHT strategy


@dataclass
class RefundInput:
    """State of one dispute when deciding refund strategy."""
    dispute_id: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED
    dispute_amount: float

    # Win probability
    p_win: float                  # 0-1

    # Delivery & fulfillment context
    was_delivered: bool = False   # Tracking shows delivered
    partial_delivery_pct: float = 1.0  # Fraction of order actually delivered (0-1)
    is_digital_product: bool = False

    # Buyer context
    buyer_dispute_count_90d: int = 0
    buyer_purchase_history_count: int = 1
    buyer_is_long_term_customer: bool = False

    # Financial & operational context
    fight_cost: float = 25.0
    is_hdr_contributing: bool = True
    days_until_deadline: int = 10
    is_formal_dispute: bool = True  # False = still in messaging phase

    # HDR pressure level (0=none, 1=elevated 0.5%, 2=limited 1.0%, 3=severe 1.5%+)
    hdr_pressure_level: int = 0


@dataclass
class RefundScenario:
    """One evaluated refund scenario with cost/benefit analysis."""
    strategy: RefundStrategy
    refund_amount: float
    total_cost: float              # Net cost to merchant
    ev_vs_fight: float            # $ advantage over fighting (negative = fight better)
    hdr_resolution_days: int      # Estimated days until dispute closes
    buyer_acceptance_likelihood: float  # 0-1
    rationale: str


@dataclass
class RefundReport:
    """
    Complete refund strategy recommendation for one dispute.
    """
    dispute_id: str
    claim_type: str
    dispute_amount: float
    p_win: float

    # Recommended strategy
    recommended_strategy: RefundStrategy
    recommended_refund_amount: float
    recommended_refund_pct: float    # 0-1

    # Scenarios evaluated
    scenarios: list[RefundScenario]

    # Key thresholds
    ev_fight: float
    min_profitable_refund: float     # Refund amount below which fighting is better
    max_acceptable_refund: float     # Same as walk-away from settlement analysis

    # Decision factors
    triggers: list[RefundTrigger]
    proactive_advantage: float       # $ benefit of acting now vs after escalation
    hdr_saving_days: int            # Days of HDR count saved by proactive refund

    # Comms
    buyer_message: str              # Template message to buyer
    internal_note: str              # For merchant's records
    summary: str


def _ev_fight(inp: RefundInput) -> float:
    return round(inp.p_win * inp.dispute_amount - inp.fight_cost, 2)


def _hdr_days(inp: RefundInput, strategy: RefundStrategy) -> int:
    """Estimate days until dispute resolves under each strategy."""
    if strategy == RefundStrategy.FULL_PROACTIVE:
        return 1 if not inp.is_formal_dispute else 3
    if strategy == RefundStrategy.PARTIAL_PROACTIVE:
        return 3 if not inp.is_formal_dispute else 7
    if strategy in (RefundStrategy.FULL_REACTIVE, RefundStrategy.PARTIAL_REACTIVE):
        return 7
    if strategy == RefundStrategy.FIGHT_NO_REFUND:
        return 20   # PayPal review takes 10-20 days
    return 10


def _buyer_message(inp: RefundInput, strategy: RefundStrategy, amount: float) -> str:
    if strategy in (RefundStrategy.FULL_PROACTIVE, RefundStrategy.FULL_REACTIVE):
        return (
            f"I'm sorry your order didn't work out. I've issued a full refund of "
            f"${inp.dispute_amount:.2f} to your original payment method. "
            f"You should see it within 3-5 business days. "
            f"Please close the dispute once you receive the refund."
        )
    if strategy in (RefundStrategy.PARTIAL_PROACTIVE, RefundStrategy.PARTIAL_REACTIVE):
        return (
            f"I'd like to resolve this by offering a partial refund of ${amount:.2f}. "
            f"This reflects the portion of the order that was {'not delivered' if inp.partial_delivery_pct < 1 else 'unsatisfactory'}. "
            f"Please let me know if you'd like to accept this offer within 48 hours."
        )
    return ""


def _evaluate_scenarios(inp: RefundInput, ev: float) -> list[RefundScenario]:
    scenarios: list[RefundScenario] = []
    partial_amt = round(inp.dispute_amount * (1 - inp.partial_delivery_pct), 2)
    partial_amt = max(partial_amt, round(inp.dispute_amount * 0.25, 2))  # Min 25%

    # Full proactive
    cost_full_proactive = inp.dispute_amount
    ev_vs_fight_full_proactive = round(ev - (-cost_full_proactive), 2)
    scenarios.append(RefundScenario(
        strategy=RefundStrategy.FULL_PROACTIVE,
        refund_amount=inp.dispute_amount,
        total_cost=cost_full_proactive,
        ev_vs_fight=ev_vs_fight_full_proactive,
        hdr_resolution_days=_hdr_days(inp, RefundStrategy.FULL_PROACTIVE),
        buyer_acceptance_likelihood=0.98,
        rationale=(
            f"Full refund now closes dispute in ~{_hdr_days(inp, RefundStrategy.FULL_PROACTIVE)} days. "
            f"Costs ${cost_full_proactive:.0f} but avoids fight cost (${inp.fight_cost:.0f}) "
            f"and HDR count stays minimal."
        ),
    ))

    # Full reactive (after formal dispute opens)
    cost_full_reactive = inp.dispute_amount
    scenarios.append(RefundScenario(
        strategy=RefundStrategy.FULL_REACTIVE,
        refund_amount=inp.dispute_amount,
        total_cost=cost_full_reactive,
        ev_vs_fight=round(ev - (-cost_full_reactive), 2),
        hdr_resolution_days=_hdr_days(inp, RefundStrategy.FULL_REACTIVE),
        buyer_acceptance_likelihood=0.95,
        rationale=(
            f"Full refund after formal dispute: costs ${cost_full_reactive:.0f}. "
            f"Slower resolution (~{_hdr_days(inp, RefundStrategy.FULL_REACTIVE)} days) adds to HDR window."
        ),
    ))

    # Partial proactive
    if inp.partial_delivery_pct < 1.0 or inp.claim_type == "SNAD":
        cost_partial_proactive = partial_amt
        scenarios.append(RefundScenario(
            strategy=RefundStrategy.PARTIAL_PROACTIVE,
            refund_amount=partial_amt,
            total_cost=cost_partial_proactive,
            ev_vs_fight=round(ev - (-cost_partial_proactive), 2),
            hdr_resolution_days=_hdr_days(inp, RefundStrategy.PARTIAL_PROACTIVE),
            buyer_acceptance_likelihood=0.65,
            rationale=(
                f"Partial refund (${partial_amt:.0f}) for undelivered/unsatisfactory portion. "
                f"Buyer may need 1-2 messages to agree. Saves ${inp.dispute_amount - partial_amt:.0f}."
            ),
        ))

    # Fight
    cost_fight = round(
        (1 - inp.p_win) * (inp.dispute_amount + inp.fight_cost) + inp.p_win * inp.fight_cost, 2
    )
    scenarios.append(RefundScenario(
        strategy=RefundStrategy.FIGHT_NO_REFUND,
        refund_amount=0.0,
        total_cost=cost_fight,
        ev_vs_fight=0.0,  # Baseline
        hdr_resolution_days=_hdr_days(inp, RefundStrategy.FIGHT_NO_REFUND),
        buyer_acceptance_likelihood=0.0,
        rationale=(
            f"Fight: P(win)={inp.p_win:.0%}. "
            f"Expected cost ${cost_fight:.0f} "
            f"(fight_cost=${inp.fight_cost:.0f} + {(1-inp.p_win):.0%} × amount). "
            f"Dispute open ~{_hdr_days(inp, RefundStrategy.FIGHT_NO_REFUND)} days."
        ),
    ))

    return scenarios


def _pick_strategy(inp: RefundInput, ev: float) -> tuple[RefundStrategy, float, list[RefundTrigger]]:
    triggers: list[RefundTrigger] = []
    partial_amt = round(inp.dispute_amount * max(0.25, 1 - inp.partial_delivery_pct), 2)

    # Tiny amount — always refund
    if inp.dispute_amount < 15.0:
        triggers.append(RefundTrigger.TINY_AMOUNT)
        return RefundStrategy.FULL_PROACTIVE, inp.dispute_amount, triggers

    # Repeat fraudster — fight, don't reward
    if inp.buyer_dispute_count_90d >= 3:
        triggers.append(RefundTrigger.STRONG_CASE)
        return RefundStrategy.FIGHT_NO_REFUND, 0.0, triggers

    # Very near deadline — full proactive to avoid deadline miss
    if inp.days_until_deadline <= 2:
        triggers.append(RefundTrigger.NEAR_DEADLINE)
        return RefundStrategy.FULL_PROACTIVE, inp.dispute_amount, triggers

    # Strong case — fight
    if inp.p_win >= 0.72 and ev >= 50.0 and inp.was_delivered:
        triggers.append(RefundTrigger.STRONG_CASE)
        return RefundStrategy.FIGHT_NO_REFUND, 0.0, triggers

    # HDR pressure and weak case — full proactive
    if inp.hdr_pressure_level >= 2 and ev < 0.0:
        triggers.append(RefundTrigger.HDR_PRESSURE)
        triggers.append(RefundTrigger.LOW_WIN_PROBABILITY)
        return RefundStrategy.FULL_PROACTIVE, inp.dispute_amount, triggers

    # Partial delivery — offer partial refund
    if inp.partial_delivery_pct < 0.95 and inp.claim_type in ("INR", "SNAD"):
        triggers.append(RefundTrigger.PARTIAL_DELIVERY)
        strategy = RefundStrategy.PARTIAL_PROACTIVE if not inp.is_formal_dispute else RefundStrategy.PARTIAL_REACTIVE
        return strategy, partial_amt, triggers

    # Weak case — full refund
    if ev < -15.0:
        triggers.append(RefundTrigger.LOW_WIN_PROBABILITY)
        strategy = RefundStrategy.FULL_PROACTIVE if not inp.is_formal_dispute else RefundStrategy.FULL_REACTIVE
        return strategy, inp.dispute_amount, triggers

    # Long-term customer + moderate case — full proactive to preserve relationship
    if inp.buyer_is_long_term_customer and inp.buyer_purchase_history_count >= 3 and ev < 20.0:
        triggers.append(RefundTrigger.REPEAT_BUYER)
        return RefundStrategy.FULL_PROACTIVE, inp.dispute_amount, triggers

    # Moderate case with high fight cost relative to amount
    if inp.fight_cost > inp.dispute_amount * 0.4:
        triggers.append(RefundTrigger.HIGH_FIGHT_COST)
        strategy = RefundStrategy.FULL_PROACTIVE if not inp.is_formal_dispute else RefundStrategy.FULL_REACTIVE
        return strategy, inp.dispute_amount, triggers

    # Default: fight if EV positive, else refund
    if ev >= 0.0:
        triggers.append(RefundTrigger.STRONG_CASE)
        return RefundStrategy.FIGHT_NO_REFUND, 0.0, triggers

    triggers.append(RefundTrigger.LOW_WIN_PROBABILITY)
    return RefundStrategy.FULL_REACTIVE, inp.dispute_amount, triggers


def evaluate_refund(inp: RefundInput) -> RefundReport:
    """
    Determine optimal refund strategy for one dispute.
    """
    ev = _ev_fight(inp)
    strategy, refund_amount, triggers = _pick_strategy(inp, ev)

    # Proactive advantage: $ saved vs waiting for full formal dispute
    proactive_advantage = round(inp.fight_cost * 0.5, 2)  # Saves half the fight overhead

    # HDR saving: how many fewer days in the HDR window
    hdr_days_proactive = _hdr_days(inp, RefundStrategy.FULL_PROACTIVE)
    hdr_days_reactive = _hdr_days(inp, RefundStrategy.FULL_REACTIVE)
    hdr_saving_days = hdr_days_reactive - hdr_days_proactive

    # Walk-away: settlement equivalent
    walk_away = round(min(inp.dispute_amount, max(0.0, -ev)), 2)

    # Min profitable: at what refund amount is fight EV = refund cost?
    # EV(fight) = -refund → refund = -EV(fight)
    min_profitable = walk_away

    scenarios = _evaluate_scenarios(inp, ev)

    pct = round(refund_amount / inp.dispute_amount, 3) if inp.dispute_amount > 0 else 0.0
    buyer_msg = _buyer_message(inp, strategy, refund_amount)

    internal_note = (
        f"Refund decision for {inp.dispute_id}: {strategy.value} @ ${refund_amount:.2f}. "
        f"P(win)={inp.p_win:.0%}, EV(fight)=${ev:+.0f}, "
        f"HDR level={inp.hdr_pressure_level}, triggers={[t.value for t in triggers]}."
    )

    summary = (
        f"Refund {inp.dispute_id}: {inp.claim_type} ${inp.dispute_amount:.0f} | "
        f"P(win)={inp.p_win:.0%} | EV(fight)=${ev:+.0f} | "
        f"strategy={strategy.value} | amount=${refund_amount:.0f} ({pct:.0%})"
    )

    return RefundReport(
        dispute_id=inp.dispute_id,
        claim_type=inp.claim_type,
        dispute_amount=inp.dispute_amount,
        p_win=inp.p_win,
        recommended_strategy=strategy,
        recommended_refund_amount=refund_amount,
        recommended_refund_pct=pct,
        scenarios=scenarios,
        ev_fight=ev,
        min_profitable_refund=min_profitable,
        max_acceptable_refund=walk_away,
        triggers=triggers,
        proactive_advantage=proactive_advantage,
        hdr_saving_days=hdr_saving_days,
        buyer_message=buyer_msg,
        internal_note=internal_note,
        summary=summary,
    )
