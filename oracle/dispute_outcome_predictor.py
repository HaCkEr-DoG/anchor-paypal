"""
Dispute Outcome Predictor — probabilistic outcome distribution for a dispute.

Combines multiple dispute characteristics into a P(WIN), P(PARTIAL_WIN), and
P(LOSE) estimate using an additive logistic scoring model. The probabilities
feed directly into EV calculations (Wave 73 ROI Calculator) and can replace
manual win_probability inputs that merchants currently have to guess.

Scoring factors and their contributions to win probability baseline (0.50):

  reason_code_base:          INR→+0.10  SNAD→-0.05  UNAUTHORIZED→-0.15  SUBSCRIPTION→+0.08
  evidence_coverage (0–1):   full coverage → +0.20  partial → pro-rated
  response_speed:            responded ≤24h → +0.07  25-72h → 0  >72h → -0.10
  transaction_age_days:      ≤7d → +0.05  8-30d → 0  >30d → -0.05
  seller_protection:         eligible → +0.15
  merchant_tier:             GOLD → +0.08  SILVER → +0.03  BRONZE → 0  REVIEW/SUSPEND → -0.10
  buyer_history_flag:        repeat_disputer → -0.08  first_time → 0

Final P(WIN) is clamped 0.05–0.95.

P(PARTIAL_WIN) models a partial refund or reduced settlement outcome:
  - Highest when P(WIN) is in the ambiguous middle (40–60%)
  - Modeled as min(P(WIN), 1-P(WIN)) × 0.60

P(LOSE) = 1 - P(WIN) - P(PARTIAL_WIN)

PayPal benefit:
  - Replaces merchant's subjective win probability input with an objective estimate
  - Outcome predictions can be audited post-resolution to calibrate the model
  - Merchants with low P(WIN) + high amount = highest EV pressure to refund early
  - PayPal can use aggregate predictions to forecast dispute program costs
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MerchantTier(str, Enum):
    GOLD    = "GOLD"
    SILVER  = "SILVER"
    BRONZE  = "BRONZE"
    REVIEW  = "REVIEW"
    SUSPEND = "SUSPEND"


class PredictedOutcome(str, Enum):
    WIN          = "WIN"
    PARTIAL_WIN  = "PARTIAL_WIN"
    LOSE         = "LOSE"


_REASON_CODE_DELTA: dict = {
    "INR":          +0.10,
    "SNAD":         -0.05,
    "UNAUTHORIZED": -0.15,
    "SUBSCRIPTION": +0.08,
}

_TIER_DELTA: dict = {
    MerchantTier.GOLD:    +0.08,
    MerchantTier.SILVER:  +0.03,
    MerchantTier.BRONZE:   0.00,
    MerchantTier.REVIEW:  -0.10,
    MerchantTier.SUSPEND: -0.10,
}

_BASELINE: float = 0.50


@dataclass
class OutcomePredictorInput:
    dispute_id: str
    reason_code: str
    evidence_coverage: float        # 0.0–1.0 (from EvidenceGapDetector coverage_score)
    response_hours: float           # hours until merchant responded (0 if not yet)
    transaction_age_days: int       # days since the original transaction
    seller_protection_eligible: bool
    merchant_tier: MerchantTier = MerchantTier.SILVER
    buyer_is_repeat_disputer: bool = False
    transaction_amount: float = 100.0


@dataclass
class FactorContribution:
    factor: str
    delta: float
    description: str


@dataclass
class OutcomePredictionReport:
    dispute_id: str
    p_win: float
    p_partial_win: float
    p_lose: float
    predicted_outcome: PredictedOutcome
    confidence: str                     # HIGH / MEDIUM / LOW
    factor_contributions: list          # List[FactorContribution]
    win_probability_for_ev: float       # P(WIN) + 0.5*P(PARTIAL_WIN) for EV calc
    paypal_signal: str


def _response_speed_delta(hours: float) -> tuple:
    if hours <= 24:
        return +0.07, "Responded within 24h (+0.07)"
    if hours <= 72:
        return 0.00, "Responded 24–72h (neutral)"
    return -0.10, "Responded after 72h (−0.10)"


def _transaction_age_delta(days: int) -> tuple:
    if days <= 7:
        return +0.05, "Transaction ≤7 days old (+0.05)"
    if days <= 30:
        return 0.00, "Transaction 8–30 days old (neutral)"
    return -0.05, "Transaction >30 days old (−0.05)"


def predict_outcome(inp: OutcomePredictorInput) -> OutcomePredictionReport:
    factors = []

    # Baseline
    score = _BASELINE

    # Reason code
    rc_delta = _REASON_CODE_DELTA.get(inp.reason_code.upper(), 0.0)
    score += rc_delta
    factors.append(FactorContribution(
        factor="reason_code",
        delta=rc_delta,
        description=f"Reason code {inp.reason_code.upper()} base delta: {rc_delta:+.2f}",
    ))

    # Evidence coverage: 0% → 0 bonus; 100% → +0.20
    ev_delta = round(inp.evidence_coverage * 0.20, 4)
    score += ev_delta
    factors.append(FactorContribution(
        factor="evidence_coverage",
        delta=ev_delta,
        description=f"Evidence coverage {inp.evidence_coverage:.0%} → {ev_delta:+.2f}",
    ))

    # Response speed
    rs_delta, rs_desc = _response_speed_delta(inp.response_hours)
    score += rs_delta
    factors.append(FactorContribution(factor="response_speed", delta=rs_delta, description=rs_desc))

    # Transaction age
    age_delta, age_desc = _transaction_age_delta(inp.transaction_age_days)
    score += age_delta
    factors.append(FactorContribution(factor="transaction_age", delta=age_delta, description=age_desc))

    # Seller protection
    sp_delta = +0.15 if inp.seller_protection_eligible else 0.0
    score += sp_delta
    factors.append(FactorContribution(
        factor="seller_protection",
        delta=sp_delta,
        description=f"Seller Protection eligible: {sp_delta:+.2f}" if sp_delta else "No Seller Protection",
    ))

    # Merchant tier
    tier_delta = _TIER_DELTA.get(inp.merchant_tier, 0.0)
    score += tier_delta
    factors.append(FactorContribution(
        factor="merchant_tier",
        delta=tier_delta,
        description=f"Merchant tier {inp.merchant_tier.value}: {tier_delta:+.2f}",
    ))

    # Buyer history
    bh_delta = -0.08 if inp.buyer_is_repeat_disputer else 0.0
    score += bh_delta
    if bh_delta:
        factors.append(FactorContribution(
            factor="buyer_history",
            delta=bh_delta,
            description=f"Buyer is repeat disputer: {bh_delta:+.2f}",
        ))
    else:
        factors.append(FactorContribution(
            factor="buyer_history",
            delta=0.0,
            description="Buyer has no prior dispute flag",
        ))

    # Clamp
    p_win = round(min(0.95, max(0.05, score)), 4)

    # Partial win: highest in ambiguous middle
    ambiguity = min(p_win, 1.0 - p_win)
    p_partial = round(min(0.30, ambiguity * 0.60), 4)

    # Normalize so they sum to 1
    total = p_win + p_partial + (1.0 - p_win - p_partial)
    p_lose = round(1.0 - p_win - p_partial, 4)

    # Predicted outcome = highest probability
    if p_win >= p_partial and p_win >= p_lose:
        outcome = PredictedOutcome.WIN
    elif p_lose >= p_win and p_lose >= p_partial:
        outcome = PredictedOutcome.LOSE
    else:
        outcome = PredictedOutcome.PARTIAL_WIN

    # Confidence
    max_prob = max(p_win, p_partial, p_lose)
    if max_prob >= 0.70:
        confidence = "HIGH"
    elif max_prob >= 0.50:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    # EV-adjusted win probability: gives partial wins half weight
    ev_p_win = round(p_win + 0.5 * p_partial, 4)

    paypal_signal = (
        f"Dispute {inp.dispute_id} [{inp.reason_code.upper()}]: "
        f"P(WIN)={p_win:.0%}, P(PARTIAL)={p_partial:.0%}, P(LOSE)={p_lose:.0%}, "
        f"outcome={outcome.value}, confidence={confidence}."
    )

    return OutcomePredictionReport(
        dispute_id=inp.dispute_id,
        p_win=p_win,
        p_partial_win=p_partial,
        p_lose=p_lose,
        predicted_outcome=outcome,
        confidence=confidence,
        factor_contributions=factors,
        win_probability_for_ev=ev_p_win,
        paypal_signal=paypal_signal,
    )


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# ---------------------------------------------------------------------------
from enum import Enum as _Enum


class DecisionStrength(str, _Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    UNCERTAIN = "UNCERTAIN"


class OutcomeConfidence(str, _Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


predict_dispute_outcome = predict_outcome
FinalAction = PredictedOutcome
