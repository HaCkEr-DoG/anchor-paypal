"""
Settlement Offer Optimizer — compute the optimal proactive settlement amount
to offer a buyer given the full economic picture.

Settlement strategy trade-off:
  - Offer too little: buyer rejects, escalates, merchant loses more
  - Offer too much: unnecessary payout; buyer wins more than they should
  - Optimal: amount where EV(settle) ≥ EV(fight) and buyer likely accepts

Economic model:
  EV(fight)     = P(win) × 0 + P(lose) × (amount + fees) + fight_cost
  EV(settle)    = offer_amount + settlement_cost (processing)
  Optimal offer = amount where EV(settle) = EV(fight), adjusted for
                  buyer acceptance probability

Buyer acceptance model:
  Acceptance probability increases with offer as a fraction of claimed amount:
  - 25% offer → ~40% acceptance
  - 50% offer → ~65% acceptance
  - 75% offer → ~85% acceptance
  - 100% offer → ~95% acceptance

This module also supports iterative settlement strategy:
  - Round 1 anchor: conservative offer
  - Round 2 (if rejected): improved offer
  - Final limit: maximum economic offer

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math


class SettlementStrategy(str, Enum):
    FULL_REFUND       = "FULL_REFUND"       # 100% — capitulate immediately
    GENEROUS          = "GENEROUS"          # 75–99% — defuse hostile buyer
    BALANCED          = "BALANCED"          # 50–74% — split the difference
    CONSERVATIVE      = "CONSERVATIVE"      # 25–49% — test buyer flexibility
    DECLINE           = "DECLINE"           # < 25% — fight is preferred
    STORE_CREDIT      = "STORE_CREDIT"      # Offer as credit (lower cash cost)


class SettlementRound(str, Enum):
    ANCHOR   = "ANCHOR"    # First offer (usually conservative)
    IMPROVED = "IMPROVED"  # Second offer after rejection
    FINAL    = "FINAL"     # Last offer before fighting


@dataclass
class SettlementOfferInput:
    """Input for settlement offer optimization."""
    dispute_id: str
    claim_amount: float              # Disputed transaction amount
    claim_type: str                  # INR / SNAD / UNAUTHORIZED / etc.

    # Economic inputs
    p_win_if_fight: float = 0.55     # Win probability (from EV model)
    fight_cost: float = 25.0         # Internal cost of fighting the dispute
    chargeback_fee: float = 20.0     # Fee if merchant loses at network level
    sp_covers_chargeback: bool = False

    # Escalation inputs
    escalation_probability: float = 0.15  # Probability buyer escalates if unsettled
    buyer_fraud_score: float = 20.0        # 0–100 (high = more fraud signals)

    # Buyer behavior inputs
    buyer_tone_hostile: bool = False
    buyer_threatened_chargeback: bool = False
    buyer_ltv: float = 0.0           # Customer lifetime value (retention incentive)
    is_vip_customer: bool = False

    # Merchant constraints
    max_acceptable_pct: float = 1.0  # Maximum fraction merchant will offer
    hdr_rate_pct: float = 0.30       # Current HDR rate (escalation affects this)

    # Negotiation state
    round_number: int = 1            # 1=anchor, 2=improved, 3=final
    previous_offer_pct: float = 0.0  # Prior offer as fraction (0 if none)
    buyer_rejected_previous: bool = False


@dataclass
class SettlementOffer:
    """One settlement offer option."""
    round: SettlementRound
    offer_amount: float
    offer_pct: float            # Fraction of claimed amount
    strategy: SettlementStrategy
    acceptance_probability: float
    ev_settle: float            # Expected value of this offer
    ev_fight: float             # Expected value of fighting
    ev_improvement: float       # ev_settle - ev_fight (positive = settle better)
    rationale: str


@dataclass
class SettlementOptimizationReport:
    """Full settlement strategy recommendation."""
    dispute_id: str

    # Core economics
    ev_fight: float
    ev_full_refund: float
    breakeven_offer_pct: float    # Minimum offer where settling beats fighting

    # Recommended offer
    primary_offer: SettlementOffer
    fallback_offer: Optional[SettlementOffer]

    # LTV consideration
    ltv_adjusted_offer_pct: float  # Offer after considering LTV retention
    ltv_premium: float             # Additional amount justified by LTV

    # Fraud adjustment
    fraud_adjusted_offer_pct: float  # Offer after considering fraud signals
    fraud_discount: float            # Reduction due to fraud signals

    # Guidance
    recommended_strategy: SettlementStrategy
    negotiation_notes: list[str]
    accept_first_offer_recommended: bool  # True if single-round likely best

    summary: str


def _acceptance_probability(offer_pct: float) -> float:
    """
    Sigmoid-based buyer acceptance probability given offer as fraction of claim.
    Calibrated to typical PayPal dispute settlement data.
    """
    # Logistic curve: p50 at ~50%, steep at extremes
    x = offer_pct * 10 - 5  # Map [0,1] → [-5, 5]
    return round(1 / (1 + math.exp(-x * 0.6)), 4)


def _ev_settle(offer_pct: float, claim_amount: float,
               processing_cost: float = 1.0) -> float:
    """EV of settling: just the offer amount plus any administrative cost."""
    return round(offer_pct * claim_amount + processing_cost, 2)


def _ev_fight(inp: SettlementOfferInput) -> float:
    """
    EV of fighting, incorporating escalation risk.

    EV(fight_no_escalate) = p_win * 0 + (1-p_win) * (amount + fees) + fight_cost
    EV(fight_escalate)    = EV(fight_no_escalate) + escalation_fee
    EV(fight) = (1 - escalation_p) * EV(fight_no_escalate)
              + escalation_p * EV(fight_escalate)
    """
    p_win = inp.p_win_if_fight
    amount = inp.claim_amount
    cb_fee = 0.0 if inp.sp_covers_chargeback else inp.chargeback_fee
    escalation_extra = 20.0  # Additional network-level fee if escalated

    ev_no_esc = (1 - p_win) * (amount + cb_fee) + inp.fight_cost
    ev_esc    = ev_no_esc + escalation_extra

    ev = ((1 - inp.escalation_probability) * ev_no_esc
          + inp.escalation_probability * ev_esc)
    return round(ev, 2)


def _build_offer(
        inp: SettlementOfferInput,
        offer_pct: float,
        round_enum: SettlementRound,
        ev_fight: float) -> SettlementOffer:
    offer_pct = round(max(0.0, min(1.0, offer_pct)), 4)
    offer_amount = round(offer_pct * inp.claim_amount, 2)
    accept_p = _acceptance_probability(offer_pct)
    ev_s = _ev_settle(offer_pct, inp.claim_amount)
    ev_improvement = round(ev_fight - ev_s, 2)  # Positive = settling is cheaper

    if offer_pct >= 0.99:
        strategy = SettlementStrategy.FULL_REFUND
    elif offer_pct >= 0.75:
        strategy = SettlementStrategy.GENEROUS
    elif offer_pct >= 0.50:
        strategy = SettlementStrategy.BALANCED
    elif offer_pct >= 0.25:
        strategy = SettlementStrategy.CONSERVATIVE
    else:
        strategy = SettlementStrategy.DECLINE

    rationale = (
        f"Offer ${offer_amount:.2f} ({offer_pct:.0%} of claim) | "
        f"accept_p={accept_p:.0%} | EV_settle=${ev_s:.2f} vs EV_fight=${ev_fight:.2f}"
    )
    return SettlementOffer(
        round=round_enum,
        offer_amount=offer_amount,
        offer_pct=offer_pct,
        strategy=strategy,
        acceptance_probability=accept_p,
        ev_settle=ev_s,
        ev_fight=ev_fight,
        ev_improvement=ev_improvement,
        rationale=rationale,
    )


def optimize_settlement(inp: SettlementOfferInput) -> SettlementOptimizationReport:
    """
    Compute the optimal settlement offer for a PayPal dispute.
    """
    ev_fight = _ev_fight(inp)
    ev_full  = _ev_settle(1.0, inp.claim_amount)

    # Breakeven: find offer_pct where EV(settle) = EV(fight)
    # ev_settle = offer_pct * claim_amount + 1 = ev_fight
    # offer_pct = (ev_fight - 1) / claim_amount
    breakeven_pct = round(max(0.0, (ev_fight - 1.0) / inp.claim_amount), 4)
    breakeven_pct = min(breakeven_pct, 1.0)

    # Base offer: start slightly above breakeven (anchor at 60% of breakeven)
    # but at least 10% above breakeven to have chance of acceptance
    if breakeven_pct >= 1.0:
        # Fighting always worse than full refund
        base_pct = 1.0
    elif breakeven_pct <= 0.0:
        # Fighting clearly preferred — offer nothing or very small
        base_pct = 0.10
    else:
        base_pct = min(breakeven_pct + 0.15, 0.80)

    # LTV adjustment: high-value customers deserve more generous offers
    ltv_premium_pct = 0.0
    if inp.buyer_ltv > 500 or inp.is_vip_customer:
        ltv_premium_pct = 0.15
    elif inp.buyer_ltv > 200:
        ltv_premium_pct = 0.08
    ltv_adjusted_pct = round(min(1.0, base_pct + ltv_premium_pct), 4)

    # Fraud adjustment: high fraud score → lower offer (they're likely lying)
    fraud_discount_pct = 0.0
    if inp.buyer_fraud_score >= 70:
        fraud_discount_pct = 0.20
    elif inp.buyer_fraud_score >= 40:
        fraud_discount_pct = 0.10
    final_pct = round(max(0.0, ltv_adjusted_pct - fraud_discount_pct), 4)

    # Hostile buyer → more generous (defuse escalation risk)
    if inp.buyer_tone_hostile or inp.buyer_threatened_chargeback:
        final_pct = min(1.0, final_pct + 0.10)

    # Cap at merchant's stated maximum
    final_pct = min(final_pct, inp.max_acceptable_pct)

    # Round 2 improvement if prior offer was rejected
    if inp.buyer_rejected_previous and inp.round_number == 2:
        improved_pct = min(inp.max_acceptable_pct,
                           max(final_pct, inp.previous_offer_pct + 0.15))
    else:
        improved_pct = min(inp.max_acceptable_pct, final_pct + 0.20)

    round_map = {1: SettlementRound.ANCHOR, 2: SettlementRound.IMPROVED,
                 3: SettlementRound.FINAL}
    current_round = round_map.get(inp.round_number, SettlementRound.ANCHOR)

    primary = _build_offer(inp, final_pct, current_round, ev_fight)
    fallback_pct = min(inp.max_acceptable_pct, improved_pct)
    fallback = _build_offer(inp, fallback_pct, SettlementRound.IMPROVED, ev_fight)

    # Determine recommended strategy
    if final_pct >= 0.99:
        rec_strategy = SettlementStrategy.FULL_REFUND
    elif final_pct >= 0.75:
        rec_strategy = SettlementStrategy.GENEROUS
    elif final_pct >= 0.50:
        rec_strategy = SettlementStrategy.BALANCED
    elif final_pct >= 0.25:
        rec_strategy = SettlementStrategy.CONSERVATIVE
    else:
        rec_strategy = SettlementStrategy.DECLINE

    # Negotiation notes
    notes: list[str] = []
    if breakeven_pct >= 0.90:
        notes.append("Fighting is economically poor — full refund may be best outcome")
    if inp.buyer_fraud_score >= 40:
        notes.append(f"Fraud signals detected (score={inp.buyer_fraud_score:.0f}) — lower offer justified")
    if inp.escalation_probability >= 0.50:
        notes.append("High escalation risk — prioritize settlement to avoid chargeback fee")
    if inp.buyer_ltv > 200:
        notes.append(f"High-LTV buyer (${inp.buyer_ltv:.0f}) — retention value warrants generous offer")
    if inp.is_vip_customer:
        notes.append("VIP customer — consider full refund to preserve relationship")
    if not notes:
        notes.append("Standard settlement economics — offer anchors near breakeven")

    accept_first = (primary.acceptance_probability >= 0.70
                    or inp.is_vip_customer
                    or inp.escalation_probability >= 0.60)

    summary = (
        f"Settlement {inp.dispute_id}: "
        f"ev_fight=${ev_fight:.2f} | breakeven={breakeven_pct:.0%} | "
        f"offer={final_pct:.0%} (${primary.offer_amount:.2f}) | "
        f"accept_p={primary.acceptance_probability:.0%} | "
        f"strategy={rec_strategy.value}"
    )

    return SettlementOptimizationReport(
        dispute_id=inp.dispute_id,
        ev_fight=ev_fight,
        ev_full_refund=ev_full,
        breakeven_offer_pct=breakeven_pct,
        primary_offer=primary,
        fallback_offer=fallback,
        ltv_adjusted_offer_pct=ltv_adjusted_pct,
        ltv_premium=round(ltv_premium_pct * inp.claim_amount, 2),
        fraud_adjusted_offer_pct=final_pct,
        fraud_discount=round(fraud_discount_pct * inp.claim_amount, 2),
        recommended_strategy=rec_strategy,
        negotiation_notes=notes,
        accept_first_offer_recommended=accept_first,
        summary=summary,
    )
