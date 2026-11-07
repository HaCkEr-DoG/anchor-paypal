"""
Refund ROI Calculator — compute the net-value of fighting vs. refunding a dispute.

The classic mistake: merchants fight every dispute to avoid "losing money."
The economic reality: fighting costs (ops time, evidence gathering, fees),
and even winning may cost more than a targeted refund for the right segment.

This module answers: for THIS dispute, what is the net expected value of
each path, and which path maximizes long-term merchant + PayPal value?

Formula used:
  EV(fight) = p_win × (amount - rc) - (1 - p_win) × (amount + cb_fee + rc)
  EV(refund) = -(refund_amount) + LTV_retention_bonus

  where rc = response_cost (staff time + evidence prep)
        cb_fee = chargeback_fee (if buyer escalates to bank)

Segment-aware cost modeling:
  Response cost is different per dispute type:
    - INR: low (tracking + POD = minutes)
    - SNAD: medium (product photos, return coordination)
    - UNAUTHORIZED: high (transaction records, IP logs, fraud team review)
    - SUBSCRIPTION: medium (agreement retrieval, renewal evidence)

PayPal benefit:
  - Identifies disputes where fighting is net-negative for the merchant
    but merchants fight anyway (wasting PayPal's resolution bandwidth)
  - Segments dispute volume into "fight vs. refund" tiers, enabling
    PayPal's seller success team to coach merchants on ROI-driven strategy
  - Flags disputes where BOTH fight AND refund have negative EV (loss leader
    indicator — merchant pricing or ops problem)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DisputeAction(str, Enum):
    FIGHT       = "FIGHT"
    REFUND      = "REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    ESCALATE    = "ESCALATE"      # proactively escalate before buyer does


class ROIConfidence(str, Enum):
    HIGH   = "HIGH"    # strong signal — act on this
    MEDIUM = "MEDIUM"  # reasonable estimate — consider context
    LOW    = "LOW"     # uncertain — human review recommended


class MerchantSegment(str, Enum):
    ENTERPRISE     = "ENTERPRISE"      # high volume, ops team, dedicated staff
    MID_MARKET     = "MID_MARKET"      # moderate volume, part-time ops
    SMALL_BUSINESS = "SMALL_BUSINESS"  # owner-operated, every hour costs
    MICRO          = "MICRO"           # solo seller, minimal ops capacity


@dataclass
class RefundROIInput:
    dispute_id: str
    merchant_id: str

    # Dispute financials
    dispute_amount: float
    reason_code: str            # INR / SNAD / UNAUTHORIZED / SUBSCRIPTION
    win_probability: float      # 0.0 - 1.0 from dispute_probability_forecaster

    # Merchant profile
    merchant_segment: MerchantSegment
    buyer_ltv: float            # estimated lifetime value of this buyer
    is_repeat_buyer: bool = False
    buyer_prior_orders: int = 0

    # Cost overrides (set to 0 to use segment defaults)
    response_cost_override: Optional[float] = None   # staff cost to fight
    chargeback_fee_override: Optional[float] = None  # PayPal CB fee override

    # Refund options
    partial_refund_pct: float = 0.0   # 0.0 = full refund; 0.5 = 50% refund
    refund_goodwill_value: float = 0.0  # estimated LTV retained by refunding


@dataclass
class EVBreakdown:
    gross_win_value: float      # amount saved if merchant wins
    gross_loss_value: float     # amount lost if merchant loses
    response_cost: float        # cost to respond (win or lose)
    chargeback_fee: float       # extra cost if buyer escalates
    expected_value: float       # net EV


@dataclass
class RefundROIReport:
    dispute_id: str
    merchant_id: str

    # Recommended action
    recommended_action: DisputeAction
    confidence: ROIConfidence

    # EV breakdown for each path
    ev_fight: float
    ev_refund: float
    ev_partial_refund: float

    fight_breakdown: EVBreakdown
    refund_breakdown: EVBreakdown

    # Margin analysis
    fight_refund_delta: float       # ev_fight - ev_refund (positive = fight wins)
    is_fight_roi_positive: bool     # is fighting worth the cost at all?
    is_refund_roi_better: bool      # refund wins on pure economics

    # Segment context
    response_cost_used: float
    chargeback_fee_used: float

    # PayPal signal
    paypal_signal: str              # insight for PayPal seller success team

    key_insight: str
    summary: str


# ─── Segment cost tables ──────────────────────────────────────────────────────

_RESPONSE_COSTS: dict[MerchantSegment, dict[str, float]] = {
    MerchantSegment.ENTERPRISE: {
        "INR": 8.0,
        "SNAD": 20.0,
        "UNAUTHORIZED": 35.0,
        "SUBSCRIPTION": 15.0,
        "DEFAULT": 15.0,
    },
    MerchantSegment.MID_MARKET: {
        "INR": 15.0,
        "SNAD": 30.0,
        "UNAUTHORIZED": 55.0,
        "SUBSCRIPTION": 25.0,
        "DEFAULT": 25.0,
    },
    MerchantSegment.SMALL_BUSINESS: {
        "INR": 25.0,
        "SNAD": 50.0,
        "UNAUTHORIZED": 80.0,
        "SUBSCRIPTION": 40.0,
        "DEFAULT": 40.0,
    },
    MerchantSegment.MICRO: {
        "INR": 40.0,
        "SNAD": 75.0,
        "UNAUTHORIZED": 120.0,
        "SUBSCRIPTION": 60.0,
        "DEFAULT": 60.0,
    },
}

_DEFAULT_CHARGEBACK_FEE = 20.0  # typical PayPal chargeback fee


def _response_cost(inp: RefundROIInput) -> float:
    if inp.response_cost_override is not None:
        return inp.response_cost_override
    table = _RESPONSE_COSTS[inp.merchant_segment]
    return table.get(inp.reason_code, table["DEFAULT"])


def _chargeback_fee(inp: RefundROIInput) -> float:
    if inp.chargeback_fee_override is not None:
        return inp.chargeback_fee_override
    return _DEFAULT_CHARGEBACK_FEE


def _ev_fight(inp: RefundROIInput, rc: float, cb: float) -> tuple[float, EVBreakdown]:
    """
    EV(fight) = p_win × (amount − rc) − (1 − p_win) × (amount + cb_fee + rc)

    If the merchant wins: saves the dispute amount, pays the response cost.
    If the merchant loses: pays the full amount + chargeback fee + response cost.
    """
    p = inp.win_probability
    amount = inp.dispute_amount

    gross_win = amount - rc
    gross_loss = amount + cb + rc

    ev = p * gross_win - (1 - p) * gross_loss

    breakdown = EVBreakdown(
        gross_win_value=gross_win,
        gross_loss_value=gross_loss,
        response_cost=rc,
        chargeback_fee=cb,
        expected_value=round(ev, 2),
    )
    return round(ev, 2), breakdown


def _ev_refund(inp: RefundROIInput) -> tuple[float, EVBreakdown]:
    """
    EV(refund) = −refund_amount + ltv_retention_bonus

    If merchant refunds: loses the refund amount but may retain the buyer.
    LTV retention bonus: fraction of LTV saved by keeping the relationship.
    """
    refund_amount = inp.dispute_amount * (1.0 - inp.partial_refund_pct)

    # Retention bonus: if buyer is high LTV, refunding may preserve future revenue
    # Heuristic: 20% of LTV is what a satisfied resolution is worth
    retention_bonus = inp.refund_goodwill_value
    if retention_bonus == 0.0 and inp.buyer_ltv > 0:
        # Auto-estimate: retain 15% of LTV if refund preserves the relationship
        retention_factor = 0.15
        if inp.is_repeat_buyer:
            retention_factor += 0.05
        if inp.buyer_prior_orders >= 5:
            retention_factor += 0.05
        retention_bonus = inp.buyer_ltv * retention_factor

    ev = -refund_amount + retention_bonus

    breakdown = EVBreakdown(
        gross_win_value=retention_bonus,
        gross_loss_value=refund_amount,
        response_cost=0.0,
        chargeback_fee=0.0,
        expected_value=round(ev, 2),
    )
    return round(ev, 2), breakdown


def _ev_partial_refund(inp: RefundROIInput) -> float:
    if inp.partial_refund_pct <= 0.0:
        return _ev_refund(inp)[0]
    partial_input = RefundROIInput(
        dispute_id=inp.dispute_id,
        merchant_id=inp.merchant_id,
        dispute_amount=inp.dispute_amount,
        reason_code=inp.reason_code,
        win_probability=inp.win_probability,
        merchant_segment=inp.merchant_segment,
        buyer_ltv=inp.buyer_ltv,
        is_repeat_buyer=inp.is_repeat_buyer,
        buyer_prior_orders=inp.buyer_prior_orders,
        response_cost_override=inp.response_cost_override,
        chargeback_fee_override=inp.chargeback_fee_override,
        partial_refund_pct=inp.partial_refund_pct,
        refund_goodwill_value=inp.refund_goodwill_value * 0.7,  # partial = less goodwill
    )
    return _ev_refund(partial_input)[0]


def _pick_action(
    ev_fight: float, ev_refund: float, ev_partial: float, win_prob: float,
) -> tuple[DisputeAction, ROIConfidence]:
    """
    Pick the action with the highest EV, with confidence based on margin.
    """
    options = {
        DisputeAction.FIGHT: ev_fight,
        DisputeAction.REFUND: ev_refund,
        DisputeAction.PARTIAL_REFUND: ev_partial,
    }
    best = max(options, key=lambda k: options[k])
    best_ev = options[best]

    # Confidence based on margin between best and second-best
    sorted_evs = sorted(options.values(), reverse=True)
    margin = sorted_evs[0] - sorted_evs[1] if len(sorted_evs) > 1 else abs(best_ev)

    if margin >= 20.0:
        confidence = ROIConfidence.HIGH
    elif margin >= 8.0:
        confidence = ROIConfidence.MEDIUM
    else:
        confidence = ROIConfidence.LOW

    # Override: if win probability is very low (<0.20), demote fight confidence
    if best == DisputeAction.FIGHT and win_prob < 0.20:
        confidence = ROIConfidence.LOW

    return best, confidence


def _paypal_signal(
    inp: RefundROIInput, recommended: DisputeAction,
    ev_fight: float, ev_refund: float,
) -> str:
    if recommended == DisputeAction.REFUND and ev_fight > 0:
        return (
            f"Merchant MERCH={inp.merchant_id} segment={inp.merchant_segment.value}: "
            f"refunding dispute {inp.dispute_id} yields higher EV (+${ev_refund:.2f} vs "
            f"+${ev_fight:.2f} to fight). PayPal seller success: coach on targeted "
            f"refund strategy to reduce resolution bandwidth consumption."
        )
    elif ev_fight < 0 and ev_refund < 0:
        return (
            f"Loss-leader alert for MERCH={inp.merchant_id}: both fight (EV={ev_fight:.2f}) "
            f"and refund (EV={ev_refund:.2f}) are net-negative on dispute {inp.dispute_id}. "
            f"Likely {inp.reason_code} pattern at segment={inp.merchant_segment.value}. "
            f"PayPal risk: investigate for systematic pricing or fulfillment problem."
        )
    elif recommended == DisputeAction.FIGHT:
        return (
            f"Merchant {inp.merchant_id} should fight dispute {inp.dispute_id}: "
            f"EV(fight)={ev_fight:.2f} vs EV(refund)={ev_refund:.2f}. "
            f"Win probability {inp.win_probability:.0%} supports contest."
        )
    else:
        return (
            f"Partial refund recommended for {inp.merchant_id} on {inp.dispute_id}: "
            f"balances buyer retention (LTV={inp.buyer_ltv:.0f}) vs full loss."
        )


# ─── Main function ─────────────────────────────────────────────────────────────

def calculate_refund_roi(inp: RefundROIInput) -> RefundROIReport:
    """
    Compute the net expected value of fighting, refunding, or partial refund.
    Returns the recommended action with full EV breakdown.
    """
    rc = _response_cost(inp)
    cb = _chargeback_fee(inp)

    ev_fight, fight_bd = _ev_fight(inp, rc, cb)
    ev_refund, refund_bd = _ev_refund(inp)
    ev_partial = _ev_partial_refund(inp)

    recommended, confidence = _pick_action(ev_fight, ev_refund, ev_partial, inp.win_probability)

    is_fight_positive = ev_fight > 0
    is_refund_better = ev_refund > ev_fight
    delta = round(ev_fight - ev_refund, 2)

    ps = _paypal_signal(inp, recommended, ev_fight, ev_refund)

    # Key insight
    if recommended == DisputeAction.FIGHT and confidence == ROIConfidence.HIGH:
        key_insight = (
            f"Fight this dispute. EV(fight)=${ev_fight:.2f} vs EV(refund)=${ev_refund:.2f}. "
            f"Win probability {inp.win_probability:.0%} with response cost ${rc:.0f} makes "
            f"fighting significantly more profitable."
        )
    elif recommended == DisputeAction.REFUND:
        key_insight = (
            f"Refund this dispute. Despite {inp.win_probability:.0%} win probability, "
            f"response cost ${rc:.0f} and buyer LTV ${inp.buyer_ltv:.0f} make refunding "
            f"more economical (EV=${ev_refund:.2f} vs fight EV=${ev_fight:.2f})."
        )
    elif recommended == DisputeAction.PARTIAL_REFUND:
        key_insight = (
            f"Partial refund is optimal. Buyer LTV ${inp.buyer_ltv:.0f} and {inp.win_probability:.0%} "
            f"win probability suggest a compromise maximizes long-term merchant value "
            f"(EV=${ev_partial:.2f})."
        )
    elif ev_fight < 0 and ev_refund < 0:
        key_insight = (
            f"Loss-leader dispute: both fight (EV=${ev_fight:.2f}) and refund "
            f"(EV=${ev_refund:.2f}) have negative expected value. Consider preemptive "
            f"policy review for {inp.reason_code} disputes at this price point."
        )
    else:
        key_insight = (
            f"Recommended: {recommended.value}. Fight EV=${ev_fight:.2f}, "
            f"Refund EV=${ev_refund:.2f}. Confidence: {confidence.value}."
        )

    summary = (
        f"ROI {inp.dispute_id}: recommend={recommended.value} confidence={confidence.value} "
        f"ev_fight=${ev_fight:.2f} ev_refund=${ev_refund:.2f} "
        f"win_prob={inp.win_probability:.0%} rc=${rc:.0f} segment={inp.merchant_segment.value}"
    )

    return RefundROIReport(
        dispute_id=inp.dispute_id,
        merchant_id=inp.merchant_id,
        recommended_action=recommended,
        confidence=confidence,
        ev_fight=ev_fight,
        ev_refund=ev_refund,
        ev_partial_refund=ev_partial,
        fight_breakdown=fight_bd,
        refund_breakdown=refund_bd,
        fight_refund_delta=delta,
        is_fight_roi_positive=is_fight_positive,
        is_refund_roi_better=is_refund_better,
        response_cost_used=rc,
        chargeback_fee_used=cb,
        paypal_signal=ps,
        key_insight=key_insight,
        summary=summary,
    )
