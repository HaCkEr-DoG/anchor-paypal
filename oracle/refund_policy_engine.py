"""
Refund Policy Engine — determine whether and how much to refund a buyer
given the merchant's return/refund policy, the dispute context, and
economic incentives.

A merchant's refund decision is not purely policy-driven; it must also
account for the economic cost of fighting vs. accommodating, the buyer's
LTV, the HDR impact of an auto-loss, and the terms of any active
promotional offer. This module combines all of these signals into a
structured refund recommendation.

Decision dimensions:
  1. Policy eligibility — does the buyer's situation fall within policy?
  2. Economic calculus — is refunding cheaper than fighting?
  3. HDR sensitivity — does auto-losing push the merchant into a higher zone?
  4. Buyer relationship — is accommodating justified by LTV?

Refund modes:
  FULL_REFUND        — Return 100% of transaction value
  PARTIAL_REFUND     — Return a specified percentage
  STORE_CREDIT       — Offer credit instead of cash
  EXCHANGE_ONLY      — Replace item, no cash back
  DECLINE_REFUND     — Deny and fight the dispute
  CONDITIONAL_REFUND — Refund contingent on item return or other condition

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RefundMode(str, Enum):
    FULL_REFUND        = "FULL_REFUND"
    PARTIAL_REFUND     = "PARTIAL_REFUND"
    STORE_CREDIT       = "STORE_CREDIT"
    EXCHANGE_ONLY      = "EXCHANGE_ONLY"
    DECLINE_REFUND     = "DECLINE_REFUND"
    CONDITIONAL_REFUND = "CONDITIONAL_REFUND"


class RefundPolicyType(str, Enum):
    NO_QUESTIONS_ASKED  = "NO_QUESTIONS_ASKED"   # 30/60/90-day full refund
    STANDARD            = "STANDARD"             # Refund if defective/wrong item
    EXCHANGE_ONLY       = "EXCHANGE_ONLY"        # No cash, exchange only
    FINAL_SALE          = "FINAL_SALE"           # No returns at all
    CUSTOM              = "CUSTOM"               # Partial or conditional


class ClaimBasis(str, Enum):
    ITEM_NOT_RECEIVED   = "ITEM_NOT_RECEIVED"
    NOT_AS_DESCRIBED    = "NOT_AS_DESCRIBED"
    DEFECTIVE_ITEM      = "DEFECTIVE_ITEM"
    WRONG_ITEM          = "WRONG_ITEM"
    CHANGED_MIND        = "CHANGED_MIND"
    DUPLICATE_CHARGE    = "DUPLICATE_CHARGE"
    UNAUTHORIZED        = "UNAUTHORIZED"


class PolicyEligibility(str, Enum):
    ELIGIBLE            = "ELIGIBLE"
    BORDERLINE          = "BORDERLINE"
    INELIGIBLE          = "INELIGIBLE"


@dataclass
class RefundPolicyInput:
    """Input for refund policy evaluation."""
    dispute_id: str
    claim_basis: ClaimBasis
    transaction_amount: float
    days_since_purchase: int

    # Merchant policy
    policy_type: RefundPolicyType
    policy_window_days: int = 30     # Return window in days
    policy_covers_changed_mind: bool = False
    policy_partial_pct: float = 1.0  # For CUSTOM policy, what fraction is offered

    # Dispute economics
    fight_cost: float = 25.0
    p_win_if_fight: float = 0.60
    hdr_rate_pct: float = 0.40        # Current rolling 90d rate
    hdr_would_breach: bool = False    # Would auto-loss push into next HDR tier?

    # Buyer context
    buyer_ltv: float = 0.0            # Estimated remaining LTV
    buyer_total_orders: int = 1
    buyer_dispute_rate: float = 0.0   # Fraction of past orders disputed

    # Item context
    item_returned: bool = False
    item_is_digital: bool = False
    item_is_custom: bool = False
    item_resale_value: float = 0.0    # If returned, what can it be resold for

    # Promotions / special context
    was_promo_purchase: bool = False
    promo_non_refundable: bool = False


@dataclass
class RefundRecommendation:
    """Structured refund recommendation."""
    dispute_id: str

    # Decision
    recommended_mode: RefundMode
    recommended_amount: float        # 0 = decline; full or partial amount
    refund_pct: float                # 0.0–1.0

    # Policy assessment
    policy_eligibility: PolicyEligibility
    policy_notes: str

    # Economics
    cost_refund_now: float           # Net cost of refunding immediately
    cost_fight_ev: float             # Expected cost if fighting (negative = profitable)
    economic_advantage: str          # "REFUND" / "FIGHT" / "NEUTRAL"

    # HDR context
    hdr_risk_note: str

    # Rationale
    rationale: str
    conditions: list[str]            # Any conditions on the refund
    summary: str


def _policy_eligibility(inp: RefundPolicyInput) -> tuple[PolicyEligibility, str]:
    """Determine if buyer is within policy."""
    within_window = inp.days_since_purchase <= inp.policy_window_days

    if inp.policy_type == RefundPolicyType.FINAL_SALE:
        return PolicyEligibility.INELIGIBLE, "Final sale — no returns accepted"

    if inp.item_is_custom and inp.claim_basis == ClaimBasis.CHANGED_MIND:
        return PolicyEligibility.INELIGIBLE, "Custom/made-to-order items not eligible for change-of-mind returns"

    if inp.item_is_digital and inp.claim_basis == ClaimBasis.CHANGED_MIND:
        return PolicyEligibility.INELIGIBLE, "Digital goods not eligible for change-of-mind returns"

    if inp.promo_non_refundable and inp.was_promo_purchase:
        return PolicyEligibility.INELIGIBLE, "Promotional purchase marked non-refundable"

    if inp.policy_type == RefundPolicyType.EXCHANGE_ONLY:
        if inp.claim_basis in (ClaimBasis.CHANGED_MIND,):
            return PolicyEligibility.INELIGIBLE, "Policy is exchange-only; change-of-mind not covered"
        return PolicyEligibility.BORDERLINE, "Policy is exchange-only; cash refund requires dispute escalation"

    if inp.policy_type == RefundPolicyType.NO_QUESTIONS_ASKED:
        if within_window:
            return PolicyEligibility.ELIGIBLE, f"Within {inp.policy_window_days}-day no-questions-asked window"
        return PolicyEligibility.INELIGIBLE, f"Outside {inp.policy_window_days}-day return window ({inp.days_since_purchase}d elapsed)"

    # STANDARD policy
    if inp.claim_basis in (ClaimBasis.ITEM_NOT_RECEIVED, ClaimBasis.DUPLICATE_CHARGE,
                            ClaimBasis.UNAUTHORIZED):
        return PolicyEligibility.ELIGIBLE, "Claim type covered by standard policy regardless of window"

    if inp.claim_basis in (ClaimBasis.NOT_AS_DESCRIBED, ClaimBasis.DEFECTIVE_ITEM,
                            ClaimBasis.WRONG_ITEM):
        if within_window:
            return PolicyEligibility.ELIGIBLE, f"Covered by standard policy within {inp.policy_window_days}-day window"
        return PolicyEligibility.BORDERLINE, f"Outside return window but defect/wrong-item claim — may still be covered"

    if inp.claim_basis == ClaimBasis.CHANGED_MIND:
        if inp.policy_covers_changed_mind and within_window:
            return PolicyEligibility.ELIGIBLE, "Policy covers change-of-mind within window"
        return PolicyEligibility.INELIGIBLE, "Change-of-mind not covered by standard policy"

    return PolicyEligibility.BORDERLINE, "Claim assessed individually"


def _economic_calculus(inp: RefundPolicyInput) -> tuple[float, float, str]:
    """
    Returns (cost_refund, cost_fight_ev, advantage).
    cost_refund: net cost of refunding now (amount minus resale recovery if returned)
    cost_fight_ev: expected value of fighting (negative = net gain)
    """
    cost_refund = inp.transaction_amount - (inp.item_resale_value if inp.item_returned else 0.0)

    # EV(fight) = P(win) * 0 - P(lose) * (amount + chargeback_fee) - fight_cost
    chargeback_fee = 20.0  # PayPal standard
    if inp.hdr_rate_pct >= 1.5:
        chargeback_fee = 30.0   # HDR surcharge zone
    cost_fight_ev = (
        inp.p_win_if_fight * inp.fight_cost
        + (1 - inp.p_win_if_fight) * (inp.transaction_amount + chargeback_fee + inp.fight_cost)
    )

    advantage = "NEUTRAL"
    if cost_refund < cost_fight_ev * 0.90:
        advantage = "REFUND"
    elif cost_fight_ev < cost_refund * 0.90:
        advantage = "FIGHT"

    return round(cost_refund, 2), round(cost_fight_ev, 2), advantage


def _hdr_note(inp: RefundPolicyInput) -> str:
    if inp.hdr_would_breach:
        return ("Auto-loss would push dispute rate into next HDR tier — "
                "proactive refund reduces HDR impact")
    if inp.hdr_rate_pct >= 1.0:
        return "Currently in HDR zone — minimizing auto-losses is a priority"
    if inp.hdr_rate_pct >= 0.5:
        return "Approaching HDR threshold — monitor dispute rate"
    return "HDR rate healthy — no special pressure to avoid auto-loss"


def evaluate_refund_policy(inp: RefundPolicyInput) -> RefundRecommendation:
    """
    Evaluate refund policy eligibility and determine optimal refund strategy.
    """
    eligibility, policy_notes = _policy_eligibility(inp)
    cost_refund, cost_fight_ev, economic_adv = _economic_calculus(inp)
    hdr_note = _hdr_note(inp)

    # Determine recommended mode and amount
    conditions: list[str] = []

    # Hard ineligibility with no economic reason to override
    if eligibility == PolicyEligibility.INELIGIBLE:
        if economic_adv == "REFUND" or inp.hdr_would_breach:
            # Economic / HDR pressure overrides policy ineligibility
            mode = RefundMode.PARTIAL_REFUND
            amount = round(inp.transaction_amount * 0.50, 2)
            pct = 0.50
            rationale = ("Policy ineligible but economic analysis favors accommodation; "
                         "offering 50% partial refund as goodwill gesture")
        elif inp.buyer_ltv > inp.transaction_amount * 2 and inp.buyer_dispute_rate < 0.10:
            mode = RefundMode.STORE_CREDIT
            amount = inp.transaction_amount
            pct = 1.0
            rationale = "Policy ineligible but high LTV buyer — offering store credit to retain relationship"
        else:
            mode = RefundMode.DECLINE_REFUND
            amount = 0.0
            pct = 0.0
            rationale = f"Policy ineligible: {policy_notes}; economics favor fighting"

    elif eligibility == PolicyEligibility.BORDERLINE:
        if inp.policy_type == RefundPolicyType.EXCHANGE_ONLY:
            mode = RefundMode.EXCHANGE_ONLY
            amount = 0.0
            pct = 0.0
            rationale = "Policy is exchange-only; offer replacement item"
            conditions.append("Buyer must return original item within 14 days")
        elif economic_adv == "REFUND" or inp.hdr_would_breach:
            mode = RefundMode.FULL_REFUND
            amount = inp.transaction_amount
            pct = 1.0
            rationale = "Borderline eligibility but economics strongly favor refunding"
        else:
            mode = RefundMode.PARTIAL_REFUND
            amount = round(inp.transaction_amount * 0.75, 2)
            pct = 0.75
            rationale = "Borderline eligibility — partial refund as good-faith resolution"

    else:  # ELIGIBLE
        if inp.policy_type == RefundPolicyType.CUSTOM:
            pct = inp.policy_partial_pct
            amount = round(inp.transaction_amount * pct, 2)
            mode = RefundMode.PARTIAL_REFUND if pct < 1.0 else RefundMode.FULL_REFUND
            rationale = f"Custom policy: offering {pct:.0%} refund per policy terms"
        elif inp.policy_type == RefundPolicyType.EXCHANGE_ONLY:
            mode = RefundMode.EXCHANGE_ONLY
            amount = 0.0
            pct = 0.0
            rationale = "Policy is exchange-only; no cash refund available"
        elif inp.claim_basis == ClaimBasis.DUPLICATE_CHARGE:
            mode = RefundMode.FULL_REFUND
            amount = inp.transaction_amount
            pct = 1.0
            rationale = "Duplicate charge — full refund required by policy and regulation"
        elif not inp.item_returned and inp.transaction_amount >= 75:
            mode = RefundMode.CONDITIONAL_REFUND
            amount = inp.transaction_amount
            pct = 1.0
            rationale = "Eligible for full refund upon item return"
            conditions.append("Buyer must ship item back within 10 business days")
            conditions.append("Refund issued within 5 days of confirmed return receipt")
        else:
            mode = RefundMode.FULL_REFUND
            amount = inp.transaction_amount
            pct = 1.0
            rationale = "Buyer is eligible for full refund under current policy"

    summary = (
        f"Refund {inp.dispute_id} [{inp.claim_basis.value}]: "
        f"mode={mode.value} | amount=${amount:.2f} ({pct:.0%}) | "
        f"eligibility={eligibility.value} | econ_adv={economic_adv} | "
        f"cost_refund=${cost_refund:.0f} vs cost_fight=${cost_fight_ev:.0f}"
    )

    return RefundRecommendation(
        dispute_id=inp.dispute_id,
        recommended_mode=mode,
        recommended_amount=amount,
        refund_pct=pct,
        policy_eligibility=eligibility,
        policy_notes=policy_notes,
        cost_refund_now=cost_refund,
        cost_fight_ev=cost_fight_ev,
        economic_advantage=economic_adv,
        hdr_risk_note=hdr_note,
        rationale=rationale,
        conditions=conditions,
        summary=summary,
    )
