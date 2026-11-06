"""
Settlement Offer Evaluator — assess whether an incoming settlement offer
from a buyer or payment network is worth accepting, countering, or rejecting.

Given the original dispute amount, evidence strength, win probability,
and the offer on the table, this oracle computes:
  - Net value comparison: EV(fight) vs. EV(accept offer)
  - Offer fairness score 0–100
  - Recommended response (ACCEPT / COUNTER / REJECT / ESCALATE)
  - Counter-offer amount if recommending a counter

Settlement contexts:
  BUYER_OFFER      — Buyer directly offers to settle for a partial refund
  MEDIATION_OFFER  — Payment network mediator proposes a split
  INTERNAL_OFFER   — Merchant's own pre-emptive offer to close the dispute

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SettlementContext(str, Enum):
    BUYER_OFFER      = "BUYER_OFFER"
    MEDIATION_OFFER  = "MEDIATION_OFFER"
    INTERNAL_OFFER   = "INTERNAL_OFFER"


class SettlementRecommendation(str, Enum):
    ACCEPT     = "ACCEPT"      # Take the offer — better than fighting
    COUNTER    = "COUNTER"     # Reject this, offer a different amount
    REJECT     = "REJECT"      # Fight — EV of fighting exceeds this offer
    ESCALATE   = "ESCALATE"    # Too complex; needs human review


class OfferFairnessTier(str, Enum):
    VERY_FAIR    = "VERY_FAIR"    # Score 80–100 — buyer is being generous
    FAIR         = "FAIR"         # Score 60–79  — reasonable split
    BELOW_FAIR   = "BELOW_FAIR"   # Score 40–59  — lowball; counter likely
    UNFAIR       = "UNFAIR"       # Score 20–39  — reject or counter
    INSULTING    = "INSULTING"    # Score 0–19   — reject immediately


@dataclass
class SettlementOfferInput:
    """Input for settlement offer evaluation."""
    dispute_id: str
    context: str                     # SettlementContext value

    # Original dispute
    original_amount: float           # Full disputed transaction amount
    chargeback_fee: float = 20.0     # Network chargeback fee if we lose
    response_cost: float = 15.0      # Internal cost to file response

    # The offer
    offer_amount: float = 0.0        # Amount buyer/network proposes to refund
    # Offer = 0 means buyer is asking for FULL refund (worst for merchant)
    # Offer = original_amount means merchant pays nothing (best)

    # Fight scenario parameters
    win_probability: float = 0.50    # Probability merchant wins if they fight
    evidence_strength: float = 0.50  # 0–1 evidence quality

    # LTV / relationship
    ltv_tier: str = "MEDIUM"         # VIP/HIGH/MEDIUM/LOW/NEGLIGIBLE
    buyer_dispute_history: int = 0   # Prior disputes from this buyer

    # Escalation context
    is_in_pre_arb: bool = False
    pre_arb_fee: float = 0.0
    is_in_arbitration: bool = False
    arbitration_fee: float = 0.0


@dataclass
class SettlementOfferReport:
    """Full settlement evaluation."""
    dispute_id: str
    context: str
    offer_amount: float
    original_amount: float

    # Fairness
    fairness_score: int           # 0–100
    fairness_tier: OfferFairnessTier
    merchant_liability_pct: float # What % of amount merchant pays under offer

    # EV comparison
    ev_fight: float
    ev_accept_offer: float
    ev_delta: float               # ev_fight - ev_accept_offer; positive = fight is better

    # Recommendation
    recommendation: SettlementRecommendation
    counter_offer_amount: Optional[float]      # Set only when recommending COUNTER
    counter_offer_rationale: str

    # Context
    accept_saves: float           # How much accepting saves vs. worst case (full loss)
    ltv_adjustment: str           # Narrative on LTV consideration
    summary: str


def _fairness_score(offer_amount: float, original_amount: float, win_prob: float) -> int:
    """Score how fair the offer is from the merchant's perspective."""
    if original_amount <= 0:
        return 50
    # Offer is what the buyer pays BACK (or equivalently, what merchant DOESN'T refund)
    # Higher offer_amount = more favorable to merchant
    pct_recovered = offer_amount / original_amount   # 0=full refund, 1=no refund
    # Adjust by win probability: a 50% pct_recovered is fair at ~50% win prob
    # Fairness = how offer compares to expected recovery
    expected_recovery = win_prob                     # What we'd expect from fighting
    if expected_recovery <= 0:
        expected_recovery = 0.01

    # Ratio of offer recovery to expected recovery
    ratio = pct_recovered / expected_recovery
    if ratio >= 1.5:
        return 90
    elif ratio >= 1.2:
        return 80
    elif ratio >= 0.9:
        return 65
    elif ratio >= 0.7:
        return 50
    elif ratio >= 0.5:
        return 35
    elif ratio >= 0.3:
        return 20
    else:
        return 8


