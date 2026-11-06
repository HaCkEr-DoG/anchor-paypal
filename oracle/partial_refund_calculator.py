"""
Partial Refund Calculator — compute the optimal partial refund amount.

When a dispute involves partial delivery, partial damage, or a service
rendered but not fully, the merchant's options are:
  1. Full refund (lose the full amount)
  2. No refund (fight, risk losing everything)
  3. Partial refund (negotiate a middle ground)

This module computes:
  - Fair value of what was delivered vs. ordered
  - Optimal partial refund that minimizes expected loss
  - Negotiation range (min acceptable, recommended, max acceptable)
  - Per-item breakdowns for multi-item orders

Partial refund cases:
  INR:          Item not received — typically 0% delivered (full refund)
                But sometimes partially received (3/5 items shipped)
  SNAD:         Significantly not as described — partial refund based on
                the gap between described and actual value
  PARTIAL_SHIP: Some items shipped, some not
  DAMAGED:      Item received but damaged — partial refund based on
                diminution of value
  SUBSCRIPTION: Service partially rendered before cancellation

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PartialRefundBasis(str, Enum):
    ITEM_COUNT          = "ITEM_COUNT"       # X of Y items received
    DELIVERY_VALUE_PCT  = "DELIVERY_VALUE_PCT"  # Delivered value as % of ordered
    DAMAGE_REDUCTION    = "DAMAGE_REDUCTION"  # Damaged item → reduced value
    SERVICE_PROPORTION  = "SERVICE_PROPORTION"  # Days of service used
    NEGOTIATED_SPLIT    = "NEGOTIATED_SPLIT"  # Agreed-upon split


class PartialRefundOutcome(str, Enum):
    FULL_REFUND         = "FULL_REFUND"      # 100% refund warranted
    PARTIAL_REFUND      = "PARTIAL_REFUND"   # Calculated partial
    NO_REFUND           = "NO_REFUND"        # 0% refund (full delivery proven)
    NEGOTIATION_RANGE   = "NEGOTIATION_RANGE"  # Offer range to buyer


@dataclass
class OrderItem:
    """One line item in the order."""
    item_id: str
    description: str
    quantity_ordered: int
    quantity_received: int
    unit_price: float
    is_damaged: bool = False
    damage_reduction_pct: float = 0.0  # 0-1.0; e.g., 0.4 = worth 60% of original


@dataclass
class PartialRefundInput:
    """
    Input for computing optimal partial refund.
    """
    dispute_id: str
    claim_type: str               # INR / SNAD / PARTIAL_SHIP / DAMAGED / SUBSCRIPTION

    # Order totals
    order_total: float
    dispute_amount: float         # What buyer is claiming

    # Basis of partial delivery
    basis: PartialRefundBasis

    # For item-count basis
    items: list[OrderItem] = field(default_factory=list)

    # For percentage-based basis
    delivery_pct: float = 1.0     # 0-1.0; portion actually delivered
    damage_reduction_pct: float = 0.0  # 0-1.0; how much value was lost

    # For subscription/service
    service_days_delivered: int = 0
    service_days_total: int = 30

    # Economics
    p_win_no_refund: float = 0.5  # Win probability if merchant fights without refund
    fight_cost: float = 25.0

    # Policy
    min_refund_pct: float = 0.0   # Merchant's floor (never offer below this)
    max_refund_pct: float = 1.0   # Merchant's ceiling

    # Context
    buyer_is_loyal: bool = False
    is_formal_dispute: bool = True


@dataclass
class ItemRefundLine:
    """Per-item refund breakdown."""
    item_id: str
    description: str
    quantity_ordered: int
    quantity_received: int
    quantity_refunded: int
    unit_price: float
    item_refund_amount: float
    rationale: str


@dataclass
class PartialRefundReport:
    """
    Computed partial refund recommendation.
    """
    dispute_id: str
    claim_type: str
    basis: PartialRefundBasis
    outcome: PartialRefundOutcome

    # Recommended refund
    recommended_refund_amount: float
    recommended_refund_pct: float    # 0-1.0

    # Negotiation range
    min_refund_amount: float         # Minimum acceptable offer
    max_refund_amount: float         # Maximum merchant should offer
    opening_offer_amount: float      # First offer to buyer

    # Economics
    ev_partial_refund: float         # EV of partial refund vs. full fight
    ev_no_refund: float              # EV of fighting entirely
    ev_full_refund: float            # EV of full refund (−dispute_amount)

    # Per-item breakdown
    item_lines: list[ItemRefundLine]

    # Guidance
    rationale: str
    buyer_message: str
    summary: str


def _item_based_refund(inp: PartialRefundInput) -> tuple[float, float, list[ItemRefundLine]]:
    """Compute refund based on items received vs. ordered."""
    lines: list[ItemRefundLine] = []
    total_refund = 0.0
    total_ordered = sum(i.quantity_ordered * i.unit_price for i in inp.items)

    for item in inp.items:
        qty_not_received = max(0, item.quantity_ordered - item.quantity_received)
        # Damage reduction on received items
        damage_refund = 0.0
        if item.is_damaged and item.quantity_received > 0:
            damage_refund = item.quantity_received * item.unit_price * item.damage_reduction_pct

        item_refund = qty_not_received * item.unit_price + damage_refund
        total_refund += item_refund

        if qty_not_received > 0 or damage_refund > 0:
            if qty_not_received > 0 and damage_refund > 0:
                rationale = f"{qty_not_received} units not received + {item.quantity_received} damaged"
            elif qty_not_received > 0:
                rationale = f"{qty_not_received} of {item.quantity_ordered} units not received"
            else:
                rationale = f"{item.quantity_received} damaged units ({item.damage_reduction_pct*100:.0f}% reduction)"
        else:
            rationale = "Fully received, no damage"

        lines.append(ItemRefundLine(
            item_id=item.item_id,
            description=item.description,
            quantity_ordered=item.quantity_ordered,
            quantity_received=item.quantity_received,
            quantity_refunded=qty_not_received,
            unit_price=item.unit_price,
            item_refund_amount=round(item_refund, 2),
            rationale=rationale,
        ))

    pct = total_refund / total_ordered if total_ordered > 0 else 0.0
    return round(total_refund, 2), min(1.0, pct), lines


def _delivery_pct_refund(inp: PartialRefundInput) -> tuple[float, float]:
    """Compute refund based on delivery percentage."""
    not_delivered_pct = 1.0 - inp.delivery_pct
    damage_pct = inp.damage_reduction_pct * inp.delivery_pct
    total_pct = min(1.0, not_delivered_pct + damage_pct)
    amount = round(inp.dispute_amount * total_pct, 2)
    return amount, total_pct


def _service_refund(inp: PartialRefundInput) -> tuple[float, float]:
    """Compute refund based on service days used."""
    if inp.service_days_total <= 0:
        return inp.dispute_amount, 1.0
    days_not_delivered = max(0, inp.service_days_total - inp.service_days_delivered)
    pct = days_not_delivered / inp.service_days_total
    amount = round(inp.dispute_amount * pct, 2)
    return amount, pct


def compute_partial_refund(inp: PartialRefundInput) -> PartialRefundReport:
    """
    Compute the optimal partial refund for a dispute.
    """
    # Step 1: Compute fair refund amount based on basis
    item_lines: list[ItemRefundLine] = []

    if inp.basis == PartialRefundBasis.ITEM_COUNT and inp.items:
        fair_amount, fair_pct, item_lines = _item_based_refund(inp)
    elif inp.basis == PartialRefundBasis.SERVICE_PROPORTION:
        fair_amount, fair_pct = _service_refund(inp)
    else:
        fair_amount, fair_pct = _delivery_pct_refund(inp)

    # Step 2: Apply policy constraints
    min_amount = inp.dispute_amount * inp.min_refund_pct
    max_amount = inp.dispute_amount * inp.max_refund_pct
    fair_amount = max(min_amount, min(max_amount, fair_amount))
    fair_pct = fair_amount / inp.dispute_amount if inp.dispute_amount > 0 else 0.0

    # Step 3: Determine outcome
    if fair_pct >= 0.95:
        outcome = PartialRefundOutcome.FULL_REFUND
    elif fair_pct <= 0.05:
        outcome = PartialRefundOutcome.NO_REFUND
    else:
        outcome = PartialRefundOutcome.PARTIAL_REFUND

    # Step 4: Compute EVs
    ev_no_refund = inp.p_win_no_refund * inp.dispute_amount - inp.fight_cost
    ev_full_refund = -inp.dispute_amount
    # EV of partial: avoid fight, lose only partial amount
    ev_partial = -fair_amount

    # If EV(fight) > EV(partial), reconsider
    if ev_no_refund > ev_partial and outcome == PartialRefundOutcome.PARTIAL_REFUND:
        # Fighting is better, but partial is still safer
        pass  # Keep the recommendation; calling code decides

    # Step 5: Negotiation range
    # Opening offer: 70% of fair amount (anchor low)
    opening_offer = round(fair_amount * 0.70, 2)
    opening_offer = max(min_amount, opening_offer)

    # Min = floor, max = fair amount
    min_offer = min_amount
    max_offer = fair_amount

    # Loyal buyers: be more generous
    if inp.buyer_is_loyal:
        max_offer = min(max_amount, fair_amount * 1.15)
        opening_offer = fair_amount  # Offer fair value directly to loyal buyers

    # Step 6: Build rationale
    if inp.basis == PartialRefundBasis.ITEM_COUNT:
        rationale = (
            f"Item-count based: {sum(max(0, i.quantity_ordered - i.quantity_received) for i in inp.items)} "
            f"units not received out of {sum(i.quantity_ordered for i in inp.items)} ordered. "
            f"Refund = ${fair_amount:.2f} ({fair_pct*100:.1f}% of dispute)."
        )
    elif inp.basis == PartialRefundBasis.SERVICE_PROPORTION:
        rationale = (
            f"Service proportion: {inp.service_days_delivered} of {inp.service_days_total} days delivered. "
            f"Refund for unused {inp.service_days_total - inp.service_days_delivered} days = ${fair_amount:.2f}."
        )
    else:
        rationale = (
            f"Delivery basis: {inp.delivery_pct*100:.0f}% delivered, "
            f"{inp.damage_reduction_pct*100:.0f}% damage reduction on received portion. "
            f"Net refund = ${fair_amount:.2f} ({fair_pct*100:.1f}%)."
        )

    # Step 7: Buyer message
    if outcome == PartialRefundOutcome.FULL_REFUND:
        buyer_msg = (
            f"I'm sorry your order didn't arrive. I'll issue a full refund of ${fair_amount:.2f} "
            f"to resolve this. Please allow 3-5 business days for it to appear."
        )
    elif outcome == PartialRefundOutcome.NO_REFUND:
        buyer_msg = ""
    else:
        buyer_msg = (
            f"I'd like to resolve this fairly. Based on what was received, "
            f"I'd like to offer a partial refund of ${opening_offer:.2f}. "
            f"Please let me know if this works for you."
        )

    summary = (
        f"Partial refund {inp.dispute_id}: {inp.claim_type} | basis={inp.basis.value} | "
        f"fair={fair_pct*100:.1f}% (${fair_amount:.2f}) | "
        f"opening_offer=${opening_offer:.2f} | "
        f"ev_partial=${ev_partial:.0f} vs ev_fight=${ev_no_refund:.0f} | "
        f"outcome={outcome.value}"
    )

    return PartialRefundReport(
        dispute_id=inp.dispute_id,
        claim_type=inp.claim_type,
        basis=inp.basis,
        outcome=outcome,
        recommended_refund_amount=fair_amount,
        recommended_refund_pct=round(fair_pct, 4),
        min_refund_amount=round(min_offer, 2),
        max_refund_amount=round(max_offer, 2),
        opening_offer_amount=round(opening_offer, 2),
        ev_partial_refund=round(ev_partial, 2),
        ev_no_refund=round(ev_no_refund, 2),
        ev_full_refund=round(ev_full_refund, 2),
        item_lines=item_lines,
        rationale=rationale,
        buyer_message=buyer_msg,
        summary=summary,
    )
