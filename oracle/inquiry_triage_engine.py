"""
Inquiry Triage Engine — classify and route incoming PayPal messages.

Before a buyer opens a formal dispute, they may send inquiries through
PayPal's Resolution Center or messaging system. Responding correctly to
an inquiry can resolve the issue without escalation to a formal dispute.

This module:
  1. Classifies an incoming inquiry into a triage category
  2. Estimates escalation risk (probability it becomes a formal dispute)
  3. Determines the urgency tier (P1–P4)
  4. Recommends a response strategy and time-to-respond target
  5. Suggests proactive actions to deflect escalation

Triage categories:
  DELIVERY_INQUIRY     — Buyer asking where their order is
  REFUND_REQUEST       — Buyer asking for a refund (not yet formal)
  ITEM_CONCERN         — Buyer reports item not as described
  CANCELLATION_REQUEST — Buyer wants to cancel an order
  PAYMENT_ISSUE        — Buyer has a payment/charge question
  ACCOUNT_QUESTION     — Account or profile question
  FRAUD_SIGNAL         — Signs of potential unauthorized access
  GENERAL_INQUIRY      — Catch-all / unclear

Urgency tiers:
  P1 — High value + high escalation risk (respond within 2h)
  P2 — Moderate value or risk (respond within 8h)
  P3 — Low value and risk (respond within 24h)
  P4 — Routine / account question (respond within 48h)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class InquiryCategory(str, Enum):
    DELIVERY_INQUIRY      = "DELIVERY_INQUIRY"
    REFUND_REQUEST        = "REFUND_REQUEST"
    ITEM_CONCERN          = "ITEM_CONCERN"
    CANCELLATION_REQUEST  = "CANCELLATION_REQUEST"
    PAYMENT_ISSUE         = "PAYMENT_ISSUE"
    ACCOUNT_QUESTION      = "ACCOUNT_QUESTION"
    FRAUD_SIGNAL          = "FRAUD_SIGNAL"
    GENERAL_INQUIRY       = "GENERAL_INQUIRY"


class InquiryUrgency(str, Enum):
    P1 = "P1"   # Respond within 2h
    P2 = "P2"   # Respond within 8h
    P3 = "P3"   # Respond within 24h
    P4 = "P4"   # Respond within 48h


class ResponseStrategy(str, Enum):
    PROVIDE_TRACKING     = "PROVIDE_TRACKING"      # Share tracking + delivery status
    OFFER_REFUND         = "OFFER_REFUND"           # Proactively offer refund
    INVESTIGATE_FIRST    = "INVESTIGATE_FIRST"      # Check order details first
    ACCEPT_CANCELLATION  = "ACCEPT_CANCELLATION"    # Cancel order immediately
    DENY_CANCELLATION    = "DENY_CANCELLATION"      # Cannot cancel (shipped)
    PARTIAL_REFUND_OFFER = "PARTIAL_REFUND_OFFER"   # Offer partial resolution
    ESCALATE_INTERNALLY  = "ESCALATE_INTERNALLY"    # Needs internal team review
    ENGAGE_DIPLOMATICALLY = "ENGAGE_DIPLOMATICALLY" # De-escalate tone, gather info
    STANDARD_RESPONSE    = "STANDARD_RESPONSE"      # Routine response


class EscalationRisk(str, Enum):
    HIGH    = "HIGH"    # > 60% chance of formal dispute
    MEDIUM  = "MEDIUM"  # 30–60%
    LOW     = "LOW"     # < 30%


@dataclass
class InquiryInput:
    """Input describing an incoming buyer inquiry."""
    inquiry_id: str
    order_id: str

    # Buyer message signals (pre-classified by caller)
    message_keywords: list[str]      # Lowercase keywords extracted from message

    # Order facts
    order_amount: float
    order_age_days: int              # Days since order was placed
    is_shipped: bool = False
    has_tracking: bool = False
    tracking_shows_delivered: bool = False
    carrier_estimated_delivery_days: int = 7

    # Buyer signals
    buyer_message_count: int = 1     # Number of messages in this thread
    buyer_dispute_history: int = 0   # Prior disputes in last 90d
    buyer_tone_negative: bool = False  # Aggressive / threatening language

    # Seller context
    seller_response_time_avg_hours: float = 24.0  # Seller's avg response time
    is_in_hdr_zone: bool = False     # Is seller in HDR zone?


@dataclass
class InquiryTriageReport:
    """
    Triage assessment for one buyer inquiry.
    """
    inquiry_id: str
    order_id: str

    # Classification
    category: InquiryCategory
    urgency: InquiryUrgency
    escalation_risk: EscalationRisk
    p_escalation: float              # Probability 0.0–1.0

    # Response guidance
    strategy: ResponseStrategy
    respond_within_hours: int        # Target response time
    response_template_hint: str      # One-line hint for templating

    # Proactive actions
    proactive_actions: list[str]

    # Risk factors
    risk_factors: list[str]

    summary: str


# ─────────────────────────────────────────────────────────────
# Classification logic
# ─────────────────────────────────────────────────────────────

_DELIVERY_KEYWORDS = {"where", "tracking", "shipped", "arrived", "delivery", "deliver", "usps", "fedex", "ups", "dhl", "package", "shipment"}
_REFUND_KEYWORDS   = {"refund", "money back", "reimburse", "return my money", "pay me back", "credit"}
_ITEM_CONCERN_KW   = {"wrong", "broken", "damaged", "not as described", "defective", "missing part", "not what", "fake", "counterfeit", "quality"}
_CANCEL_KEYWORDS   = {"cancel", "cancellation", "haven't shipped", "not shipped yet", "stop order"}
_PAYMENT_KW        = {"charge", "charged twice", "double charge", "billing", "overcharged", "extra", "unauthorized"}
_FRAUD_KEYWORDS    = {"hacked", "someone else", "not me", "i didn't order", "unauthorized", "fraud", "stolen", "account access"}


def _classify_category(inp: InquiryInput) -> InquiryCategory:
    kw = set(inp.message_keywords)

    if kw & _FRAUD_KEYWORDS:
        return InquiryCategory.FRAUD_SIGNAL
    if kw & _CANCEL_KEYWORDS:
        return InquiryCategory.CANCELLATION_REQUEST
    if kw & _ITEM_CONCERN_KW:
        return InquiryCategory.ITEM_CONCERN
    if kw & _REFUND_KEYWORDS:
        return InquiryCategory.REFUND_REQUEST
    if kw & _DELIVERY_KEYWORDS:
        return InquiryCategory.DELIVERY_INQUIRY
    if kw & _PAYMENT_KW:
        return InquiryCategory.PAYMENT_ISSUE
    return InquiryCategory.GENERAL_INQUIRY


def _estimate_p_escalation(inp: InquiryInput, category: InquiryCategory) -> float:
    """Estimate probability inquiry becomes a formal dispute."""
    base = {
        InquiryCategory.DELIVERY_INQUIRY:      0.25,
        InquiryCategory.REFUND_REQUEST:        0.55,
        InquiryCategory.ITEM_CONCERN:          0.50,
        InquiryCategory.CANCELLATION_REQUEST:  0.35,
        InquiryCategory.PAYMENT_ISSUE:         0.45,
        InquiryCategory.ACCOUNT_QUESTION:      0.05,
        InquiryCategory.FRAUD_SIGNAL:          0.70,
        InquiryCategory.GENERAL_INQUIRY:       0.10,
    }[category]

    p = base

    # Adjust for signals
    if inp.buyer_tone_negative:
        p += 0.20
    if inp.buyer_dispute_history >= 3:
        p += 0.15
    if inp.buyer_message_count >= 3:
        p += 0.10
    if not inp.is_shipped and inp.order_age_days >= inp.carrier_estimated_delivery_days:
        p += 0.15
    if inp.tracking_shows_delivered and category == InquiryCategory.DELIVERY_INQUIRY:
        p -= 0.15   # Tracking proof reduces risk
    if inp.seller_response_time_avg_hours > 48:
        p += 0.10   # Slow sellers get more disputes
    if inp.order_amount >= 500:
        p += 0.08
    if inp.is_in_hdr_zone:
        p += 0.05   # HDR pressure heightens caution

    return round(max(0.02, min(0.97, p)), 3)


def _urgency(inp: InquiryInput, p_esc: float) -> InquiryUrgency:
    if p_esc >= 0.60 or inp.order_amount >= 500:
        return InquiryUrgency.P1
    if p_esc >= 0.35 or inp.order_amount >= 100:
        return InquiryUrgency.P2
    if p_esc >= 0.20:
        return InquiryUrgency.P3
    return InquiryUrgency.P4


_URGENCY_HOURS = {
    InquiryUrgency.P1: 2,
    InquiryUrgency.P2: 8,
    InquiryUrgency.P3: 24,
    InquiryUrgency.P4: 48,
}


def _strategy(inp: InquiryInput, category: InquiryCategory) -> ResponseStrategy:
    if category == InquiryCategory.FRAUD_SIGNAL:
        return ResponseStrategy.ESCALATE_INTERNALLY
    if category == InquiryCategory.CANCELLATION_REQUEST:
        return ResponseStrategy.ACCEPT_CANCELLATION if not inp.is_shipped else ResponseStrategy.DENY_CANCELLATION
    if category == InquiryCategory.DELIVERY_INQUIRY:
        if inp.has_tracking:
            return ResponseStrategy.PROVIDE_TRACKING
        return ResponseStrategy.INVESTIGATE_FIRST
    if category == InquiryCategory.REFUND_REQUEST:
        if inp.order_amount <= 50:
            return ResponseStrategy.OFFER_REFUND   # Cheap to refund, buy goodwill
        return ResponseStrategy.PARTIAL_REFUND_OFFER
    if category == InquiryCategory.ITEM_CONCERN:
        return ResponseStrategy.PARTIAL_REFUND_OFFER if inp.order_amount <= 200 else ResponseStrategy.INVESTIGATE_FIRST
    if inp.buyer_tone_negative:
        return ResponseStrategy.ENGAGE_DIPLOMATICALLY
    return ResponseStrategy.STANDARD_RESPONSE


def _proactive_actions(inp: InquiryInput, category: InquiryCategory, p_esc: float) -> list[str]:
    actions: list[str] = []

    if category == InquiryCategory.DELIVERY_INQUIRY and inp.has_tracking:
        actions.append("Share tracking link and estimated delivery window immediately")
    if category == InquiryCategory.DELIVERY_INQUIRY and not inp.is_shipped:
        actions.append("Expedite fulfillment or update buyer with ship date")
    if category == InquiryCategory.CANCELLATION_REQUEST and not inp.is_shipped:
        actions.append("Process cancellation within 1h before item ships")
    if category == InquiryCategory.ITEM_CONCERN and inp.order_amount <= 100:
        actions.append("Consider proactive replacement or partial refund to avoid formal dispute")
    if p_esc >= 0.55:
        actions.append("Flag for case manager review — escalation risk is elevated")
    if inp.buyer_dispute_history >= 3:
        actions.append("Note buyer dispute history (serial filer) — document response thoroughly")
    if inp.is_in_hdr_zone:
        actions.append("HDR zone active — resolving this inquiry counts toward HDR rate improvement")
    if inp.buyer_message_count >= 3:
        actions.append("Buyer has messaged 3+ times — consider phone/chat de-escalation")

    return actions


def _risk_factors(inp: InquiryInput, category: InquiryCategory) -> list[str]:
    factors: list[str] = []
    if inp.buyer_tone_negative:
        factors.append("Buyer tone is negative/threatening")
    if inp.buyer_dispute_history >= 3:
        factors.append(f"Serial filer: {inp.buyer_dispute_history} disputes in 90d")
    if inp.order_amount >= 500:
        factors.append(f"High-value order: ${inp.order_amount:.2f}")
    if not inp.is_shipped and inp.order_age_days >= inp.carrier_estimated_delivery_days:
        factors.append(f"Order overdue: {inp.order_age_days}d old, estimated {inp.carrier_estimated_delivery_days}d delivery")
    if inp.buyer_message_count >= 3:
        factors.append(f"Buyer sent {inp.buyer_message_count} messages — escalating patience level")
    if category == InquiryCategory.FRAUD_SIGNAL:
        factors.append("Potential unauthorized transaction signal")
    return factors


def triage_inquiry(inp: InquiryInput) -> InquiryTriageReport:
    """
    Triage an incoming buyer inquiry and produce routing + response guidance.
    """
    category    = _classify_category(inp)
    p_esc       = _estimate_p_escalation(inp, category)
    urgency     = _urgency(inp, p_esc)
    esc_risk    = (EscalationRisk.HIGH if p_esc >= 0.60
                   else EscalationRisk.MEDIUM if p_esc >= 0.30
                   else EscalationRisk.LOW)
    strategy    = _strategy(inp, category)
    respond_hrs = _URGENCY_HOURS[urgency]
    actions     = _proactive_actions(inp, category, p_esc)
    risk_fctrs  = _risk_factors(inp, category)

    hint_map = {
        ResponseStrategy.PROVIDE_TRACKING:      "Share tracking number and carrier link; confirm expected delivery",
        ResponseStrategy.OFFER_REFUND:           "Apologize and offer full refund; include refund timeline",
        ResponseStrategy.INVESTIGATE_FIRST:      "Acknowledge receipt; investigate order status before replying further",
        ResponseStrategy.ACCEPT_CANCELLATION:    "Confirm cancellation; share refund timeline (3–5 business days)",
        ResponseStrategy.DENY_CANCELLATION:      "Explain item already shipped; offer return/refund process instead",
        ResponseStrategy.PARTIAL_REFUND_OFFER:   "Acknowledge concern; offer partial refund or replacement",
        ResponseStrategy.ESCALATE_INTERNALLY:    "Escalate to fraud/account team immediately; do not respond to buyer yet",
        ResponseStrategy.ENGAGE_DIPLOMATICALLY:  "Acknowledge frustration empathetically; ask one clarifying question",
        ResponseStrategy.STANDARD_RESPONSE:      "Respond politely with order status and next steps",
    }

    summary = (
        f"Inquiry {inp.inquiry_id} [{inp.order_id}]: "
        f"category={category.value} | urgency={urgency.value} | "
        f"p_esc={p_esc:.0%} ({esc_risk.value}) | "
        f"strategy={strategy.value} | respond_within={respond_hrs}h"
    )

    return InquiryTriageReport(
        inquiry_id=inp.inquiry_id,
        order_id=inp.order_id,
        category=category,
        urgency=urgency,
        escalation_risk=esc_risk,
        p_escalation=p_esc,
        strategy=strategy,
        respond_within_hours=respond_hrs,
        response_template_hint=hint_map[strategy],
        proactive_actions=actions,
        risk_factors=risk_fctrs,
        summary=summary,
    )