def _ev_fight(inp: SettlementOfferInput) -> float:
    """EV of fighting the dispute from current stage."""
    amount = inp.original_amount
    p = inp.win_probability
    rc = inp.response_cost
    cb = inp.chargeback_fee

    if inp.is_in_arbitration:
        af = inp.arbitration_fee
        paf = inp.pre_arb_fee
        ev = (p * (amount - rc - paf - af)
              - (1 - p) * (amount + cb + rc + paf + af))
    elif inp.is_in_pre_arb:
        paf = inp.pre_arb_fee
        ev = (p * (amount - rc - paf)
              - (1 - p) * (amount + cb + rc + paf))
    else:
        ev = (p * (amount - rc)
              - (1 - p) * (amount + cb + rc))

    return round(ev, 2)


def _ev_accept(offer_amount: float, original_amount: float, response_cost: float) -> float:
    """EV of accepting the settlement offer."""
    # Net = amount recovered under offer minus any response cost already sunk
    # Merchant recovers: offer_amount (portion not refunded)
    # Merchant pays back: (original_amount - offer_amount)
    # Net position = -(original_amount - offer_amount) = offer_amount - original_amount
    return round(offer_amount - original_amount, 2)


def evaluate_settlement_offer(inp: SettlementOfferInput) -> SettlementOfferReport:
    """Evaluate whether to accept, counter, or reject a settlement offer."""
    amount = inp.original_amount
    offer  = inp.offer_amount

    # Clamp offer to [0, amount]
    offer = max(0.0, min(amount, offer))

    # Fairness
    fs = _fairness_score(offer, amount, inp.win_probability)
    if fs >= 80:
        ft = OfferFairnessTier.VERY_FAIR
    elif fs >= 60:
        ft = OfferFairnessTier.FAIR
    elif fs >= 40:
        ft = OfferFairnessTier.BELOW_FAIR
    elif fs >= 20:
        ft = OfferFairnessTier.UNFAIR
    else:
        ft = OfferFairnessTier.INSULTING

    merchant_liability_pct = round((amount - offer) / max(amount, 0.01), 4)

    ev_f = _ev_fight(inp)
    ev_a = _ev_accept(offer, amount, inp.response_cost)
    delta = round(ev_f - ev_a, 2)

    # ── Recommendation logic ──────────────────────────────────────────────────
    counter: Optional[float] = None
    counter_rationale = ""

    # LTV safety: be more flexible with VIP buyers
    ltv_flexibility = {
        "VIP":        0.10,   # Accept 10% lower than fair offer for VIP
        "HIGH":       0.05,
        "MEDIUM":     0.0,
        "LOW":       -0.05,   # Require 5% higher for low-LTV
        "NEGLIGIBLE":-0.10,
    }
    ltv_adj = ltv_flexibility.get(inp.ltv_tier.upper(), 0.0)
    ltv_adj_text = ""
    if ltv_adj > 0:
        ltv_adj_text = f"VIP/HIGH-LTV buyer — accept threshold lowered by {ltv_adj:.0%}"
    elif ltv_adj < 0:
        ltv_adj_text = f"LOW/NEGLIGIBLE LTV buyer — accept threshold raised by {abs(ltv_adj):.0%}"
    else:
        ltv_adj_text = "MEDIUM LTV — standard thresholds"

    # Serial disputer: never accept insulting offers
    serial_flag = inp.buyer_dispute_history >= 5

    if inp.is_in_arbitration and inp.arbitration_fee >= amount * 0.30:
        # Arb fee would exceed 30% of amount — accept unless offer is insulting
        if ft != OfferFairnessTier.INSULTING:
            rec = SettlementRecommendation.ACCEPT
        else:
            rec = SettlementRecommendation.COUNTER
            counter = round(amount * max(0.30, inp.win_probability - ltv_adj), 2)
            counter_rationale = "Arb fees too high; counter at estimated win value"
    elif delta > 0 and not serial_flag:
        # Fighting is better EV — reject unless offer is very fair
        if ft in (OfferFairnessTier.VERY_FAIR,):
            rec = SettlementRecommendation.ACCEPT
        elif ft == OfferFairnessTier.FAIR and inp.ltv_tier in ("VIP", "HIGH"):
            rec = SettlementRecommendation.ACCEPT
        elif ft in (OfferFairnessTier.INSULTING, OfferFairnessTier.UNFAIR):
            rec = SettlementRecommendation.REJECT
        else:
            rec = SettlementRecommendation.COUNTER
            counter = round(amount * max(0.20, inp.win_probability + ltv_adj), 2)
            counter_rationale = f"Fight EV ${ev_f:.0f} > accept EV ${ev_a:.0f}; counter at {inp.win_probability:.0%} of amount"
    elif serial_flag and ft != OfferFairnessTier.VERY_FAIR:
        # Serial abuser — always fight unless offer is very generous
        rec = SettlementRecommendation.REJECT
    else:
        # EV of accepting is better
        if ft in (OfferFairnessTier.VERY_FAIR, OfferFairnessTier.FAIR):
            rec = SettlementRecommendation.ACCEPT
        elif ft == OfferFairnessTier.BELOW_FAIR and inp.ltv_tier in ("VIP", "HIGH"):
            rec = SettlementRecommendation.ACCEPT
        elif ft == OfferFairnessTier.BELOW_FAIR:
            rec = SettlementRecommendation.COUNTER
            counter = round(amount * max(0.15, inp.win_probability - 0.10 + ltv_adj), 2)
            counter_rationale = "Below-fair offer; counter closer to expected win value"
        else:
            rec = SettlementRecommendation.REJECT

    # Complexity override
    if inp.is_in_arbitration and inp.arbitration_fee >= amount * 0.50:
        rec = SettlementRecommendation.ESCALATE
        counter = None
        counter_rationale = "Arbitration fee exceeds 50% of dispute amount — human review required"

    accept_saves = round((amount + inp.chargeback_fee) - (amount - offer), 2)

    summary = (
        f"Settlement {inp.dispute_id}: "
        f"offer=${offer:.0f}/{amount:.0f} ({merchant_liability_pct:.0%} liability) | "
        f"fairness={fs}/100 ({ft.value}) | "
        f"ev_fight=${ev_f:.0f} ev_accept=${ev_a:.0f} | "
        f"rec={rec.value}"
    )

    return SettlementOfferReport(
        dispute_id=inp.dispute_id,
        context=inp.context,
        offer_amount=offer,
        original_amount=amount,
        fairness_score=fs,
        fairness_tier=ft,
        merchant_liability_pct=merchant_liability_pct,
        ev_fight=ev_f,
        ev_accept_offer=ev_a,
        ev_delta=delta,
        recommendation=rec,
        counter_offer_amount=counter,
        counter_offer_rationale=counter_rationale,
        accept_saves=accept_saves,
        ltv_adjustment=ltv_adj_text,
        summary=summary,
    )
