"""
Settlement Negotiator — partial settlement offer optimizer.

Before submitting a PayPal dispute response, merchants can reach out to buyers
directly to settle. This module answers:
  "Should I settle this dispute? If so, what amount should I offer?"

Settlement is attractive when:
  1. Fight win probability is moderate (40-65%) — not a certain win, not hopeless
  2. The dispute is for a service or subjective quality issue (SNAD)
  3. The buyer has not filed disputes repeatedly (not a fraudster)
  4. Settlement amount < expected loss from fighting

Settlement math:
  EV(fight) = P(win) × amount − fight_cost
  EV(settle at X) = −X (cost is the settlement paid)
  → Settle if X < −EV(fight), i.e., X < fight_cost − P(win) × amount
  → Max acceptable settlement = max amount where settling beats fighting

Offer strategy:
  - Opening offer: 40-60% of dispute amount (buyer expects to negotiate)
  - Walk-away: max acceptable settlement amount
  - If buyer counters above walk-away: reject and fight

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SettlementVerdict(str, Enum):
    SETTLE       = "SETTLE"      # Proactively offer settlement
    FIGHT        = "FIGHT"       # Fight is better EV — don't settle
    REFUND       = "REFUND"      # Just refund — fighting AND settling waste effort
    CONDITIONAL  = "CONDITIONAL" # Settle only if buyer initiates contact


class BuyerLikelihood(str, Enum):
    HIGH   = "HIGH"    # Buyer likely to accept a reasonable offer
    MEDIUM = "MEDIUM"  # Uncertain
    LOW    = "LOW"     # Buyer unlikely to settle (may be fraudulent)


_FIGHT_COST_DEFAULT = 25.0        # $ internal cost per dispute fought
_SETTLEMENT_TRANSACTION_COST = 0.0  # PayPal doesn't charge extra for direct refunds


@dataclass
class SettlementInput:
    """State of one dispute at settlement evaluation time."""
    dispute_id: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED
    dispute_amount: float

    # Win probability estimate (0-1)
    p_win: float                  # Probability of winning if fought

    # Buyer signals
    buyer_dispute_count_90d: int = 0
    buyer_responded_to_messages: bool = True
    buyer_is_long_term_customer: bool = False
    buyer_purchase_history_count: int = 1  # Total past orders

    # Context
    is_hdr_contributing: bool = True
    days_until_deadline: int = 10
    fight_cost: float = _FIGHT_COST_DEFAULT
    can_partially_fulfill: bool = False   # Can ship replacement, partial item, etc.


@dataclass
class CounterOfferAnalysis:
    """Analysis of a specific counter-offer amount from buyer."""
    counter_amount: float
    should_accept: bool
    ev_accept: float              # −counter_amount (accepting it costs that much)
    ev_fight_instead: float
    recommendation: str


@dataclass
class SettlementReport:
    """
    Settlement strategy for one dispute.
    """
    dispute_id: str
    claim_type: str
    dispute_amount: float
    p_win: float

    # Economics
    ev_fight: float               # EV(fight) = P(win) × amount − fight_cost
    max_acceptable_settlement: float   # Settling above this is worse than fighting
    break_even_settlement: float  # Settlement amount where EV(settle) = EV(fight)

    # Verdict
    verdict: SettlementVerdict
    buyer_likelihood: BuyerLikelihood

    # Offer strategy
    opening_offer: float          # Amount to open with
    walk_away_amount: float       # Maximum to pay before walking away
    offer_script: str             # Template message to buyer
    offer_tactics: list[str]

    # Decision support
    hdr_impact_note: str          # How HDR pressure affects the decision
    rationale: str
    summary: str

    def analyze_counter(self, counter_amount: float) -> CounterOfferAnalysis:
        """Should we accept this counter-offer from the buyer?"""
        ev_fight = self.ev_fight
        ev_accept = -counter_amount
        should_accept = counter_amount <= self.walk_away_amount
        if should_accept:
            rec = (
                f"Accept ${counter_amount:.0f} counter — "
                f"within walk-away limit (${self.walk_away_amount:.0f}). "
                f"Better than fighting (EV=${ev_fight:+.0f})."
            )
        else:
            rec = (
                f"Reject ${counter_amount:.0f} counter — exceeds walk-away (${self.walk_away_amount:.0f}). "
                f"Fight instead (EV=${ev_fight:+.0f})."
            )
        return CounterOfferAnalysis(
            counter_amount=counter_amount,
            should_accept=should_accept,
            ev_accept=round(ev_accept, 2),
            ev_fight_instead=round(ev_fight, 2),
            recommendation=rec,
        )


def _buyer_likelihood(inp: SettlementInput) -> BuyerLikelihood:
    if inp.buyer_dispute_count_90d >= 3:
        return BuyerLikelihood.LOW     # Likely fraudster — won't settle in good faith
    if not inp.buyer_responded_to_messages:
        return BuyerLikelihood.LOW
    if inp.buyer_is_long_term_customer and inp.buyer_purchase_history_count >= 3:
        return BuyerLikelihood.HIGH
    if inp.buyer_responded_to_messages and inp.buyer_purchase_history_count >= 2:
        return BuyerLikelihood.MEDIUM
    return BuyerLikelihood.MEDIUM


def _opening_offer(dispute_amount: float, p_win: float) -> float:
    """
    Opening offer as a fraction of dispute amount.
    Lower win probability → more generous opening (buyer has leverage).
    """
    if p_win >= 0.65:
        fraction = 0.35   # We're likely winning — start low
    elif p_win >= 0.50:
        fraction = 0.45
    elif p_win >= 0.35:
        fraction = 0.55
    else:
        fraction = 0.65   # We're likely losing — open more generously
    return round(dispute_amount * fraction, 2)


def _walk_away(ev_fight: float, dispute_amount: float) -> float:
    """
    Walk-away = max settlement we'd accept.
    If fighting has negative EV, walk-away is higher (more willing to pay to avoid fight).
    """
    # Walk-away is where EV(settle) = EV(fight)
    # EV(settle) = −X → X = −EV(fight)
    # But we cap at dispute_amount (never pay more than the disputed amount)
    walk = -ev_fight
    return round(min(dispute_amount, max(0.0, walk)), 2)


def _offer_tactics(inp: SettlementInput, verdict: SettlementVerdict, likelihood: BuyerLikelihood) -> list[str]:
    tactics: list[str] = []
    if verdict == SettlementVerdict.SETTLE:
        tactics.append("Contact buyer via PayPal messaging BEFORE submitting formal response")
        tactics.append("Lead with empathy: 'I want to resolve this quickly and fairly'")
        if inp.claim_type == "SNAD":
            tactics.append("For SNAD: offer partial refund + keep item (avoids return cost)")
        if inp.can_partially_fulfill:
            tactics.append("Offer replacement/partial fulfillment as alternative to cash refund")
        tactics.append("Set 48-hour deadline on offer to maintain urgency")
        if likelihood == BuyerLikelihood.HIGH:
            tactics.append("Long-term customer: lead with loyalty framing ('valued customer resolution')")
    elif verdict == SettlementVerdict.CONDITIONAL:
        tactics.append("Don't initiate — but if buyer contacts you, be open to reasonable settlement")
        tactics.append("Your walk-away limit still applies if buyer initiates")
    return tactics


def _offer_script(inp: SettlementInput, opening_offer: float) -> str:
    amount_str = f"${opening_offer:.0f}"
    if inp.claim_type == "SNAD":
        return (
            f"Hi, I'd like to resolve your dispute quickly. "
            f"I understand there was a concern with your order — "
            f"I'd like to offer a partial refund of {amount_str} as a goodwill resolution "
            f"and keep the case out of PayPal's formal process. "
            f"Please let me know within 48 hours if you'd like to accept this offer."
        )
    return (
        f"Hi, I want to resolve your dispute as quickly as possible. "
        f"I'd like to offer a settlement of {amount_str} to close this case. "
        f"This avoids a lengthy PayPal review process for both of us. "
        f"Please respond within 48 hours to accept."
    )


def _hdr_impact_note(inp: SettlementInput, verdict: SettlementVerdict) -> str:
    if not inp.is_hdr_contributing:
        return "This dispute does not contribute to HDR rate — HDR pressure not a factor."
    if verdict == SettlementVerdict.SETTLE:
        return (
            "Settling removes this dispute from the HDR count quickly — "
            "faster resolution means less impact on rolling 90-day rate."
        )
    return "Fighting extends dispute lifetime, keeping it in HDR count longer."


def evaluate_settlement(inp: SettlementInput) -> SettlementReport:
    """
    Evaluate whether to offer a settlement and compute optimal offer amounts.
    """
    ev_fight = round(inp.p_win * inp.dispute_amount - inp.fight_cost, 2)
    break_even = round(inp.dispute_amount * inp.p_win - inp.fight_cost, 2)
    max_acceptable = _walk_away(ev_fight, inp.dispute_amount)
    opening = _opening_offer(inp.dispute_amount, inp.p_win)
    likelihood = _buyer_likelihood(inp)

    # Determine verdict
    if inp.buyer_dispute_count_90d >= 3:
        verdict = SettlementVerdict.FIGHT  # Fraudsters: don't reward with settlement
        rationale = (
            f"Buyer filed {inp.buyer_dispute_count_90d} disputes in 90 days — likely fraud. "
            f"Do not settle; fight with fraud evidence instead."
        )
    elif inp.dispute_amount < 15.0:
        # Tiny amount — just refund, settlement overhead not worth it
        verdict = SettlementVerdict.REFUND
        rationale = f"${inp.dispute_amount:.0f} is too small for settlement negotiation. Just refund."
    elif ev_fight >= 50.0 and inp.p_win >= 0.65:
        # Strong case — fight is better than giving money away
        verdict = SettlementVerdict.FIGHT
        rationale = (
            f"Strong case: P(win)={inp.p_win:.0%}, EV(fight)=${ev_fight:+.0f}. "
            f"Fighting is more profitable than any settlement."
        )
    elif ev_fight < -15.0:
        # Weak case — just refund (settlement overhead not worth it)
        verdict = SettlementVerdict.REFUND
        rationale = (
            f"Weak case: EV(fight)=${ev_fight:+.0f}. Settlement offers little benefit "
            f"over direct refund, which is faster and cheaper."
        )
    elif 0.35 <= inp.p_win <= 0.65:
        # Moderate win probability — settlement sweet spot
        if likelihood in (BuyerLikelihood.HIGH, BuyerLikelihood.MEDIUM):
            verdict = SettlementVerdict.SETTLE
            rationale = (
                f"Moderate win probability ({inp.p_win:.0%}) and buyer likely to respond. "
                f"Proactive settlement at ${opening:.0f} has better expected value than "
                f"fighting (EV=${ev_fight:+.0f}) when accounting for uncertainty."
            )
        else:
            verdict = SettlementVerdict.CONDITIONAL
            rationale = (
                f"Moderate win probability ({inp.p_win:.0%}) but buyer unlikely to engage. "
                f"Don't initiate; accept reasonable offers up to ${max_acceptable:.0f} if buyer contacts you."
            )
    elif inp.claim_type == "SNAD" and inp.p_win < 0.50:
        verdict = SettlementVerdict.SETTLE
        rationale = (
            f"SNAD with sub-50% win rate ({inp.p_win:.0%}). "
            f"Settling avoids a likely loss and preserves customer relationship."
        )
    else:
        verdict = SettlementVerdict.CONDITIONAL
        rationale = (
            f"P(win)={inp.p_win:.0%}, EV(fight)=${ev_fight:+.0f}. "
            f"Fighting is marginally better; accept settlement only if buyer initiates below ${max_acceptable:.0f}."
        )

    tactics = _offer_tactics(inp, verdict, likelihood)
    script = _offer_script(inp, opening) if verdict in (SettlementVerdict.SETTLE, SettlementVerdict.CONDITIONAL) else ""
    hdr_note = _hdr_impact_note(inp, verdict)

    summary = (
        f"Settlement {inp.dispute_id}: {inp.claim_type} ${inp.dispute_amount:.0f} | "
        f"P(win)={inp.p_win:.0%} | EV(fight)=${ev_fight:+.0f} | "
        f"verdict={verdict.value} | opening=${opening:.0f} | walk-away=${max_acceptable:.0f}"
    )

    return SettlementReport(
        dispute_id=inp.dispute_id,
        claim_type=inp.claim_type,
        dispute_amount=inp.dispute_amount,
        p_win=inp.p_win,
        ev_fight=ev_fight,
        max_acceptable_settlement=max_acceptable,
        break_even_settlement=break_even,
        verdict=verdict,
        buyer_likelihood=likelihood,
        opening_offer=opening,
        walk_away_amount=max_acceptable,
        offer_script=script,
        offer_tactics=tactics,
        hdr_impact_note=hdr_note,
        rationale=rationale,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Backward-compatibility + Wave test-API
# ---------------------------------------------------------------------------
from enum import Enum as _Enum
from dataclasses import dataclass as _dataclass, field as _field_sn


class SettlementOffer(str, _Enum):
    FIGHT            = "FIGHT"
    PARTIAL_REFUND   = "PARTIAL_REFUND"
    FULL_REFUND_KEEP = "FULL_REFUND_KEEP"
    FULL_REFUND      = "FULL_REFUND"
    NEGOTIATE        = "NEGOTIATE"
    PARTIAL_30       = "PARTIAL_30"
    PARTIAL_50       = "PARTIAL_50"
    STORE_CREDIT_120 = "STORE_CREDIT_120"


@_dataclass
class _NegotiationPlan:
    opening_offer: SettlementOffer
    should_negotiate: bool
    opening_message: str
    escalation_sequence: list
    escalation_messages: dict
    ev_comparison: dict
    walkaway_threshold: float
    summary: str


def plan_negotiation(
    dispute_amount=None,
    p_win=0.5,
    ev_fight=None,
    buyer_risk_tier: str = "",
    claim_type: str = "ITEM_NOT_RECEIVED",
    item_already_delivered: bool = False,
    digital_goods_accessed: bool = False,
    is_repeat_customer: bool = False,
    buyer_has_engaged_positively: bool = False,
    fight_win_probability: float = 0.5,
    **kwargs,
):
    """Rich negotiation plan (wave test-API)."""
    amount = float(dispute_amount) if isinstance(dispute_amount, (int, float)) else 100.0

    # Hard-stop cases: always fight
    if buyer_risk_tier in ("TIER_5_FRAUD", "TIER_4_SERIAL"):
        offer = SettlementOffer.FIGHT
        negotiate = False
        msg = f"Dispute ${amount:.2f}: Buyer flagged {buyer_risk_tier} — fight, no negotiation."
    elif item_already_delivered:
        offer = SettlementOffer.FIGHT
        negotiate = False
        msg = f"Dispute ${amount:.2f}: Item confirmed delivered — fight INR claim."
    elif digital_goods_accessed:
        offer = SettlementOffer.FIGHT
        negotiate = False
        msg = f"Dispute ${amount:.2f}: Digital goods were accessed prior to dispute — fight."
    elif amount < 30:
        offer = SettlementOffer.FULL_REFUND_KEEP
        negotiate = False
        msg = f"Dispute ${amount:.2f}: Small amount — issue full refund, not worth contesting."
    elif is_repeat_customer and buyer_has_engaged_positively:
        offer = SettlementOffer.STORE_CREDIT_120
        negotiate = True
        msg = f"Dispute ${amount:.2f}: Valued repeat buyer — offer ${amount*1.20:.2f} store credit."
    elif buyer_has_engaged_positively:
        offer = SettlementOffer.PARTIAL_30
        negotiate = True
        msg = f"Dispute ${amount:.2f}: Amicable buyer — offer ${amount*0.30:.2f} partial refund (30%)."
    elif claim_type in ("SIGNIFICANTLY_NOT_AS_DESCRIBED", "SNAD"):
        offer = SettlementOffer.PARTIAL_30
        negotiate = True
        msg = f"Dispute ${amount:.2f}: SNAD claim — open with 30% partial refund."
    elif fight_win_probability >= 0.65 or p_win >= 0.65:
        offer = SettlementOffer.FIGHT
        negotiate = False
        msg = f"Dispute ${amount:.2f}: High win probability — fight."
    else:
        offer = SettlementOffer.NEGOTIATE
        negotiate = True
        msg = f"Dispute ${amount:.2f}: Moderate case — negotiate settlement."

    p = max(fight_win_probability, p_win)
    fight_ev = p * amount - 15.0 - (1 - p) * 20.0
    ev_cmp = {"fight": round(fight_ev, 2), "settle_30": round(-amount * 0.30, 2)}

    walkaway = round(amount * 0.5, 2) if negotiate else 0.0
    esc_seq = [SettlementOffer.PARTIAL_50, SettlementOffer.FULL_REFUND_KEEP] if negotiate else []
    esc_msg = {o: f"Escalation to {o.value}: offer ${amount * 0.5:.2f}" for o in esc_seq}
    esc_msg[offer] = msg

    return _NegotiationPlan(
        opening_offer=offer,
        should_negotiate=negotiate,
        opening_message=msg,
        escalation_sequence=esc_seq,
        escalation_messages=esc_msg,
        ev_comparison=ev_cmp,
        walkaway_threshold=walkaway,
        summary=msg,
    )
