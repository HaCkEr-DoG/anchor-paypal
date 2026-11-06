"""
Return Merchandise Authorizer — decide whether to accept a buyer's return
request for SNAD (Significantly Not As Described) disputes.

For SNAD disputes the merchant's best lever is often to offer a return
rather than fight on description accuracy. The return must be managed
carefully to avoid:
  - Paying for return shipping unnecessarily
  - Accepting empty-box or switch fraud
  - Taking a full loss on goods with partial delivery value

This module calculates:
  1. Whether to authorize the return (and under what conditions)
  2. Expected net cost of accepting vs. refusing the return
  3. Restocking value: can the returned item be resold?
  4. Recommended return conditions: who pays shipping, inspection required
  5. Red flags indicating potential fraud

Authorization decisions:
  AUTHORIZE_FULL        — Accept return, full refund on receipt
  AUTHORIZE_PARTIAL     — Accept return, offer partial refund
  AUTHORIZE_CONDITIONAL — Accept return with specific conditions (photos, etc.)
  DENY_RETURN           — Do not accept return; fight the dispute
  REQUEST_MORE_INFO     — Need more details before deciding

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ReturnDecision(str, Enum):
    AUTHORIZE_FULL        = "AUTHORIZE_FULL"
    AUTHORIZE_PARTIAL     = "AUTHORIZE_PARTIAL"
    AUTHORIZE_CONDITIONAL = "AUTHORIZE_CONDITIONAL"
    DENY_RETURN           = "DENY_RETURN"
    REQUEST_MORE_INFO     = "REQUEST_MORE_INFO"


class ReturnReason(str, Enum):
    NOT_AS_DESCRIBED     = "NOT_AS_DESCRIBED"
    SIGNIFICANTLY_WRONG  = "SIGNIFICANTLY_WRONG"
    WRONG_ITEM           = "WRONG_ITEM"
    MISSING_PARTS        = "MISSING_PARTS"
    DAMAGED_IN_TRANSIT   = "DAMAGED_IN_TRANSIT"
    COUNTERFEIT          = "COUNTERFEIT"
    NOT_FUNCTIONAL       = "NOT_FUNCTIONAL"


class FraudRisk(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


@dataclass
class ReturnInput:
    """Input for return authorization decision."""
    dispute_id: str
    order_id: str
    claim_type: str              # Should be SNAD for most cases

    # Order facts
    order_amount: float
    item_description: str
    cogs_pct: float = 0.40       # Cost of goods as % of order value

    # Buyer's claim
    return_reason: ReturnReason = ReturnReason.NOT_AS_DESCRIBED
    buyer_description_of_issue: str = ""

    # Item condition signals
    item_is_perishable: bool = False
    item_is_custom_made: bool = False
    item_is_digital: bool = False
    item_can_be_resold: bool = True       # Can item be restocked?
    item_resale_pct: float = 0.70         # Expected resale value as % of COGS

    # Shipping facts
    return_shipping_cost: float = 15.0
    merchant_pays_return_shipping: bool = False

    # Buyer signals
    buyer_dispute_history: int = 0        # Prior disputes in 90d
    buyer_has_sent_photos: bool = False
    buyer_account_age_days: int = 365
    is_first_order_from_buyer: bool = False

    # Seller signals
    listing_matches_item_sent: bool = True  # Does listing match what was sent?
    has_quality_control_docs: bool = False

    # Win probability if fighting the dispute
    p_win_if_fight: float = 0.38          # Base SNAD win rate
    fight_cost: float = 25.0


@dataclass
class ReturnCondition:
    """A specific condition attached to an authorized return."""
    requirement: str
    reason: str


@dataclass
class ReturnAuthorizationReport:
    """
    Return authorization decision and cost analysis.
    """
    dispute_id: str
    order_id: str
    claim_type: str

    # Decision
    decision: ReturnDecision
    fraud_risk: FraudRisk

    # Cost analysis
    cost_accept_return: float       # Net cost if merchant accepts return
    cost_refuse_return: float       # Expected cost if merchant fights
    ev_fight: float                 # Expected value of fighting
    recommended_net_cost: float     # Cost of recommended decision

    # Return terms
    conditions: list[ReturnCondition]
    merchant_pays_shipping: bool
    partial_refund_pct: Optional[float]    # If AUTHORIZE_PARTIAL
    inspection_required: bool

    # Fraud flags
    fraud_flags: list[str]

    # Guidance
    response_to_buyer: str
    action_items: list[str]
    summary: str


# ─────────────────────────────────────────────────────────────
# Cost calculations
# ─────────────────────────────────────────────────────────────

def _cogs(inp: ReturnInput) -> float:
    return round(inp.order_amount * inp.cogs_pct, 2)


def _resale_recovery(inp: ReturnInput) -> float:
    if not inp.item_can_be_resold or inp.item_is_perishable or inp.item_is_custom_made:
        return 0.0
    return round(_cogs(inp) * inp.item_resale_pct, 2)


def _cost_accept_return(inp: ReturnInput) -> float:
    """Net cost of accepting the return (full refund)."""
    cogs = _cogs(inp)
    resale = _resale_recovery(inp)
    ship_cost = inp.return_shipping_cost if inp.merchant_pays_return_shipping else 0.0
    return round(inp.order_amount + ship_cost - resale, 2)


def _cost_refuse(inp: ReturnInput) -> float:
    """Expected cost of fighting the dispute (EV-weighted)."""
    ev_loss = (1 - inp.p_win_if_fight) * inp.order_amount
    return round(inp.fight_cost + ev_loss, 2)


# ─────────────────────────────────────────────────────────────
# Fraud signals
# ─────────────────────────────────────────────────────────────

def _assess_fraud(inp: ReturnInput) -> tuple[FraudRisk, list[str]]:
    flags: list[str] = []

    if inp.buyer_dispute_history >= 3:
        flags.append(f"Serial filer: {inp.buyer_dispute_history} disputes in 90d")
    if inp.is_first_order_from_buyer and inp.order_amount >= 100:
        flags.append("First-time buyer, high-value order — elevated switch-fraud risk")
    if inp.buyer_account_age_days < 30:
        flags.append(f"New buyer account ({inp.buyer_account_age_days}d old)")
    if inp.return_reason == ReturnReason.COUNTERFEIT:
        flags.append("Counterfeit claim — verify with photos before authorizing")
    if not inp.buyer_has_sent_photos and inp.order_amount >= 100:
        flags.append("No photos submitted for high-value SNAD claim")
    if inp.return_reason == ReturnReason.WRONG_ITEM and inp.listing_matches_item_sent:
        flags.append("Buyer claims wrong item but listing matches shipped item")

    risk = (FraudRisk.HIGH if len(flags) >= 3
            else FraudRisk.MEDIUM if len(flags) >= 1
            else FraudRisk.LOW)
    return risk, flags


# ─────────────────────────────────────────────────────────────
# Decision logic
# ─────────────────────────────────────────────────────────────

def _decide(
    inp: ReturnInput,
    cost_accept: float,
    cost_refuse: float,
    fraud_risk: FraudRisk,
    fraud_flags: list[str],
) -> tuple[ReturnDecision, list[ReturnCondition], bool, Optional[float], bool]:
    """
    Returns: (decision, conditions, merchant_pays_shipping, partial_pct, inspection_required)
    """
    conditions: list[ReturnCondition] = []
    inspection_required = False

    # Digital items — cannot return
    if inp.item_is_digital:
        return ReturnDecision.DENY_RETURN, [], False, None, False

    # Custom-made — policy typically excludes returns
    if inp.item_is_custom_made:
        conditions.append(ReturnCondition(
            requirement="Item must be returned in original condition",
            reason="Custom-made items are generally non-returnable",
        ))
        return ReturnDecision.DENY_RETURN, conditions, False, None, False

    # Need photos if none provided for high-value
    if not inp.buyer_has_sent_photos and inp.order_amount >= 75:
        return ReturnDecision.REQUEST_MORE_INFO, [], False, None, False

    # High fraud risk — conditional authorization
    if fraud_risk == FraudRisk.HIGH:
        conditions.append(ReturnCondition(
            requirement="Buyer must submit clear photos of the issue before shipping",
            reason="Fraud signals detected on this return request",
        ))
        conditions.append(ReturnCondition(
            requirement="Inspection required upon receipt before refund issued",
            reason="Verifying item condition to prevent switch fraud",
        ))
        inspection_required = True
        return ReturnDecision.AUTHORIZE_CONDITIONAL, conditions, False, None, inspection_required

    # Economic analysis: compare costs
    ev_fight = inp.p_win_if_fight * inp.order_amount - inp.fight_cost
    accept_wins = cost_accept < cost_refuse

    # Perishable or missing parts — partial refund more appropriate
    if inp.return_reason == ReturnReason.MISSING_PARTS or inp.return_reason == ReturnReason.DAMAGED_IN_TRANSIT:
        partial_pct = 0.50 if inp.return_reason == ReturnReason.MISSING_PARTS else 0.30
        conditions.append(ReturnCondition(
            requirement="Provide photos/documentation of the specific issue",
            reason="Partial refund covers the damaged/missing portion only",
        ))
        return ReturnDecision.AUTHORIZE_PARTIAL, conditions, False, partial_pct, False

    if accept_wins or inp.order_amount <= 30:
        # Authorize full return
        pays_shipping = inp.order_amount >= 50 or inp.return_reason == ReturnReason.WRONG_ITEM
        if fraud_risk == FraudRisk.MEDIUM:
            inspection_required = True
            conditions.append(ReturnCondition(
                requirement="Inspect item on receipt before issuing refund",
                reason="Moderate fraud signals detected",
            ))
        return ReturnDecision.AUTHORIZE_FULL, conditions, pays_shipping, None, inspection_required

    # Fighting is cheaper
    return ReturnDecision.DENY_RETURN, [], False, None, False


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def authorize_return(inp: ReturnInput) -> ReturnAuthorizationReport:
    """
    Evaluate a return request for a SNAD dispute and produce authorization guidance.
    """
    fraud_risk, fraud_flags = _assess_fraud(inp)

    cost_accept = _cost_accept_return(inp)
    cost_refuse = _cost_refuse(inp)
    ev_fight    = inp.p_win_if_fight * inp.order_amount - inp.fight_cost

    decision, conditions, merchant_pays, partial_pct, inspection = _decide(
        inp, cost_accept, cost_refuse, fraud_risk, fraud_flags
    )

    recommended_cost = cost_accept if decision in (
        ReturnDecision.AUTHORIZE_FULL,
        ReturnDecision.AUTHORIZE_PARTIAL,
        ReturnDecision.AUTHORIZE_CONDITIONAL,
    ) else cost_refuse

    # Build buyer response hint
    if decision == ReturnDecision.AUTHORIZE_FULL:
        buyer_msg = (
            f"We're sorry the item didn't meet your expectations. "
            f"We've authorized a return — please ship it back "
            f"{'using the prepaid label we will send you' if merchant_pays else 'at your cost'} "
            f"and we'll issue a full refund upon receipt."
        )
    elif decision == ReturnDecision.AUTHORIZE_PARTIAL:
        pct_str = f"{int((partial_pct or 0.5) * 100)}%"
        buyer_msg = (
            f"We apologize for the inconvenience. We'd like to offer a {pct_str} partial refund "
            f"to resolve this without requiring a return shipment."
        )
    elif decision == ReturnDecision.AUTHORIZE_CONDITIONAL:
        buyer_msg = (
            f"We'd like to resolve this for you. Please send us clear photos of the issue "
            f"so we can verify and proceed with the return authorization."
        )
    elif decision == ReturnDecision.REQUEST_MORE_INFO:
        buyer_msg = (
            f"Thank you for reaching out. To process your request, please send us photos "
            f"clearly showing the issue with the item."
        )
    else:
        buyer_msg = (
            f"Thank you for your message. We've reviewed your claim and are unable to "
            f"authorize a return at this time. We're happy to discuss the issue further."
        )

    # Action items
    actions: list[str] = []
    if decision == ReturnDecision.AUTHORIZE_FULL:
        if merchant_pays:
            actions.append("Generate prepaid return shipping label and email to buyer")
        actions.append("Log return tracking; issue refund within 2 business days of receipt")
    if inspection:
        actions.append("Schedule item inspection on receipt before refund is released")
    if fraud_risk == FraudRisk.HIGH:
        actions.append("Flag order for fraud review team; document all buyer communications")
    if decision == ReturnDecision.DENY_RETURN:
        actions.append("Prepare dispute evidence package; file response within PayPal deadline")

    summary = (
        f"RMA {inp.dispute_id} [{inp.order_id}]: "
        f"decision={decision.value} | fraud={fraud_risk.value} | "
        f"cost_accept=${cost_accept:.0f} | cost_refuse=${cost_refuse:.0f} | "
        f"ev_fight=${ev_fight:.0f} | merchant_pays_ship={merchant_pays}"
    )

    return ReturnAuthorizationReport(
        dispute_id=inp.dispute_id,
        order_id=inp.order_id,
        claim_type=inp.claim_type,
        decision=decision,
        fraud_risk=fraud_risk,
        cost_accept_return=cost_accept,
        cost_refuse_return=cost_refuse,
        ev_fight=round(ev_fight, 2),
        recommended_net_cost=round(recommended_cost, 2),
        conditions=conditions,
        merchant_pays_shipping=merchant_pays,
        partial_refund_pct=partial_pct,
        inspection_required=inspection,
        fraud_flags=fraud_flags,
        response_to_buyer=buyer_msg,
        action_items=actions,
        summary=summary,
    )
