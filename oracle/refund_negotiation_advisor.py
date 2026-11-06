"""
Refund Negotiation Advisor — recommend the optimal negotiation strategy
for a dispute given evidence strength, LTV, win probability, and EV.

For each dispute this oracle computes:
  - Buyer intent signal (legitimate / opportunistic / serial abuser)
  - Optimal counter-offer amount (if partial negotiation is preferred)
  - Negotiation strategy: FIGHT_FULL / COUNTER_OFFER / ACCEPT_PARTIAL /
                          ACCEPT_FULL / ESCALATE_ARBITRATION
  - EV comparison: fight vs. accept vs. counter-offer
  - Negotiation talking points for the response letter

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NegotiationStrategy(str, Enum):
    FIGHT_FULL          = "FIGHT_FULL"           # Contest the full amount
    COUNTER_OFFER       = "COUNTER_OFFER"        # Propose partial refund
    ACCEPT_PARTIAL      = "ACCEPT_PARTIAL"       # Accept buyer's partial offer
    ACCEPT_FULL         = "ACCEPT_FULL"          # Accept full chargeback
    ESCALATE_ARBITRATION = "ESCALATE_ARBITRATION"  # Push to pre-arb/arbitration


class BuyerIntentSignal(str, Enum):
    LEGITIMATE    = "LEGITIMATE"    # First-time, coherent claim, reasonable amount
    OPPORTUNISTIC = "OPPORTUNISTIC" # Pattern of minor disputes, inflated claims
    SERIAL_ABUSER = "SERIAL_ABUSER" # Multiple chargebacks, known bad actor
    UNCLEAR       = "UNCLEAR"


class LtvTier(str, Enum):
    VIP        = "VIP"       # ≥ $500 total lifetime value
    HIGH       = "HIGH"      # $200–499
    MEDIUM     = "MEDIUM"    # $50–199
    LOW        = "LOW"       # $10–49
    NEGLIGIBLE = "NEGLIGIBLE"  # < $10


@dataclass
class NegotiationInput:
    """Input for refund negotiation recommendation."""
    dispute_id: str
    transaction_amount: float
    reason_code: str            # INR, SNAD, UNAUTHORIZED, SUBSCRIPTION, etc.
    network: str

    # Probabilities and scores from other oracles
    win_probability: float = 0.50       # Base win prob from triage/scorer
    evidence_completeness_pct: float = 0.50   # 0–1 from evidence gap oracle
    policy_defensibility_score: int = 50      # 0–100 from policy scorer

    # Buyer signals
    buyer_prior_disputes: int = 0
    buyer_ltv: float = 0.0           # Lifetime total purchase value
    buyer_dispute_rate: float = 0.0  # Disputes / orders ratio
    buyer_made_offer: bool = False   # Buyer proposed a partial refund
    buyer_offer_amount: float = 0.0  # Their proposed refund

    # Cost inputs (defaults follow PayPal standard)
    chargeback_fee: float = 20.0     # Network chargeback processing fee
    fight_cost: float = 15.0         # Operational cost to respond
    pre_arb_fee: float = 0.0         # Pre-arb reversal fee (Visa/MC = $150)
    arbitration_fee: float = 0.0     # Arb fee ($500 Visa/MC, $0 PayPal)

    # Context
    days_until_deadline: int = 10


@dataclass
class NegotiationScenario:
    """EV analysis for one outcome scenario."""
    name: str
    expected_value: float
    win_probability: float
    cost: float
    rationale: str


@dataclass
class NegotiationReport:
    """Full negotiation recommendation."""
    dispute_id: str
    transaction_amount: float

    buyer_intent: BuyerIntentSignal
    ltv_tier: LtvTier

    recommended_strategy: NegotiationStrategy
    counter_offer_amount: float          # 0 if not COUNTER_OFFER
    counter_offer_pct: float             # 0–1

    scenarios: list[NegotiationScenario]  # EV for fight / counter / accept
    best_ev_scenario: str

    talking_points: list[str]            # For response letter
    risk_factors: list[str]

    urgency: str                         # CRITICAL / HIGH / MODERATE
    summary: str


def _classify_ltv(ltv: float) -> LtvTier:
    if ltv >= 500:
        return LtvTier.VIP
    if ltv >= 200:
        return LtvTier.HIGH
    if ltv >= 50:
        return LtvTier.MEDIUM
    if ltv >= 10:
        return LtvTier.LOW
    return LtvTier.NEGLIGIBLE


def _classify_buyer_intent(inp: NegotiationInput) -> BuyerIntentSignal:
    if inp.buyer_prior_disputes >= 5 or inp.buyer_dispute_rate >= 0.20:
        return BuyerIntentSignal.SERIAL_ABUSER
    if inp.buyer_prior_disputes >= 2 or inp.buyer_dispute_rate >= 0.10:
        return BuyerIntentSignal.OPPORTUNISTIC
    if inp.buyer_prior_disputes == 0 and inp.buyer_dispute_rate < 0.05:
        return BuyerIntentSignal.LEGITIMATE
    return BuyerIntentSignal.UNCLEAR


def _ev_fight(inp: NegotiationInput, win_prob: float) -> float:
    return win_prob * inp.transaction_amount - inp.fight_cost - (1 - win_prob) * inp.chargeback_fee


def _ev_accept(inp: NegotiationInput) -> float:
    return -inp.transaction_amount - inp.chargeback_fee


def _ev_counter(inp: NegotiationInput, refund_pct: float, win_prob: float) -> float:
    """EV of offering a partial refund (refund_pct × amount)."""
    refund = inp.transaction_amount * refund_pct
    # If accepted: merchant pays refund + operational cost but avoids chargeback fee
    # We model 50% chance buyer accepts the counter, rest goes to fight
    accept_ev = -(refund + inp.fight_cost * 0.5)
    fight_ev  = _ev_fight(inp, win_prob) * 0.5
    return accept_ev + fight_ev


def advise_negotiation(inp: NegotiationInput) -> NegotiationReport:
    """Recommend negotiation strategy for a dispute."""
    buyer_intent = _classify_buyer_intent(inp)
    ltv_tier     = _classify_ltv(inp.buyer_ltv)

    # Adjust win probability by evidence and intent
    adj_win = inp.win_probability
    adj_win += (inp.evidence_completeness_pct - 0.50) * 0.20
    if buyer_intent == BuyerIntentSignal.SERIAL_ABUSER:
        adj_win += 0.05   # Networks take serial abuse seriously
    adj_win = max(0.0, min(1.0, adj_win))

    # Determine optimal counter-offer percentage
    # Heuristic: offer refund = (1 - win_prob) × amount, floored at 10%, capped at 70%
    raw_counter_pct = max(0.10, min(0.70, 1.0 - adj_win))

    # Compute EV for each scenario
    ev_fight   = _ev_fight(inp, adj_win)
    ev_accept  = _ev_accept(inp)
    ev_counter = _ev_counter(inp, raw_counter_pct, adj_win)

    scenarios = [
        NegotiationScenario(
            name="FIGHT_FULL",
            expected_value=round(ev_fight, 2),
            win_probability=adj_win,
            cost=inp.fight_cost,
            rationale=f"Win prob {adj_win:.0%}: EV = {adj_win:.2f}×${inp.transaction_amount:.0f} − ${inp.fight_cost:.0f}",
        ),
        NegotiationScenario(
            name="COUNTER_OFFER",
            expected_value=round(ev_counter, 2),
            win_probability=adj_win,
            cost=inp.fight_cost * 0.5,
            rationale=f"Offer {raw_counter_pct:.0%} refund (${inp.transaction_amount * raw_counter_pct:.0f}); blended EV",
        ),
        NegotiationScenario(
            name="ACCEPT_FULL",
            expected_value=round(ev_accept, 2),
            win_probability=0.0,
            cost=0.0,
            rationale=f"Pay full ${inp.transaction_amount:.0f} + ${inp.chargeback_fee:.0f} fee = ${-ev_accept:.0f} total loss",
        ),
    ]

    best = max(scenarios, key=lambda s: s.expected_value)

    # Choose strategy
    talking_points: list[str] = []
    risk_factors: list[str] = []

    if ltv_tier == LtvTier.VIP and buyer_intent != BuyerIntentSignal.SERIAL_ABUSER:
        # Prioritize relationship retention for VIP
        strategy = NegotiationStrategy.COUNTER_OFFER
        talking_points.append("VIP customer — propose goodwill partial refund to preserve relationship")
        risk_factors.append("Accepting too readily sets precedent for future disputes")
    elif inp.transaction_amount <= 10.0 or ev_fight < -15.0:
        strategy = NegotiationStrategy.ACCEPT_FULL
        talking_points.append("Amount too small to justify dispute cost — accept immediately")
    elif adj_win >= 0.70 and inp.evidence_completeness_pct >= 0.70:
        strategy = NegotiationStrategy.FIGHT_FULL
        talking_points.append(f"Strong evidence ({inp.evidence_completeness_pct:.0%} complete) supports full contest")
        talking_points.append("Lead with authentication records and delivery confirmation")
    elif best.name == "COUNTER_OFFER" and buyer_intent != BuyerIntentSignal.SERIAL_ABUSER:
        strategy = NegotiationStrategy.COUNTER_OFFER
        talking_points.append(f"Offer {raw_counter_pct:.0%} partial refund to avoid chargeback fee")
    elif buyer_intent == BuyerIntentSignal.SERIAL_ABUSER:
        strategy = NegotiationStrategy.FIGHT_FULL
        talking_points.append("Serial disputer — do not reward with partial refund; fight on principle")
        risk_factors.append("Flag buyer account for fraud review")
    elif inp.arbitration_fee == 0 and adj_win >= 0.55 and inp.transaction_amount >= 100:
        strategy = NegotiationStrategy.ESCALATE_ARBITRATION
        talking_points.append("No arbitration fee — escalate to press full recovery")
    else:
        strategy = NegotiationStrategy.COUNTER_OFFER if ev_counter > ev_accept else NegotiationStrategy.ACCEPT_FULL

    # Additional talking points by reason code
    if inp.reason_code == "INR":
        talking_points.append("Provide carrier-confirmed delivery scan and timestamp")
    elif inp.reason_code == "UNAUTHORIZED":
        talking_points.append("Cite 3DS authentication result and device match data")
    elif inp.reason_code == "SNAD":
        talking_points.append("Include original listing photos vs. delivered item documentation")
    elif inp.reason_code == "SUBSCRIPTION":
        talking_points.append("Reference signed terms-of-service with renewal notice timestamps")

    # Buyer offer assessment
    counter_amount = inp.transaction_amount * raw_counter_pct
    if inp.buyer_made_offer:
        if inp.buyer_offer_amount >= counter_amount:
            strategy = NegotiationStrategy.ACCEPT_PARTIAL
            talking_points.append(f"Buyer offer ${inp.buyer_offer_amount:.0f} ≥ recommended counter — accept it")
        else:
            talking_points.append(
                f"Buyer offer ${inp.buyer_offer_amount:.0f} < recommended ${counter_amount:.0f} — counter-propose"
            )

    # Risk factors
    if inp.days_until_deadline <= 2:
        risk_factors.append("CRITICAL: ≤2 days until deadline — act today")
    if adj_win < 0.35:
        risk_factors.append("Low win probability — weight acceptance heavily")
    if inp.policy_defensibility_score < 40:
        risk_factors.append("Weak policy documentation may undermine fight")

    urgency = "CRITICAL" if inp.days_until_deadline <= 2 else (
        "HIGH" if inp.days_until_deadline <= 5 else "MODERATE"
    )

    summary = (
        f"Negotiation {inp.dispute_id}: "
        f"strategy={strategy.value} | "
        f"ev_fight=${ev_fight:.0f} ev_accept=${ev_accept:.0f} | "
        f"buyer_intent={buyer_intent.value} ltv={ltv_tier.value}"
    )

    return NegotiationReport(
        dispute_id=inp.dispute_id,
        transaction_amount=inp.transaction_amount,
        buyer_intent=buyer_intent,
        ltv_tier=ltv_tier,
        recommended_strategy=strategy,
        counter_offer_amount=round(counter_amount, 2),
        counter_offer_pct=round(raw_counter_pct, 4),
        scenarios=scenarios,
        best_ev_scenario=best.name,
        talking_points=talking_points,
        risk_factors=risk_factors,
        urgency=urgency,
        summary=summary,
    )
