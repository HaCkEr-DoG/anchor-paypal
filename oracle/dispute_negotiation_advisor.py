"""
Dispute Negotiation Advisor — recommend a negotiation strategy for
borderline disputes where fighting at full cost is not optimal but
outright acceptance leaves money on the table.

Covers:
  - Counter-offer calculation (what to offer and why)
  - Timing strategy (when to engage, when to stall)
  - Script suggestions for reaching out to buyer
  - Risk of escalation if negotiation fails
  - Walk-away threshold (EV below which accept is better than negotiating)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NegotiationStance(str, Enum):
    OFFER_FULL_REFUND          = "OFFER_FULL_REFUND"
    OFFER_PARTIAL_REFUND       = "OFFER_PARTIAL_REFUND"
    OFFER_STORE_CREDIT         = "OFFER_STORE_CREDIT"
    OFFER_REPLACEMENT          = "OFFER_REPLACEMENT"
    HOLD_FIRM                  = "HOLD_FIRM"    # Don't offer — fight
    ACCEPT_AND_CLOSE           = "ACCEPT_AND_CLOSE"


class NegotiationOutcome(str, Enum):
    RESOLVED_BEFORE_ESCALATION = "RESOLVED_BEFORE_ESCALATION"
    ESCALATED_TO_DISPUTE       = "ESCALATED_TO_DISPUTE"
    BUYER_WITHDREW             = "BUYER_WITHDREW"
    STALEMATE                  = "STALEMATE"


class TimingRecommendation(str, Enum):
    ACT_NOW        = "ACT_NOW"       # Respond within 24 hours
    ACT_SOON       = "ACT_SOON"      # Within 72 hours
    STANDARD       = "STANDARD"      # Normal window
    WAIT_FOR_INFO  = "WAIT_FOR_INFO" # Gather evidence first


@dataclass
class NegotiationAdvisorInput:
    """Input for negotiation strategy."""
    dispute_id: str
    reason_code: str
    transaction_amount: float

    # From other oracles
    win_probability: float = 0.50
    fight_cost: float = 15.0
    chargeback_fee: float = 20.0
    buyer_tone: str = "NEUTRAL"       # From buyer_communication_analyzer
    escalation_risk: str = "LOW"      # From buyer_communication_analyzer
    fraud_signal_level: str = "MINIMAL"

    # Buyer LTV
    buyer_ltv: float = 0.0
    is_vip: bool = False

    # What we can offer
    can_offer_partial: bool = True
    can_offer_store_credit: bool = True
    can_offer_replacement: bool = False
    max_refund_pct: float = 1.0       # Maximum refund as fraction of transaction

    # Timeline
    days_until_deadline: int = 10
    has_prior_negotiation: bool = False   # Already tried once

    # Context
    seller_protection_eligible: bool = False


@dataclass
class CounterOffer:
    """A single negotiation offer option."""
    stance: NegotiationStance
    offer_amount: float
    offer_pct: float            # As fraction of transaction
    rationale: str
    expected_buyer_acceptance: float    # Probability buyer accepts (0–1)
    ev: float                   # Expected value of this path
    script_opening: str         # Sample opening line for outreach


@dataclass
class NegotiationAdvisorReport:
    """Full negotiation strategy recommendation."""
    dispute_id: str
    reason_code: str
    transaction_amount: float

    # EV baselines
    ev_fight: float
    ev_accept: float

    # Recommended strategy
    primary_stance: NegotiationStance
    primary_offer: Optional[CounterOffer]
    fallback_stance: NegotiationStance
    fallback_offer: Optional[CounterOffer]

    # All options ranked by EV
    all_options: list[CounterOffer]

    timing: TimingRecommendation
    walk_away_threshold: float   # If buyer demands above this, accept chargeback

    negotiation_leverage: str    # Key points of leverage to cite
    risks: list[str]

    summary: str


def _ev_fight(p: float, amount: float, fight_cost: float, cb_fee: float) -> float:
    return p * amount - fight_cost - (1 - p) * cb_fee


def _ev_partial(offer_amount: float, acceptance_prob: float,
                p_win: float, amount: float,
                fight_cost: float, cb_fee: float) -> float:
    """EV of offering a partial refund.
    If buyer accepts: pay offer_amount.
    If buyer rejects: fall back to fighting.
    """
    ev_fight_fallback = _ev_fight(p_win, amount, fight_cost, cb_fee)
    return acceptance_prob * (-offer_amount) + (1 - acceptance_prob) * ev_fight_fallback


def _acceptance_prob_for_pct(pct: float, tone: str) -> float:
    """Rough estimate of buyer acceptance given refund pct and tone."""
    base = 0.30 + pct * 0.60   # 30% base, rises to 90% at full refund
    if tone == "COOPERATIVE":
        base = min(1.0, base + 0.10)
    elif tone == "HOSTILE":
        base = max(0.0, base - 0.15)
    elif tone == "DEMANDING":
        base = max(0.0, base - 0.05)
    return round(base, 2)


_SCRIPTS: dict[NegotiationStance, str] = {
    NegotiationStance.OFFER_PARTIAL_REFUND: (
        "Thank you for reaching out. We understand your concern and want to resolve "
        "this quickly. As a gesture of good faith, we'd like to offer a partial refund "
        "of {offer_amount} to close this matter — please let us know if that works for you."
    ),
    NegotiationStance.OFFER_FULL_REFUND: (
        "We sincerely apologize for the inconvenience. We're happy to issue a full refund "
        "of {offer_amount} to resolve this matter. Please confirm your preferred resolution."
    ),
    NegotiationStance.OFFER_STORE_CREDIT: (
        "We'd like to make this right with store credit of {offer_amount}, valid for 12 months. "
        "This lets you choose a replacement at your convenience. Would that work for you?"
    ),
    NegotiationStance.OFFER_REPLACEMENT: (
        "We'd like to send a replacement item at no additional charge. "
        "Please confirm your shipping address and we'll dispatch it immediately."
    ),
    NegotiationStance.HOLD_FIRM: (
        "Thank you for reaching out. We've reviewed the order and confirmed delivery "
        "to the address on file. We're confident in our position and have submitted "
        "our evidence to the payment network for review."
    ),
    NegotiationStance.ACCEPT_AND_CLOSE: (
        "We've processed a full refund for your order. "
        "Please allow 3–5 business days for it to appear on your statement."
    ),
}


def advise_negotiation(inp: NegotiationAdvisorInput) -> NegotiationAdvisorReport:
    """Generate a negotiation strategy recommendation."""
    amount = inp.transaction_amount
    p = inp.win_probability
    ev_f = _ev_fight(p, amount, inp.fight_cost, inp.chargeback_fee)
    ev_accept = -(amount)   # Net cost of accepting (negative — it's a loss)

    # Walk-away threshold: if refund > this, just take the chargeback
    # Chargeback cost = amount + chargeback_fee; partial saves cb_fee
    walk_away = round(amount + inp.chargeback_fee - 1.0, 2)

    all_options: list[CounterOffer] = []

    # ── Option A: Hold firm / fight ───────────────────────────────
    opt_fight = CounterOffer(
        stance=NegotiationStance.HOLD_FIRM,
        offer_amount=0.0,
        offer_pct=0.0,
        rationale="Win probability and EV favor fighting",
        expected_buyer_acceptance=0.0,
        ev=round(ev_f, 2),
        script_opening=_SCRIPTS[NegotiationStance.HOLD_FIRM],
    )
    all_options.append(opt_fight)

    # ── Option B: 25% partial ─────────────────────────────────────
    if inp.can_offer_partial:
        pct_25 = min(0.25, inp.max_refund_pct)
        amt_25 = round(amount * pct_25, 2)
        acc_25 = _acceptance_prob_for_pct(pct_25, inp.buyer_tone)
        ev_25 = _ev_partial(amt_25, acc_25, p, amount, inp.fight_cost, inp.chargeback_fee)
        all_options.append(CounterOffer(
            stance=NegotiationStance.OFFER_PARTIAL_REFUND,
            offer_amount=amt_25,
            offer_pct=pct_25,
            rationale="Low offer; buyer may push back but saves most revenue",
            expected_buyer_acceptance=acc_25,
            ev=round(ev_25, 2),
            script_opening=_SCRIPTS[NegotiationStance.OFFER_PARTIAL_REFUND].format(
                offer_amount=f"${amt_25:.2f}"),
        ))

    # ── Option C: 50% partial ─────────────────────────────────────
    if inp.can_offer_partial:
        pct_50 = min(0.50, inp.max_refund_pct)
        amt_50 = round(amount * pct_50, 2)
        acc_50 = _acceptance_prob_for_pct(pct_50, inp.buyer_tone)
        ev_50 = _ev_partial(amt_50, acc_50, p, amount, inp.fight_cost, inp.chargeback_fee)
        all_options.append(CounterOffer(
            stance=NegotiationStance.OFFER_PARTIAL_REFUND,
            offer_amount=amt_50,
            offer_pct=pct_50,
            rationale="Balanced offer; moderate acceptance probability",
            expected_buyer_acceptance=acc_50,
            ev=round(ev_50, 2),
            script_opening=_SCRIPTS[NegotiationStance.OFFER_PARTIAL_REFUND].format(
                offer_amount=f"${amt_50:.2f}"),
        ))

    # ── Option D: Store credit (75% equivalent) ───────────────────
    if inp.can_offer_store_credit:
        credit_pct = min(0.75, inp.max_refund_pct)
        credit_amt = round(amount * credit_pct, 2)
        acc_credit = _acceptance_prob_for_pct(credit_pct, inp.buyer_tone) * 0.80  # buyers prefer cash
        ev_credit = _ev_partial(credit_amt * 0.60, acc_credit, p, amount,
                                inp.fight_cost, inp.chargeback_fee)   # 60% redemption cost
        all_options.append(CounterOffer(
            stance=NegotiationStance.OFFER_STORE_CREDIT,
            offer_amount=credit_amt,
            offer_pct=credit_pct,
            rationale="Store credit costs less than cash refund (not all redeemed)",
            expected_buyer_acceptance=acc_credit,
            ev=round(ev_credit, 2),
            script_opening=_SCRIPTS[NegotiationStance.OFFER_STORE_CREDIT].format(
                offer_amount=f"${credit_amt:.2f}"),
        ))

    # ── Option E: Full refund ─────────────────────────────────────
    if inp.max_refund_pct >= 1.0:
        acc_full = _acceptance_prob_for_pct(1.0, inp.buyer_tone)
        ev_full_path = _ev_partial(amount, acc_full, p, amount,
                                    inp.fight_cost, inp.chargeback_fee)
        all_options.append(CounterOffer(
            stance=NegotiationStance.OFFER_FULL_REFUND,
            offer_amount=amount,
            offer_pct=1.0,
            rationale="Full refund avoids chargeback fee and HDR impact",
            expected_buyer_acceptance=acc_full,
            ev=round(ev_full_path, 2),
            script_opening=_SCRIPTS[NegotiationStance.OFFER_FULL_REFUND].format(
                offer_amount=f"${amount:.2f}"),
        ))

    # ── Option F: Accept / close ──────────────────────────────────
    opt_accept = CounterOffer(
        stance=NegotiationStance.ACCEPT_AND_CLOSE,
        offer_amount=amount,
        offer_pct=1.0,
        rationale="Accept chargeback — negative EV makes fighting pointless",
        expected_buyer_acceptance=1.0,
        ev=round(ev_accept, 2),
        script_opening=_SCRIPTS[NegotiationStance.ACCEPT_AND_CLOSE],
    )
    all_options.append(opt_accept)

    # ── Sort by EV (maximize — higher EV = cheaper to us in cost terms)
    # Note: EV here is from fight scenario perspective where positive = profitable
    # Accept EV is negative (pure cost), fight EV may be positive
    all_options_sorted = sorted(all_options, key=lambda x: -x.ev)

    # ── Primary and fallback recommendations ─────────────────────
    # Primary: best EV option that isn't just "hold firm" if tone is hostile
    if inp.buyer_tone == "COOPERATIVE" or p >= 0.65:
        primary = all_options_sorted[0]
    elif inp.escalation_risk in ("IMMINENT", "HIGH") and inp.can_offer_partial:
        # Escalation imminent — offer something to prevent CB
        partials = [o for o in all_options if o.stance == NegotiationStance.OFFER_PARTIAL_REFUND]
        primary = partials[0] if partials else all_options_sorted[0]
    else:
        primary = all_options_sorted[0]

    fallback_opts = [o for o in all_options_sorted if o.stance != primary.stance]
    fallback = fallback_opts[0] if fallback_opts else opt_accept

    # ── Timing ────────────────────────────────────────────────────
    if inp.escalation_risk == "IMMINENT" or inp.days_until_deadline <= 2:
        timing = TimingRecommendation.ACT_NOW
    elif inp.escalation_risk == "HIGH" or inp.days_until_deadline <= 5:
        timing = TimingRecommendation.ACT_SOON
    elif inp.fraud_signal_level in ("VERY_HIGH", "HIGH"):
        timing = TimingRecommendation.WAIT_FOR_INFO
    else:
        timing = TimingRecommendation.STANDARD

    # ── Leverage ──────────────────────────────────────────────────
    leverage_parts = []
    if inp.seller_protection_eligible:
        leverage_parts.append("Seller Protection coverage")
    if inp.fraud_signal_level in ("VERY_HIGH", "HIGH"):
        leverage_parts.append("Buyer fraud signals in communication")
    if p >= 0.60:
        leverage_parts.append(f"Strong win probability ({p:.0%})")
    if inp.buyer_tone == "COOPERATIVE":
        leverage_parts.append("Buyer appears willing to resolve — act now")
    if not leverage_parts:
        leverage_parts.append("Lead with evidence quality")
    leverage = "; ".join(leverage_parts)

    # ── Risks ────────────────────────────────────────────────────
    risks: list[str] = []
    if inp.escalation_risk in ("IMMINENT", "HIGH"):
        risks.append("Buyer likely to escalate quickly — time-sensitive")
    if inp.has_prior_negotiation:
        risks.append("Prior negotiation failed — buyer may expect full refund")
    if inp.buyer_tone == "HOSTILE":
        risks.append("Hostile tone — partial offers may be rejected or escalated")
    if inp.is_vip:
        risks.append(f"VIP buyer (LTV ${inp.buyer_ltv:.0f}) — relationship at risk")

    summary = (
        f"Negotiation {inp.dispute_id}: "
        f"primary={primary.stance.value} offer=${primary.offer_amount:.0f} | "
        f"ev={primary.ev:+.0f} | timing={timing.value}"
    )

    return NegotiationAdvisorReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        transaction_amount=amount,
        ev_fight=round(ev_f, 2),
        ev_accept=round(ev_accept, 2),
        primary_stance=primary.stance,
        primary_offer=primary,
        fallback_stance=fallback.stance,
        fallback_offer=fallback,
        all_options=all_options_sorted,
        timing=timing,
        walk_away_threshold=walk_away,
        negotiation_leverage=leverage,
        risks=risks,
        summary=summary,
    )
