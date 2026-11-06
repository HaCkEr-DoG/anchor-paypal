"""
Partial Shipment Analyzer — handle disputes where a buyer ordered multiple
items but only some were shipped, or shipped items don't fully match the order.

Partial shipment disputes are tricky because:
  1. The merchant may have shipped what was available
  2. The buyer may be disputing the full order amount
  3. SP may only apply to items that were shipped
  4. The correct response is usually a partial refund for unshipped items

This module computes:
  - Fair partial refund amount
  - Which items have SP coverage
  - Suggested defense approach (fight for shipped items, refund unshipped)
  - Evidence requirements per line item

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ShipmentStatus(str, Enum):
    SHIPPED_DELIVERED  = "SHIPPED_DELIVERED"
    SHIPPED_IN_TRANSIT = "SHIPPED_IN_TRANSIT"
    NOT_SHIPPED        = "NOT_SHIPPED"
    PARTIAL_SHIPMENT   = "PARTIAL_SHIPMENT"   # Sent partial quantity


@dataclass
class OrderLineItem:
    """One line item in an order."""
    item_id: str
    description: str
    quantity_ordered: int
    quantity_shipped: int
    unit_price: float
    shipment_status: str    # ShipmentStatus value
    tracking_number: Optional[str] = None
    has_delivery_confirmation: bool = False


@dataclass
class PartialShipmentInput:
    """Input for partial shipment analysis."""
    dispute_id: str
    total_order_amount: float
    disputed_amount: float    # What the buyer is disputing (may be full order)

    line_items: list[OrderLineItem]

    # Context
    sp_eligible_for_shipped: bool = True
    chargeback_fee: float = 20.0
    fight_cost: float = 10.0


def _item_value(item: OrderLineItem) -> float:
    return item.quantity_ordered * item.unit_price


def _shipped_value(item: OrderLineItem) -> float:
    return item.quantity_shipped * item.unit_price


def _unshipped_value(item: OrderLineItem) -> float:
    unshipped_qty = max(0, item.quantity_ordered - item.quantity_shipped)
    return unshipped_qty * item.unit_price


def _item_sp_eligible(item: OrderLineItem, sp_eligible_for_shipped: bool) -> bool:
    if not sp_eligible_for_shipped:
        return False
    status = item.shipment_status.upper()
    return status in ("SHIPPED_DELIVERED", "SHIPPED_IN_TRANSIT") and item.quantity_shipped > 0


@dataclass
class LineItemAnalysis:
    """Analysis for one line item."""
    item_id: str
    description: str
    total_value: float
    shipped_value: float
    unshipped_value: float
    sp_eligible: bool
    should_fight: bool
    should_refund: bool
    evidence_required: list[str]


@dataclass
class PartialShipmentReport:
    """Full analysis of a partial shipment dispute."""
    dispute_id: str
    total_order_amount: float
    disputed_amount: float

    # Per-item analysis
    line_items: list[LineItemAnalysis]

    # Aggregates
    total_shipped_value: float
    total_unshipped_value: float
    total_sp_eligible_value: float

    # Recommended actions
    refund_amount: float          # Amount to proactively refund (unshipped)
    fight_amount: float           # Amount to defend (shipped + SP)
    counter_dispute_amount: float # Appropriate counterclaim vs. full dispute

    # Whether buyer's disputed amount is correct
    buyer_dispute_fair: bool
    buyer_overcharging: bool
    buyer_overcharge_amount: float

    # Win probabilities
    p_win_shipped: float
    p_win_overall: float

    # Evidence checklist per item
    global_evidence: list[str]

    immediate_actions: list[str]
    summary: str


def analyze_partial_shipment(inp: PartialShipmentInput) -> PartialShipmentReport:
    """
    Analyze a partial shipment dispute and produce a defense/refund plan.
    """
    item_analyses: list[LineItemAnalysis] = []

    total_shipped = 0.0
    total_unshipped = 0.0
    total_sp_eligible = 0.0

    for item in inp.line_items:
        sv = _shipped_value(item)
        uv = _unshipped_value(item)
        tv = _item_value(item)
        sp_ok = _item_sp_eligible(item, inp.sp_eligible_for_shipped)

        should_fight = sv > 0 and sp_ok
        should_refund = uv > 0

        evidence: list[str] = []
        status = item.shipment_status.upper()
        if status in ("SHIPPED_DELIVERED", "SHIPPED_IN_TRANSIT"):
            if item.tracking_number:
                evidence.append(f"[READY] Tracking: {item.tracking_number}")
            else:
                evidence.append("[MISSING] Tracking number")
            if item.has_delivery_confirmation:
                evidence.append("[READY] Delivery confirmation")
            elif status == "SHIPPED_DELIVERED":
                evidence.append("[MISSING] Delivery confirmation")
        if status == "NOT_SHIPPED":
            evidence.append("[ACTION] Issue refund for unshipped item")

        item_analyses.append(LineItemAnalysis(
            item_id=item.item_id,
            description=item.description,
            total_value=round(tv, 2),
            shipped_value=round(sv, 2),
            unshipped_value=round(uv, 2),
            sp_eligible=sp_ok,
            should_fight=should_fight,
            should_refund=should_refund,
            evidence_required=evidence,
        ))

        total_shipped += sv
        total_unshipped += uv
        if sp_ok:
            total_sp_eligible += sv

    # Recommended refund = unshipped items
    refund_amount = round(total_unshipped, 2)
    fight_amount = round(total_shipped, 2)

    # What should the buyer's dispute really be?
    fair_dispute_amount = round(total_unshipped, 2)
    buyer_overcharging = inp.disputed_amount > fair_dispute_amount + 0.01
    overcharge_amount = round(max(0, inp.disputed_amount - fair_dispute_amount), 2)

    # Counter dispute: dispute is for full order, but only unshipped items valid
    counter_amount = round(inp.disputed_amount - refund_amount, 2)

    # Win probabilities
    # Shipped items with tracking: ~72% (INR base with SP)
    # Overall: depends on ratio
    if inp.total_order_amount > 0:
        shipped_ratio = total_shipped / inp.total_order_amount
    else:
        shipped_ratio = 0.0

    p_win_shipped = 0.72 if total_sp_eligible > 0 else 0.55
    p_win_overall = round(p_win_shipped * shipped_ratio + 0.0 * (1 - shipped_ratio), 2)

    # Global evidence
    global_evidence = [
        "Order confirmation showing all items ordered and prices",
        "Shipping records showing which items were dispatched",
    ]
    if total_unshipped > 0:
        global_evidence.append(
            f"Acknowledgment of unshipped items (${total_unshipped:.2f}) and proactive refund offer")

    # Immediate actions
    actions: list[str] = []
    if refund_amount > 0:
        actions.append(f"Issue proactive refund of ${refund_amount:.2f} for unshipped items")
    if fight_amount > 0 and total_sp_eligible > 0:
        actions.append(f"Fight shipped portion (${fight_amount:.2f}) — submit tracking evidence")
    if buyer_overcharging:
        actions.append(
            f"Buyer disputing ${inp.disputed_amount:.2f} but only ${fair_dispute_amount:.2f} valid — counter with partial")

    summary = (
        f"PartialShipment {inp.dispute_id}: "
        f"shipped=${total_shipped:.2f} | unshipped=${total_unshipped:.2f} | "
        f"refund=${refund_amount:.2f} | fight=${fight_amount:.2f} | "
        f"buyer_fair={not buyer_overcharging}"
    )

    return PartialShipmentReport(
        dispute_id=inp.dispute_id,
        total_order_amount=inp.total_order_amount,
        disputed_amount=inp.disputed_amount,
        line_items=item_analyses,
        total_shipped_value=round(total_shipped, 2),
        total_unshipped_value=round(total_unshipped, 2),
        total_sp_eligible_value=round(total_sp_eligible, 2),
        refund_amount=refund_amount,
        fight_amount=fight_amount,
        counter_dispute_amount=counter_amount,
        buyer_dispute_fair=not buyer_overcharging,
        buyer_overcharging=buyer_overcharging,
        buyer_overcharge_amount=overcharge_amount,
        p_win_shipped=p_win_shipped,
        p_win_overall=p_win_overall,
        global_evidence=global_evidence,
        immediate_actions=actions,
        summary=summary,
    )
