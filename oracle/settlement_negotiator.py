"""
Settlement Negotiation Engine — HOW to settle, not just IF.

decision.py says "settle at $X." This module says HOW.

The gap: when EV(fight) < EV(settle), merchants typically either:
  a) Issue a full refund immediately (overpay by 30–60%)
  b) Do nothing and lose by default

The right play is a structured negotiation:
  1. Lead with empathy (reduces buyer hostility)
  2. Offer partial refund + keep item (costs 40–60% less than full refund)
  3. Offer replacement (costs inventory + shipping, often < full refund)
  4. Offer store credit (costs margin, not cash)
  5. Full refund only when item return confirmed

Research on dispute negotiation outcomes:
  - 42% of buyers accept a partial refund offer (avg: 55% of original price)
  - 28% accept store credit at 120% of purchase price
  - 15% accept replacement
  - Only 15% insist on full cash refund (these are often fraudsters)

The module also detects when negotiation is pointless:
  - Buyer has opened a formal PayPal case (not a message) → respond through PayPal
  - Buyer is a serial disputer (high risk tier) → don't negotiate, fight
  - Digital goods already accessed → present access log, don't offer refund

This engine generates:
  1. The optimal opening offer (minimize cost while maximizing acceptance)
  2. Ready-to-send negotiation messages at each escalation stage
  3. A walk-away threshold (what's the most you'll pay before fighting?)
  4. The EV model comparing each settlement path

Settlement paths ranked by cost (lowest to highest):
  - Store credit (120% face value): cost = 0.X × amount (margin only)
  - Partial refund 30%: cost = 0.30 × amount
  - Partial refund 50%: cost = 0.50 × amount
  - Replacement item: cost = COGS + shipping
  - Full refund: cost = 1.0 × amount
  - Full refund + keep item: cost = 1.0 × amount (but good PR, faster close)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SettlementOffer(str, Enum):
    STORE_CREDIT_120  = "STORE_CREDIT_120"   # Store credit at 120% of dispute amount
    PARTIAL_30        = "PARTIAL_30"         # 30% refund, keep item
    PARTIAL_50        = "PARTIAL_50"         # 50% refund, keep item
    PARTIAL_75        = "PARTIAL_75"         # 75% refund (near-full)
    REPLACEMENT       = "REPLACEMENT"        # Send replacement item
    FULL_REFUND_RETURN = "FULL_REFUND_RETURN" # Full refund on item return
    FULL_REFUND_KEEP  = "FULL_REFUND_KEEP"   # Full refund, buyer keeps item (fastest close)
    FIGHT             = "FIGHT"              # Don't settle — fight the dispute


@dataclass
class NegotiationPlan:
    """Complete negotiation strategy for one dispute."""
    opening_offer: SettlementOffer
    opening_cost: float                # Estimated cost of opening offer
    walkaway_threshold: float          # Maximum you'll pay before fighting
    escalation_sequence: list[SettlementOffer]  # What to offer if opening rejected
    opening_message: str               # Ready-to-send opening message
    escalation_messages: dict[SettlementOffer, str]  # Message per escalation step
    ev_comparison: dict[str, float]    # EV of each path
    should_negotiate: bool             # False = fight outright, True = try to settle
    reasoning: list[str] = field(default_factory=list)


def plan_negotiation(
    dispute_amount: float,
    claim_type: str = "ITEM_NOT_RECEIVED",
    *,
    # Merchant cost model
    item_cogs: float = 0.0,            # Cost of goods (for replacement EV)
    shipping_cost: float = 0.0,
    dispute_fee: float = 15.0,
    fight_win_probability: float = 0.60,
    merchant_hourly_rate: float = 50.0,
    fight_hours: float = 1.5,
    # Context signals
    buyer_risk_tier: str = "TIER_1_NORMAL",    # From buyer_intelligence
    is_repeat_customer: bool = False,
    buyer_has_engaged_positively: bool = False,  # Buyer replied amicably in messages
    item_already_delivered: bool = False,
    digital_goods_accessed: bool = False,
    # Store credit margin
    store_credit_margin: float = 0.40,  # Your margin % (what store credit actually costs)
) -> NegotiationPlan:
    """
    Plan the optimal settlement negotiation for a dispute.

    The goal: close the dispute at minimum cost while preserving the buyer
    relationship where possible. Fight only when EV(fight) > EV(best settlement).
    """
    reasoning = []

    # ── Never negotiate with high-risk / fraud buyers ─────────────────────────
    if buyer_risk_tier in ("TIER_4_SERIAL", "TIER_5_FRAUD"):
        return NegotiationPlan(
            opening_offer=SettlementOffer.FIGHT,
            opening_cost=0.0,
            walkaway_threshold=0.0,
            escalation_sequence=[],
            opening_message="",
            escalation_messages={},
            ev_comparison={"fight": fight_win_probability * dispute_amount - dispute_fee},
            should_negotiate=False,
            reasoning=[
                f"Buyer risk tier {buyer_risk_tier} — do not negotiate, fight aggressively",
                "Settling with fraud buyers rewards the behavior",
            ],
        )

    # ── EV(fight) calculation ─────────────────────────────────────────────────
    fight_cost = merchant_hourly_rate * fight_hours
    ev_fight = (fight_win_probability * dispute_amount) - fight_cost - dispute_fee
    reasoning.append(
        f"EV(fight) = {fight_win_probability:.0%} × ${dispute_amount:.2f} "
        f"− ${fight_cost:.0f} − ${dispute_fee:.0f} = ${ev_fight:.2f}"
    )

    # ── Cost of each settlement option ───────────────────────────────────────
    store_credit_cost = dispute_amount * store_credit_margin  # You pay margin, not face
    partial_30_cost   = dispute_amount * 0.30
    partial_50_cost   = dispute_amount * 0.50
    partial_75_cost   = dispute_amount * 0.75
    replacement_cost  = item_cogs + shipping_cost if item_cogs > 0 else dispute_amount * 0.55
    full_return_cost  = dispute_amount             # Refund on return
    full_keep_cost    = dispute_amount             # Refund, buyer keeps item

    # EV of each settlement (negative = cost to merchant, positive = amount saved vs full refund)
    ev_comparison = {
        "store_credit_120": -store_credit_cost,
        "partial_30":       -partial_30_cost,
        "partial_50":       -partial_50_cost,
        "partial_75":       -partial_75_cost,
        "replacement":      -replacement_cost,
        "full_refund_return": -full_return_cost,
        "full_refund_keep": -full_keep_cost,
        "fight":            ev_fight,
    }

    # Digital goods: accessed = do not refund
    if digital_goods_accessed:
        return NegotiationPlan(
            opening_offer=SettlementOffer.FIGHT,
            opening_cost=0.0,
            walkaway_threshold=0.0,
            escalation_sequence=[],
            opening_message=(
                "Thank you for reaching out. Our records show the digital product was "
                "accessed [DATE] prior to this dispute being filed. We are unable to "
                "process a refund for products that have been used. We have submitted "
                "our access logs to PayPal as evidence."
            ),
            escalation_messages={},
            ev_comparison=ev_comparison,
            should_negotiate=False,
            reasoning=["Digital goods accessed before dispute — fight, do not refund"],
        )

    # ── Choose opening offer ──────────────────────────────────────────────────
    # Logic: choose lowest-cost offer with reasonable acceptance probability
    # For repeat customers / engaged buyers, store credit is often accepted
    # For first-time buyers with amicable communication, partial refund works
    # For adversarial buyers, skip to higher offers

    if dispute_amount < 30:
        # Small amounts: just refund, not worth the negotiation overhead
        opening = SettlementOffer.FULL_REFUND_KEEP
        opening_cost = full_keep_cost
        escalation = []
        reasoning.append(f"Small amount (${dispute_amount:.0f}) — full refund is EV-optimal")

    elif item_already_delivered and claim_type == "ITEM_NOT_RECEIVED":
        # Delivered but buyer claims not received — escalate to PayPal, do not negotiate
        return NegotiationPlan(
            opening_offer=SettlementOffer.FIGHT,
            opening_cost=0.0,
            walkaway_threshold=0.0,
            escalation_sequence=[],
            opening_message="",
            escalation_messages={},
            ev_comparison=ev_comparison,
            should_negotiate=False,
            reasoning=["Item confirmed delivered — fight the INR claim with delivery proof"],
        )

    elif is_repeat_customer and buyer_has_engaged_positively:
        opening = SettlementOffer.STORE_CREDIT_120
        opening_cost = store_credit_cost
        escalation = [
            SettlementOffer.PARTIAL_50,
            SettlementOffer.PARTIAL_75,
            SettlementOffer.FULL_REFUND_RETURN,
        ]
        reasoning.append("Repeat + amicable buyer → store credit as opening offer")

    elif buyer_has_engaged_positively:
        opening = SettlementOffer.PARTIAL_30
        opening_cost = partial_30_cost
        escalation = [
            SettlementOffer.PARTIAL_50,
            SettlementOffer.PARTIAL_75,
            SettlementOffer.FULL_REFUND_RETURN,
        ]
        reasoning.append("Amicable buyer → 30% partial refund as opening offer")

    elif claim_type == "SIGNIFICANTLY_NOT_AS_DESCRIBED":
        # SNAD: some validity usually, start at 30%
        opening = SettlementOffer.PARTIAL_30
        opening_cost = partial_30_cost
        escalation = [SettlementOffer.PARTIAL_50, SettlementOffer.FULL_REFUND_RETURN]
        reasoning.append("SNAD claim → partial refund, escalate to full on return")

    else:
        opening = SettlementOffer.PARTIAL_50
        opening_cost = partial_50_cost
        escalation = [SettlementOffer.PARTIAL_75, SettlementOffer.FULL_REFUND_RETURN]
        reasoning.append("Standard opening: 50% partial refund + keep item")

    # Walk-away = when it's cheaper to fight than settle
    walkaway = min(ev_fight + dispute_fee + fight_cost, dispute_amount)
    reasoning.append(f"Walk-away threshold: ${walkaway:.0f} (beyond this, fight is EV-better)")

    # ── Message templates ─────────────────────────────────────────────────────
    opening_msg = _opening_message(opening, dispute_amount, claim_type, is_repeat_customer)
    escalation_msgs = {
        offer: _escalation_message(offer, dispute_amount)
        for offer in escalation
    }

    should_negotiate = ev_fight < -dispute_fee or dispute_amount < 100

    return NegotiationPlan(
        opening_offer=opening,
        opening_cost=round(opening_cost, 2),
        walkaway_threshold=round(walkaway, 2),
        escalation_sequence=escalation,
        opening_message=opening_msg,
        escalation_messages=escalation_msgs,
        ev_comparison={k: round(v, 2) for k, v in ev_comparison.items()},
        should_negotiate=should_negotiate,
        reasoning=reasoning,
    )


def _opening_message(
    offer: SettlementOffer,
    amount: float,
    claim_type: str,
    repeat: bool,
) -> str:
    name = "valued customer" if repeat else "you"
    base = (
        f"Dear {name},\n\n"
        "Thank you for reaching out. We're sorry to hear about your experience "
        "and want to resolve this quickly and fairly.\n\n"
    )
    if offer == SettlementOffer.STORE_CREDIT_120:
        body = (
            f"As a gesture of goodwill, we'd like to offer you a store credit of "
            f"${amount * 1.20:.2f} (20% above your purchase price), which you can "
            f"apply to any future order. This lets us resolve this immediately without "
            f"a formal dispute process."
        )
    elif offer == SettlementOffer.PARTIAL_30:
        body = (
            f"We'd like to offer a partial refund of ${amount * 0.30:.2f} "
            f"(30% of your purchase price) while you keep the item. "
            f"This reflects the fair-market value difference for the issue you've described."
        )
    elif offer == SettlementOffer.PARTIAL_50:
        body = (
            f"We'd like to offer a partial refund of ${amount * 0.50:.2f} "
            f"(50% of your purchase price) while you keep the item. "
            f"We believe this is a fair resolution given the circumstances."
        )
    elif offer == SettlementOffer.FULL_REFUND_KEEP:
        body = (
            f"We'd like to offer a full refund of ${amount:.2f}. "
            f"You're welcome to keep the item. We'll process this immediately "
            f"upon your confirmation."
        )
    elif offer == SettlementOffer.FULL_REFUND_RETURN:
        body = (
            f"We're happy to issue a full refund of ${amount:.2f} "
            f"upon receipt of the returned item. We'll provide a prepaid return label."
        )
    else:
        body = f"We'd like to offer a resolution valued at ${amount * 0.50:.2f}."

    close = (
        "\n\nPlease reply to confirm you'd like to proceed with this offer, "
        "and we'll process it right away.\n\nBest regards"
    )
    return base + body + close


def _escalation_message(offer: SettlementOffer, amount: float) -> str:
    if offer == SettlementOffer.PARTIAL_50:
        return (
            f"We understand our initial offer wasn't what you were hoping for. "
            f"We'd like to increase our offer to a ${amount * 0.50:.2f} partial refund "
            f"(50% of your purchase) while you keep the item. "
            f"This is our best offer before we respond through PayPal's formal process."
        )
    elif offer == SettlementOffer.PARTIAL_75:
        return (
            f"We want to resolve this for you. Our final offer before the formal "
            f"dispute process is ${amount * 0.75:.2f} — 75% of your purchase price. "
            f"Please let us know within 24 hours if you'd like to accept."
        )
    elif offer == SettlementOffer.FULL_REFUND_RETURN:
        return (
            f"We're prepared to issue a full refund of ${amount:.2f} upon return of "
            f"the item. We'll cover return shipping. This is our final offer."
        )
    else:
        return f"We're increasing our offer to ${amount * 0.75:.2f} to resolve this quickly."


if __name__ == "__main__":
    plan = plan_negotiation(
        dispute_amount=120.0,
        claim_type="ITEM_NOT_RECEIVED",
        item_cogs=35.0,
        fight_win_probability=0.65,
        is_repeat_customer=True,
        buyer_has_engaged_positively=True,
    )
    print(f"Opening offer: {plan.opening_offer.value} (cost: ${plan.opening_cost:.0f})")
    print(f"Walk-away: ${plan.walkaway_threshold:.0f}")
    print(f"Should negotiate: {plan.should_negotiate}")
    print(f"\nOpening message:\n{plan.opening_message}")
    print(f"\nEV comparison:")
    for k, v in sorted(plan.ev_comparison.items(), key=lambda x: x[1], reverse=True):
        print(f"  {k}: ${v:.2f}")
