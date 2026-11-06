"""
Dispute Outcome Predictor — synthesize all available oracle signals into
a final prediction and actionable recommendation for a single dispute.

This is the top-level orchestration oracle — it aggregates:
  - Win rate estimate
  - EV calculation (fight vs. accept)
  - HDR impact of fighting vs. accepting
  - Opportunity cost of management time
  - Deadline urgency

Output: a final FIGHT / ACCEPT / ESCALATE / NEGOTIATE recommendation
with full supporting rationale.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FinalAction(str, Enum):
    FIGHT       = "FIGHT"       # File dispute response
    ACCEPT      = "ACCEPT"      # Accept loss, no response
    NEGOTIATE   = "NEGOTIATE"   # Offer partial refund before dispute escalates
    ESCALATE    = "ESCALATE"    # Escalate to arbitration / account manager
    DEFER       = "DEFER"       # Wait for more evidence before deciding


class OutcomeConfidence(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


class DecisionStrength(str, Enum):
    STRONG      = "STRONG"       # Clear winner by multiple criteria
    MODERATE    = "MODERATE"     # Leans one way with caveats
    BORDERLINE  = "BORDERLINE"   # Could go either way
    DEFER       = "DEFER"        # Not enough information


@dataclass
class OutcomePredictorInput:
    """All signals for the final outcome prediction."""
    dispute_id: str
    reason_code: str
    network: str
    transaction_amount: float

    # From win_rate_estimator
    win_probability: float = 0.50

    # Costs
    fight_cost: float = 15.0        # Staff time + system cost to fight
    chargeback_fee: float = 20.0    # Fee charged if we lose (card networks)
    negotiation_cost: float = 5.0   # Cost to offer partial refund

    # Partial refund option
    can_offer_partial_refund: bool = False
    partial_refund_amount: float = 0.0   # Amount we'd refund to avoid dispute

    # HDR impact
    hdr_tier: str = "NORMAL"
    hdr_dispute_rate_current: float = 0.0   # Current rolling rate (0–1)
    hdr_dispute_rate_threshold: float = 0.01

    # Deadline
    days_until_deadline: int = 10
    has_missed_deadline: bool = False

    # Evidence state
    evidence_strength: Optional[str] = None    # COMPELLING/STRONG/ADEQUATE/WEAK/INSUFFICIENT
    can_file_dispute: bool = True

    # Buyer context
    buyer_lifetime_value: float = 0.0
    is_vip_buyer: bool = False

    # Seller Protection
    seller_protection_eligible: bool = False
    ce30_applicable: bool = False


@dataclass
class FinancialBreakdown:
    """EV comparison between fight and accept scenarios."""
    # Fight scenario
    fight_ev: float
    fight_cost: float
    fight_win_payout: float
    fight_loss_cost: float

    # Accept scenario
    accept_cost: float    # Usually just transaction amount (revenue lost)
    accept_churn_cost: float  # Estimated LTV churn risk

    # Negotiate scenario
    negotiate_cost: float  # Partial refund + management time

    # Comparison
    fight_vs_accept_advantage: float   # fight_ev - (-accept_cost)
    recommended_ev: float


@dataclass
class OutcomePrediction:
    """Final dispute outcome prediction and recommendation."""
    dispute_id: str
    reason_code: str

    final_action: FinalAction
    decision_strength: DecisionStrength
    confidence: OutcomeConfidence

    win_probability: float
    financial: FinancialBreakdown

    rationale: list[str]       # Ordered list of key reasons
    risks: list[str]           # Risks with the recommended action
    alternatives: list[str]    # Other viable options

    deadline_urgent: bool
    hdr_concern: bool

    summary: str


def _compute_ev_fight(p: float, amount: float,
                       fight_cost: float, cb_fee: float) -> float:
    return p * amount - fight_cost - (1 - p) * cb_fee


def _compute_ev_accept(amount: float, ltv: float,
                        is_vip: bool) -> tuple[float, float]:
    """Return (direct_loss, churn_risk)."""
    churn_risk = 0.0
    if is_vip:
        churn_risk = ltv * 0.15
    elif ltv > 200:
        churn_risk = ltv * 0.05
    return amount, churn_risk


def _compute_ev_negotiate(partial: float, neg_cost: float,
                           amount: float) -> float:
    """Cost = partial refund + negotiation time, savings = amount - partial."""
    return -(partial + neg_cost)


def predict_dispute_outcome(inp: OutcomePredictorInput) -> OutcomePrediction:
    """Synthesize all signals into a final dispute action recommendation."""
    p = inp.win_probability

    # ── EV calculations ──────────────────────────────────────────
    ev_fight = _compute_ev_fight(p, inp.transaction_amount,
                                  inp.fight_cost, inp.chargeback_fee)
    accept_loss, churn = _compute_ev_accept(
        inp.transaction_amount, inp.buyer_lifetime_value, inp.is_vip_buyer)
    ev_accept = -(accept_loss + churn)   # Net cost of accepting (negative)

    ev_negotiate = 0.0
    if inp.can_offer_partial_refund:
        ev_negotiate = _compute_ev_negotiate(
            inp.partial_refund_amount, inp.negotiation_cost,
            inp.transaction_amount)

    fight_vs_accept = ev_fight - ev_accept
    recommended_ev = max(ev_fight, ev_accept,
                          ev_negotiate if inp.can_offer_partial_refund else -9999)

    fin = FinancialBreakdown(
        fight_ev=round(ev_fight, 2),
        fight_cost=inp.fight_cost,
        fight_win_payout=round(p * inp.transaction_amount, 2),
        fight_loss_cost=round((1 - p) * inp.chargeback_fee, 2),
        accept_cost=round(accept_loss, 2),
        accept_churn_cost=round(churn, 2),
        negotiate_cost=round(inp.partial_refund_amount + inp.negotiation_cost, 2),
        fight_vs_accept_advantage=round(fight_vs_accept, 2),
        recommended_ev=round(recommended_ev, 2),
    )

    rationale: list[str] = []
    risks: list[str] = []
    alternatives: list[str] = []

    hdr_concern = (inp.hdr_dispute_rate_current >=
                   inp.hdr_dispute_rate_threshold * 0.80)

    deadline_urgent = inp.days_until_deadline <= 3

    # ── Can't file? ───────────────────────────────────────────────
    if inp.has_missed_deadline or not inp.can_file_dispute:
        action = FinalAction.ACCEPT
        strength = DecisionStrength.STRONG
        rationale.append("Cannot file dispute response — deadline missed or insufficient evidence")
        risks.append("No recourse available")
        confidence = OutcomeConfidence.HIGH
    else:
        # ── Decision logic ────────────────────────────────────────
        if p >= 0.70 and ev_fight > 20:
            action = FinalAction.FIGHT
            strength = DecisionStrength.STRONG
            rationale.append(f"High win probability ({p:.0%}) with positive EV (${ev_fight:+.0f})")
            if inp.seller_protection_eligible:
                rationale.append("Seller Protection coverage strengthens position")
            if inp.ce30_applicable:
                rationale.append("CE3.0 applies — +18pp boost from prior transactions")
            risks.append("Still ~{:.0%} chance of losing + chargeback fee".format(1 - p))

        elif p >= 0.55 and ev_fight > 0:
            action = FinalAction.FIGHT
            strength = DecisionStrength.MODERATE
            rationale.append(f"Positive EV (${ev_fight:+.0f}) with {p:.0%} win odds — worth fighting")
            if inp.can_offer_partial_refund and ev_negotiate > ev_fight - 5:
                alternatives.append("Negotiate partial refund as low-risk alternative")
            risks.append(f"Win odds below 60% — outcome uncertain")

        elif inp.can_offer_partial_refund and ev_negotiate > ev_fight + 5:
            action = FinalAction.NEGOTIATE
            strength = DecisionStrength.MODERATE
            rationale.append("Partial refund offers better net outcome than fighting")
            rationale.append(f"Saves dispute escalation cost and preserves buyer relationship")
            alternatives.append(f"Fight with {p:.0%} win odds if buyer refuses negotiation")
            risks.append("Buyer may still escalate after partial refund")

        elif ev_fight < -10 and p < 0.40:
            action = FinalAction.ACCEPT
            strength = DecisionStrength.MODERATE
            rationale.append(f"Negative EV (${ev_fight:+.0f}) with low win odds ({p:.0%})")
            if inp.can_offer_partial_refund:
                alternatives.append("Offer partial refund to show good faith")
            risks.append("Accepting sets precedent for future disputes from this buyer")

        elif inp.evidence_strength == "INSUFFICIENT":
            action = FinalAction.DEFER
            strength = DecisionStrength.MODERATE
            rationale.append("Evidence insufficient to file credible response — gather more first")
            risks.append("Deadline pressure may force premature action")
            alternatives.append("Fight with what's available if deadline imminent")

        else:
            action = FinalAction.FIGHT if ev_fight > ev_accept else FinalAction.ACCEPT
            strength = DecisionStrength.BORDERLINE
            rationale.append(f"Borderline decision: fight_ev=${ev_fight:+.0f}, accept_cost=${-ev_accept:+.0f}")
            alternatives.append("Negotiate partial refund to split the difference")

        # HDR modifier
        if hdr_concern and action == FinalAction.FIGHT:
            rationale.append(
                f"Note: HDR rate approaching threshold ({inp.hdr_dispute_rate_current:.2%}) — "
                "each additional dispute costs points"
            )
            risks.append("Additional disputes push HDR closer to FEE_TIER")

        if deadline_urgent and action == FinalAction.DEFER:
            action = FinalAction.FIGHT
            rationale.append("Deadline urgent — filing now despite evidence gaps")

        # VIP buyer logic
        if inp.is_vip_buyer and action == FinalAction.ACCEPT:
            alternatives.append(f"VIP buyer (LTV ${inp.buyer_lifetime_value:.0f}) — "
                                 "consider negotiating rather than fighting to preserve relationship")

        confidence = (OutcomeConfidence.HIGH if len(rationale) >= 2
                      else OutcomeConfidence.MEDIUM)

    summary = (
        f"Outcome {inp.dispute_id}: "
        f"action={action.value} | strength={strength.value} | "
        f"p={p:.2f} | ev_fight=${ev_fight:+.0f} | "
        f"deadline={inp.days_until_deadline}d"
    )

    return OutcomePrediction(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        final_action=action,
        decision_strength=strength,
        confidence=confidence,
        win_probability=p,
        financial=fin,
        rationale=rationale,
        risks=risks,
        alternatives=alternatives,
        deadline_urgent=deadline_urgent,
        hdr_concern=hdr_concern,
        summary=summary,
    )
