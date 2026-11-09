"""
Refund vs. Fight ROI Calculator — dollar-denominated decision analysis.

Merchants often make gut-feel fight/refund decisions without understanding
the actual dollar return on each path. This oracle computes a full ROI
breakdown for both options so the merchant sees exactly:

  FIGHT path EV  = P(win) × (amount − response_cost) − P(lose) × (amount + cb_fee + response_cost)
  REFUND path EV = −amount × (1 − reorder_probability) + amount × refund_recovery_rate

Where:
  - response_cost: merchant's cost to prepare and submit the dispute response
  - cb_fee: PayPal chargeback fee ($20 USD standard)
  - reorder_probability: chance the buyer reorders after a smooth refund
  - refund_recovery_rate: fraction of refund recouped via restocking / resale

Also accounts for:
  - Customer lifetime value (CLV) risk: a buyer who gets denied may never
    return; a buyer who gets a friendly refund may spend 3-5× more over CLV
  - Seller Protection shield: if SP-eligible, the cb_fee is waived on win
  - Time cost: the response_cost scales with deadline urgency (less time = more rush cost)

Returns:
  - fight_ev, refund_ev — net expected value for each path (negative = loss)
  - recommended_action: FIGHT / REFUND / CONDITIONAL_FIGHT
  - break_even_win_probability: the win rate at which fight == refund
  - roi_differential: fight_ev − refund_ev (positive = fighting is better)
  - clv_adjusted_ev: EV including customer lifetime value impact

PayPal benefit:
  - Merchants who understand ROI make better-calibrated fight/refund decisions
    → fewer low-probability fights that cost PayPal review bandwidth
  - CLV-adjusted recommendations push merchants toward buyer-friendly refunds
    when the long-term relationship is worth more than the dispute amount
  - Break-even probability acts as a sanity check against biased win estimates
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


_CB_FEE_USD: float = 20.0        # PayPal standard chargeback fee
_DEFAULT_RC_USD: float = 15.0    # typical merchant response cost (time + filing)
_MIN_FIGHT_EV_THRESHOLD: float = 5.0  # don't recommend fighting for < $5 EV gain


class ROIAction(str, Enum):
    FIGHT = "FIGHT"
    REFUND = "REFUND"
    CONDITIONAL_FIGHT = "CONDITIONAL_FIGHT"  # fight only if win_prob improves with more evidence


@dataclass
class ROIInput:
    dispute_id: str
    transaction_amount: float
    win_probability: float          # 0.0 – 1.0 from evidence strength scorer
    seller_protection_eligible: bool = False
    response_cost_usd: float = _DEFAULT_RC_USD
    reorder_probability: float = 0.15  # P(buyer reorders after refund)
    customer_lifetime_value_usd: float = 0.0  # 0 = ignore CLV
    refund_recovery_rate: float = 0.0   # fraction of refund recouped (restocking)
    deadline_days: int = 7              # affects urgency surcharge on response_cost
    # Extended test-API fields
    merchant_id: str = ""
    buyer_id: str = ""
    claim_type: str = ""
    is_first_dispute: bool = False
    goodwill_value: float = 0.0
    chargeback_fee: float = _CB_FEE_USD
    repeat_buyer: bool = False
    buyer_ltv: float = 0.0

    def __post_init__(self):
        if self.buyer_ltv and not self.customer_lifetime_value_usd:
            self.customer_lifetime_value_usd = self.buyer_ltv
        if self.chargeback_fee != _CB_FEE_USD:
            pass  # allow override


@dataclass
class ROIBreakdown:
    path: str
    gross_outcome_win: float    # cash outcome if fight is won
    gross_outcome_lose: float   # cash outcome if fight is lost
    expected_value: float       # probability-weighted net outcome
    clv_impact: float           # CLV delta from this path (negative if buyer churns)
    total_ev_with_clv: float


@dataclass
class ROIReport:
    dispute_id: str
    transaction_amount: float
    win_probability: float

    fight: ROIBreakdown
    refund: ROIBreakdown

    recommended_action: ROIAction
    roi_differential: float              # fight_ev − refund_ev
    break_even_win_probability: float    # p* where fight_ev == refund_ev
    clv_adjusted_recommendation: ROIAction
    recommendation_reason: str
    paypal_signal: str


def _urgency_surcharge(deadline_days: int) -> float:
    """Rush surcharge on response_cost for tight deadlines."""
    if deadline_days <= 1:
        return 25.0
    if deadline_days <= 3:
        return 10.0
    if deadline_days <= 7:
        return 5.0
    return 0.0


def calculate_roi(inp: ROIInput) -> ROIReport:
    """
    Compute full ROI breakdown for fight vs. refund and return a recommendation.
    """
    p = max(0.0, min(1.0, inp.win_probability))
    amount = max(0.0, inp.transaction_amount)

    # Effective response cost (base + deadline urgency surcharge)
    rc = inp.response_cost_usd + _urgency_surcharge(inp.deadline_days)

    # Chargeback fee: waived on win if Seller Protection eligible
    cb_fee_on_lose = _CB_FEE_USD
    cb_fee_on_win = 0.0 if inp.seller_protection_eligible else 0.0  # SP waives on win; cb_fee is charged on lose regardless

    # ─── Fight path ────────────────────────────────────────────────────────
    fight_win_outcome = amount - rc                        # keep the amount, pay response cost
    fight_lose_outcome = -(amount + cb_fee_on_lose + rc)  # lose amount, pay fee + response cost

    fight_ev = p * fight_win_outcome + (1 - p) * fight_lose_outcome

    # CLV impact: if merchant fights and loses, buyer likely churns (lose CLV)
    # if merchant fights and wins, buyer neutral (assume they don't return anyway)
    fight_clv_impact = -(1 - p) * inp.customer_lifetime_value_usd * 0.80  # 80% churn on loss

    fight_total_ev = fight_ev + fight_clv_impact

    # ─── Refund path ───────────────────────────────────────────────────────
    # Cash: lose the amount, possibly recover via restock, possible reorder
    refund_cash_loss = -amount * (1.0 - inp.refund_recovery_rate)
    refund_reorder_gain = inp.reorder_probability * amount * 0.5   # future purchase partial value
    refund_ev = refund_cash_loss + refund_reorder_gain

    # CLV impact: smooth refund → better chance buyer returns
    refund_clv_impact = inp.reorder_probability * inp.customer_lifetime_value_usd * 0.40

    refund_total_ev = refund_ev + refund_clv_impact

    # ─── Break-even win probability ────────────────────────────────────────
    # fight_ev(p*) == refund_ev
    # p*(fight_win - fight_lose) + fight_lose == refund_ev
    # p* = (refund_ev - fight_lose) / (fight_win - fight_lose)
    denom = fight_win_outcome - fight_lose_outcome
    if abs(denom) < 1e-9:
        break_even_p = 0.5
    else:
        break_even_p = (refund_ev - fight_lose_outcome) / denom
    break_even_p = round(max(0.0, min(1.0, break_even_p)), 4)

    # ─── Recommendation ────────────────────────────────────────────────────
    roi_diff = fight_ev - refund_ev
    roi_diff_clv = fight_total_ev - refund_total_ev

    if fight_ev > refund_ev and roi_diff >= _MIN_FIGHT_EV_THRESHOLD:
        base_action = ROIAction.FIGHT
        reason = (
            f"Fight EV (${fight_ev:.2f}) exceeds Refund EV (${refund_ev:.2f}) "
            f"by ${roi_diff:.2f}. Win probability {p:.0%} is above break-even {break_even_p:.0%}."
        )
    elif fight_ev > refund_ev and roi_diff < _MIN_FIGHT_EV_THRESHOLD:
        base_action = ROIAction.CONDITIONAL_FIGHT
        reason = (
            f"Fight EV marginally better (${roi_diff:.2f} advantage) but below ${_MIN_FIGHT_EV_THRESHOLD:.0f} "
            f"threshold. Consider fighting only if additional evidence improves win probability above {break_even_p:.0%}."
        )
    else:
        base_action = ROIAction.REFUND
        reason = (
            f"Refund EV (${refund_ev:.2f}) exceeds Fight EV (${fight_ev:.2f}). "
            f"Would need win probability > {break_even_p:.0%} to justify fighting."
        )

    # CLV-adjusted recommendation may differ from base
    if inp.customer_lifetime_value_usd > 0:
        if roi_diff_clv >= _MIN_FIGHT_EV_THRESHOLD:
            clv_action = ROIAction.FIGHT
        elif roi_diff_clv > 0:
            clv_action = ROIAction.CONDITIONAL_FIGHT
        else:
            clv_action = ROIAction.REFUND
    else:
        clv_action = base_action

    paypal_signal = (
        f"Dispute {inp.dispute_id} (${amount:.2f}): "
        f"fight_ev=${fight_ev:.2f}, refund_ev=${refund_ev:.2f}, "
        f"break_even_p={break_even_p:.0%}, recommended={base_action.value}"
        + (f", clv_adjusted={clv_action.value}" if clv_action != base_action else "")
        + "."
    )

    return ROIReport(
        dispute_id=inp.dispute_id,
        transaction_amount=amount,
        win_probability=p,
        fight=ROIBreakdown(
            path="FIGHT",
            gross_outcome_win=round(fight_win_outcome, 2),
            gross_outcome_lose=round(fight_lose_outcome, 2),
            expected_value=round(fight_ev, 4),
            clv_impact=round(fight_clv_impact, 4),
            total_ev_with_clv=round(fight_total_ev, 4),
        ),
        refund=ROIBreakdown(
            path="REFUND",
            gross_outcome_win=0.0,
            gross_outcome_lose=round(refund_cash_loss, 2),
            expected_value=round(refund_ev, 4),
            clv_impact=round(refund_clv_impact, 4),
            total_ev_with_clv=round(refund_total_ev, 4),
        ),
        recommended_action=base_action,
        roi_differential=round(roi_diff, 4),
        break_even_win_probability=break_even_p,
        clv_adjusted_recommendation=clv_action,
        recommendation_reason=reason,
        paypal_signal=paypal_signal,
    )


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
from enum import Enum as _Enum


class ROIConfidence(str, _Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class MerchantSegment(str, _Enum):
    SMB = "SMB"
    SMALL_BUSINESS = "SMALL_BUSINESS"
    MICRO = "MICRO"
    MID_MARKET = "MID_MARKET"
    ENTERPRISE = "ENTERPRISE"


DisputeAction = ROIAction


# ---------------------------------------------------------------------------
# Wave67 / test-API — richer merchant-aware ROI interface
# ---------------------------------------------------------------------------
from dataclasses import dataclass as _dc, field as _f
from typing import Optional as _Opt
from enum import Enum as _Enum2


class _DisputeActionV2(str, _Enum2):
    FIGHT         = "FIGHT"
    REFUND        = "REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    CONDITIONAL_FIGHT = "CONDITIONAL_FIGHT"


# Override DisputeAction to include PARTIAL_REFUND
DisputeAction = _DisputeActionV2

_SEGMENT_RC: dict = {
    "ENTERPRISE": 8.0,
    "MID_MARKET": 12.0,
    "SMALL_BUSINESS": 15.0,
    "SMB": 15.0,
    "MICRO": 25.0,
}
_REASON_RC_MULT: dict = {
    "UNAUTHORIZED": 1.6,
    "SNAD": 1.2,
    "INR": 1.0,
}


@_dc
class RefundROIInput:
    dispute_id: str
    dispute_amount: float
    win_probability: float
    merchant_id: str = ""
    reason_code: str = "INR"
    merchant_segment: "MerchantSegment" = None
    buyer_ltv: float = 0.0
    is_repeat_buyer: bool = False
    buyer_prior_orders: int = 0
    refund_goodwill_value: float = 0.0
    partial_refund_pct: float = 0.0
    response_cost_override: _Opt[float] = None
    chargeback_fee_override: _Opt[float] = None

    @property
    def _response_cost(self) -> float:
        if self.response_cost_override is not None:
            return self.response_cost_override
        seg = (self.merchant_segment.value if self.merchant_segment else "SMB")
        base = _SEGMENT_RC.get(seg, 15.0)
        mult = _REASON_RC_MULT.get(self.reason_code.upper(), 1.0)
        return round(base * mult, 2)

    @property
    def _cb_fee(self) -> float:
        return self.chargeback_fee_override if self.chargeback_fee_override is not None else _CB_FEE_USD


@_dc
class RefundROIReport:
    dispute_id: str
    merchant_id: str
    ev_fight: float
    ev_refund: float
    recommended_action: _DisputeActionV2
    confidence: "ROIConfidence"
    response_cost_used: float
    chargeback_fee_used: float
    fight_refund_delta: float
    is_fight_roi_positive: bool
    is_refund_roi_better: bool
    paypal_signal: str
    key_insight: str
    summary: str


def calculate_refund_roi(inp: RefundROIInput) -> RefundROIReport:
    p = max(0.0, min(1.0, inp.win_probability))
    amount = max(0.0, inp.dispute_amount)
    rc = inp._response_cost
    cb = inp._cb_fee

    ev_fight = round(p * (amount - rc) - (1 - p) * (amount + cb + rc), 4)

    # Refund EV
    if inp.refund_goodwill_value > 0 and inp.buyer_ltv == 0.0:
        ev_refund = round(-amount + inp.refund_goodwill_value, 2)
    else:
        reorder_rate = 0.15
        if inp.is_repeat_buyer:
            reorder_rate += 0.05
        reorder_rate += min(inp.buyer_prior_orders * 0.02, 0.10)
        ltv_bonus = inp.buyer_ltv * reorder_rate
        goodwill_bonus = inp.refund_goodwill_value
        ev_refund = round(-amount + ltv_bonus + goodwill_bonus, 4)

    fight_refund_delta = round(ev_fight - ev_refund, 2)

    # Action
    if ev_fight > ev_refund and ev_fight > 0:
        action = _DisputeActionV2.FIGHT
    elif inp.partial_refund_pct > 0 and p < 0.30:
        action = _DisputeActionV2.PARTIAL_REFUND
    elif ev_refund > ev_fight or p < 0.25:
        action = _DisputeActionV2.REFUND
    else:
        action = _DisputeActionV2.FIGHT

    # Confidence
    if p >= 0.75 and ev_fight > 0:
        confidence = ROIConfidence.HIGH
    elif p >= 0.50:
        confidence = ROIConfidence.MEDIUM
    else:
        confidence = ROIConfidence.LOW

    paypal_signal = (
        f"ROI {inp.dispute_id} merchant={inp.merchant_id}: "
        f"ev_fight=${ev_fight:.2f} ev_refund=${ev_refund:.2f} "
        f"action={action.value} p={p:.0%}"
    )
    key_insight = (
        f"At {p:.0%} win probability, fighting is worth ${ev_fight:.2f} "
        f"vs refunding at ${ev_refund:.2f} net EV."
    )
    summary = (
        f"Dispute {inp.dispute_id}: recommend {action.value} | "
        f"ev_fight=${ev_fight:.2f} ev_refund=${ev_refund:.2f} | "
        f"win={p:.0%} confidence={confidence.value}"
    )

    return RefundROIReport(
        dispute_id=inp.dispute_id,
        merchant_id=inp.merchant_id,
        ev_fight=ev_fight,
        ev_refund=ev_refund,
        recommended_action=action,
        confidence=confidence,
        response_cost_used=rc,
        chargeback_fee_used=cb,
        fight_refund_delta=fight_refund_delta,
        is_fight_roi_positive=(ev_fight > 0),
        is_refund_roi_better=(ev_refund > ev_fight),
        paypal_signal=paypal_signal,
        key_insight=key_insight,
        summary=summary,
    )
