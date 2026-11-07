"""
Chargeback Prevention Advisor — prescribe proactive actions a merchant should
take RIGHT NOW to prevent a pending inquiry or at-risk transaction from ever
becoming a chargeback.

This is fundamentally different from all existing dispute tools: those work
AFTER the chargeback is filed. This module works BEFORE — in the 0-72 hour
window between a buyer's first contact and their decision to escalate.

Research basis:
  - 40% of chargebacks are filed after the buyer contacted merchant and
    got no satisfactory response (Verifi/Ethoca data).
  - Proactive outreach resolving a buyer complaint within 24h prevents
    chargeback escalation in ~65% of cases.
  - Partial refund offers at 60-80% prevent chargeback AND preserve the
    relationship in ~55% of SNAD/INR cases.

Signals analyzed:
  - Inquiry stage (before chargeback) → highest prevention window
  - Hours since purchase (0-24h vs 24-72h vs >72h)
  - Buyer contact history (did they reach out before filing?)
  - Delivery status (pending vs delayed vs delivered)
  - Reason type (INR vs SNAD vs UNAUTHORIZED)
  - Transaction amount vs dispute cost (small = accept first)
  - Buyer tier (VIP = prioritize relationship preservation)

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PreventionUrgency(str, Enum):
    IMMEDIATE  = "IMMEDIATE"   # Act within 1 hour
    URGENT     = "URGENT"      # Act within 6 hours
    STANDARD   = "STANDARD"    # Act within 24 hours
    MONITORING = "MONITORING"  # Low risk, monitor only


class PreventionAction(str, Enum):
    PROACTIVE_REFUND         = "PROACTIVE_REFUND"         # Issue refund before dispute
    PARTIAL_REFUND_OFFER     = "PARTIAL_REFUND_OFFER"     # Offer 60-80% partial
    SEND_TRACKING_UPDATE     = "SEND_TRACKING_UPDATE"     # Push carrier tracking to buyer
    BUYER_OUTREACH           = "BUYER_OUTREACH"           # Contact buyer proactively
    EXPEDITE_RESHIPMENT      = "EXPEDITE_RESHIPMENT"      # Reship lost/damaged item
    PROVIDE_USAGE_ACCESS     = "PROVIDE_USAGE_ACCESS"     # Restore digital access
    DOCUMENT_AND_MONITOR     = "DOCUMENT_AND_MONITOR"     # Low risk, just document
    ESCALATE_TO_MANAGER      = "ESCALATE_TO_MANAGER"      # High-value, needs SR rep


class BuyerTier(str, Enum):
    VIP         = "VIP"          # LTV ≥ $500
    HIGH        = "HIGH"         # LTV $200-499
    MEDIUM      = "MEDIUM"       # LTV $50-199
    LOW         = "LOW"          # LTV < $50
    UNKNOWN     = "UNKNOWN"


@dataclass
class PreventionInput:
    transaction_id: str
    merchant_id: str
    reason_code: str            # What buyer claims
    amount: float
    hours_since_purchase: float

    # Delivery signals
    delivery_status: str = "UNKNOWN"  # PENDING/IN_TRANSIT/DELAYED/DELIVERED/LOST
    days_past_expected_delivery: int = 0
    carrier_has_tracking: bool = False

    # Buyer relationship
    buyer_tier: str = "UNKNOWN"  # BuyerTier value
    buyer_contacted_support: bool = False   # Did buyer reach out before?
    buyer_complaint_unresolved: bool = False
    hours_since_buyer_contact: float = 0.0

    # Dispute stage
    is_inquiry_stage: bool = True   # Inquiry = pre-chargeback window
    has_active_inquiry: bool = False

    # Digital goods
    is_digital_product: bool = False
    digital_access_active: bool = True  # Is the buyer's access still live?

    # Economic signals
    response_cost: float = 15.0
    chargeback_fee: float = 20.0


@dataclass
class PreventionStep:
    step_number: int
    action: PreventionAction
    rationale: str
    time_to_complete: str   # "5 minutes", "1 hour", etc.
    expected_outcome: str
    cost_estimate: float    # $ cost to merchant to execute this step


@dataclass
class ChasebackPreventionReport:
    transaction_id: str
    merchant_id: str

    urgency: PreventionUrgency
    prevention_probability: float   # 0-1 probability this prevents the chargeback

    recommended_steps: list[PreventionStep]
    primary_action: PreventionAction

    prevention_roi: float   # $ saved by preventing vs cost of steps
    chargeback_cost_if_lost: float  # what this will cost if it becomes a chargeback

    buyer_tier: BuyerTier
    relationship_risk: str   # "HIGH" if VIP buyer about to churn

    key_insight: str    # One-sentence summary of why this dispute can be prevented
    summary: str


def _parse_buyer_tier(tier_str: str) -> BuyerTier:
    try:
        return BuyerTier(tier_str.upper())
    except ValueError:
        return BuyerTier.UNKNOWN


def _chargeback_total_cost(inp: PreventionInput) -> float:
    """Worst case cost: lose the dispute + fee + response cost."""
    return inp.amount + inp.chargeback_fee + inp.response_cost


def _build_steps_inr(inp: PreventionInput) -> list[PreventionStep]:
    steps: list[PreventionStep] = []
    n = 1

    if inp.delivery_status in ("PENDING", "IN_TRANSIT") and inp.carrier_has_tracking:
        steps.append(PreventionStep(
            step_number=n, action=PreventionAction.SEND_TRACKING_UPDATE,
            rationale="Buyer likely filed because they don't know where the package is — "
                      "proactive tracking push resolves 40% of INR inquiries instantly",
            time_to_complete="5 minutes",
            expected_outcome="Buyer sees real-time carrier status and waits for delivery",
            cost_estimate=0.0,
        )); n += 1

    if inp.delivery_status == "DELAYED" or inp.days_past_expected_delivery >= 2:
        steps.append(PreventionStep(
            step_number=n, action=PreventionAction.BUYER_OUTREACH,
            rationale=f"Package is {inp.days_past_expected_delivery}d past expected delivery — "
                      "proactive apology + ETA prevents escalation in most cases",
            time_to_complete="10 minutes",
            expected_outcome="Buyer feels heard and waits for updated ETA",
            cost_estimate=0.0,
        )); n += 1

    if inp.delivery_status == "LOST" or inp.days_past_expected_delivery >= 7:
        steps.append(PreventionStep(
            step_number=n, action=PreventionAction.EXPEDITE_RESHIPMENT,
            rationale="Package appears lost — reshipment costs less than chargeback + fee",
            time_to_complete="1 hour",
            expected_outcome="Buyer receives replacement; dispute never filed",
            cost_estimate=inp.amount * 0.35,  # rough reshipment/replacement cost
        )); n += 1

    if not steps:  # generic INR with no specific delivery context
        steps.append(PreventionStep(
            step_number=n, action=PreventionAction.BUYER_OUTREACH,
            rationale="Reach out before buyer escalates — 65% of inquiries resolved with proactive contact",
            time_to_complete="10 minutes",
            expected_outcome="Buyer cancels inquiry",
            cost_estimate=0.0,
        ))

    return steps


def _build_steps_snad(inp: PreventionInput) -> list[PreventionStep]:
    steps: list[PreventionStep] = []
    n = 1

    # Partial refund is most effective for SNAD — avoids chargeback + preserves LTV
    partial_pct = 0.70 if _parse_buyer_tier(inp.buyer_tier) in (BuyerTier.VIP, BuyerTier.HIGH) else 0.60
    partial_amount = round(inp.amount * partial_pct, 2)
    total_chargeback_cost = _chargeback_total_cost(inp)

    if partial_amount < total_chargeback_cost:
        steps.append(PreventionStep(
            step_number=n, action=PreventionAction.PARTIAL_REFUND_OFFER,
            rationale=f"Offer {partial_pct:.0%} partial refund (${partial_amount:.2f}) — "
                      f"costs ${partial_amount:.2f} vs ${total_chargeback_cost:.2f} chargeback total. "
                      f"Saves ${total_chargeback_cost - partial_amount:.2f} and preserves relationship.",
            time_to_complete="5 minutes",
            expected_outcome="Buyer accepts partial refund and closes inquiry",
            cost_estimate=partial_amount,
        )); n += 1

    steps.append(PreventionStep(
        step_number=n, action=PreventionAction.BUYER_OUTREACH,
        rationale="Ask buyer to describe exactly what was wrong — often reveals a miscommunication that can be resolved cheaply",
        time_to_complete="10 minutes",
        expected_outcome="Buyer explains specific issue; can be resolved without chargeback",
        cost_estimate=0.0,
    ))

    return steps


def _build_steps_unauthorized(inp: PreventionInput) -> list[PreventionStep]:
    steps: list[PreventionStep] = []
    n = 1

    # For unauthorized, proactive refund is usually best — win rate is low
    total_cost = _chargeback_total_cost(inp)
    if inp.amount <= total_cost * 1.5:
        steps.append(PreventionStep(
            step_number=n, action=PreventionAction.PROACTIVE_REFUND,
            rationale="UNAUTHORIZED disputes are hard to win without 3DS. "
                      "Proactive refund avoids chargeback fee and preserves account health.",
            time_to_complete="5 minutes",
            expected_outcome="Dispute closed; chargeback fee avoided; account health maintained",
            cost_estimate=inp.amount,
        )); n += 1
    else:
        steps.append(PreventionStep(
            step_number=n, action=PreventionAction.DOCUMENT_AND_MONITOR,
            rationale="High-value unauthorized claim — verify authenticity before refunding. "
                      "Pull IP/device logs and 3DS records first.",
            time_to_complete="30 minutes",
            expected_outcome="Informed decision on fight vs refund",
            cost_estimate=0.0,
        ))

    return steps


def _build_steps_subscription(inp: PreventionInput) -> list[PreventionStep]:
    return [
        PreventionStep(
            step_number=1, action=PreventionAction.PROACTIVE_REFUND,
            rationale="Subscription disputes are almost always about forgotten renewals — "
                      "proactive cancel+refund closes inquiry instantly and prevents chargeback fee",
            time_to_complete="5 minutes",
            expected_outcome="Buyer satisfied; subscription cancelled; no dispute",
            cost_estimate=inp.amount,
        )
    ]


_DISPATCH = {
    "INR":           _build_steps_inr,
    "ITEM_NOT_RECEIVED": _build_steps_inr,
    "SNAD":          _build_steps_snad,
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": _build_steps_snad,
    "13.1":          _build_steps_snad,
    "UNAUTHORIZED":  _build_steps_unauthorized,
    "10.4":          _build_steps_unauthorized,
    "SUBSCRIPTION":  _build_steps_subscription,
    "12.6":          _build_steps_subscription,
}


def advise_chargeback_prevention(inp: PreventionInput) -> ChasebackPreventionReport:
    """
    Prescribe proactive steps to prevent an inquiry from escalating to a chargeback.
    Most effective in the 0-72h window after buyer first contacts merchant.
    """
    code = inp.reason_code.upper()
    tier = _parse_buyer_tier(inp.buyer_tier)

    # Prevention probability — varies by action window
    if inp.hours_since_purchase <= 24:
        base_prevention_prob = 0.72
    elif inp.hours_since_purchase <= 72:
        base_prevention_prob = 0.55
    else:
        base_prevention_prob = 0.35

    if inp.is_inquiry_stage:
        base_prevention_prob = min(base_prevention_prob + 0.15, 0.90)
    if inp.buyer_contacted_support and not inp.buyer_complaint_unresolved:
        base_prevention_prob = min(base_prevention_prob + 0.10, 0.90)
    if inp.buyer_complaint_unresolved:
        base_prevention_prob = max(base_prevention_prob - 0.20, 0.10)

    # Urgency
    if inp.is_inquiry_stage and inp.hours_since_purchase <= 12:
        urgency = PreventionUrgency.IMMEDIATE
    elif inp.hours_since_purchase <= 24:
        urgency = PreventionUrgency.URGENT
    elif inp.hours_since_purchase <= 72:
        urgency = PreventionUrgency.STANDARD
    else:
        urgency = PreventionUrgency.MONITORING

    # Steps
    dispatch_fn = _DISPATCH.get(code)
    if dispatch_fn:
        steps = dispatch_fn(inp)
    else:
        steps = [PreventionStep(
            step_number=1, action=PreventionAction.BUYER_OUTREACH,
            rationale="Contact buyer immediately to understand complaint before escalation",
            time_to_complete="10 minutes",
            expected_outcome="Inquiry resolved or redirected to support channel",
            cost_estimate=0.0,
        )]

    # VIP escalation
    if tier in (BuyerTier.VIP, BuyerTier.HIGH) and urgency != PreventionUrgency.IMMEDIATE:
        steps.insert(0, PreventionStep(
            step_number=0, action=PreventionAction.ESCALATE_TO_MANAGER,
            rationale=f"VIP/HIGH buyer — assign to senior rep immediately to preserve relationship",
            time_to_complete="1 hour",
            expected_outcome="High-value customer retained; dispute resolved with white-glove treatment",
            cost_estimate=0.0,
        ))
        # Renumber
        for i, s in enumerate(steps, 1):
            s.step_number = i

    primary_action = steps[0].action if steps else PreventionAction.DOCUMENT_AND_MONITOR
    prevention_step_cost = sum(s.cost_estimate for s in steps[:1])  # first step cost
    chargeback_total = _chargeback_total_cost(inp)
    roi = round(chargeback_total * base_prevention_prob - prevention_step_cost, 2)

    relationship_risk = "HIGH" if tier in (BuyerTier.VIP, BuyerTier.HIGH) else "NORMAL"

    # Key insight
    if inp.is_inquiry_stage and base_prevention_prob >= 0.65:
        insight = (
            f"Dispute is still in inquiry stage — {base_prevention_prob:.0%} prevention probability "
            f"with immediate action. Acting now saves ~${roi:.0f}."
        )
    elif inp.hours_since_purchase <= 24:
        insight = (
            f"Within 24h of purchase — optimal prevention window. "
            f"Proactive contact resolves ~65% of early inquiries before chargeback."
        )
    else:
        insight = (
            f"Prevention still possible ({base_prevention_prob:.0%} probability) but window is narrowing. "
            f"Act within {6 if urgency == PreventionUrgency.STANDARD else 1} hours."
        )

    summary = (
        f"Prevention {inp.transaction_id} [{code}]: "
        f"urgency={urgency.value} prevention_prob={base_prevention_prob:.0%} "
        f"action={primary_action.value} roi=${roi:.0f}"
    )

    return ChasebackPreventionReport(
        transaction_id=inp.transaction_id,
        merchant_id=inp.merchant_id,
        urgency=urgency,
        prevention_probability=round(base_prevention_prob, 4),
        recommended_steps=steps,
        primary_action=primary_action,
        prevention_roi=roi,
        chargeback_cost_if_lost=round(chargeback_total, 2),
        buyer_tier=tier,
        relationship_risk=relationship_risk,
        key_insight=insight,
        summary=summary,
    )
