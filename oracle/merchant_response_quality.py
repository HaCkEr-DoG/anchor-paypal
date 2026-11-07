"""
Merchant Response Quality Scorer — grade a dispute response before submission.

A merchant's rebuttal quality directly determines win probability. This oracle
scores a draft response across five evidence dimensions and predicts whether the
response is strong enough to win at PayPal's Resolution Center.

Scoring dimensions (each 0–1, then weighted):
  1. tracking_evidence     (0.25) — has tracking number + delivery confirmation
  2. communication_log     (0.20) — has buyer messages / emails documented
  3. policy_citation       (0.20) — explicitly cites merchant's return/refund policy
  4. product_description   (0.20) — describes product accurately and in detail
  5. photo_evidence        (0.15) — includes photos / videos / screenshots

Overall quality score = weighted sum (0–1).
Win probability estimate based on score + dispute reason base rates.

PayPal benefit:
  - Higher-quality responses reduce back-and-forth review cycles
  - Better-prepared merchants resolve disputes faster, reducing case-open time
  - Quality signals help PayPal's ML models score case complexity
  - Merchants who score poorly get specific coaching, reducing repeat errors
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_W_TRACKING    = 0.25
_W_COMMS       = 0.20
_W_POLICY      = 0.20
_W_PRODUCT     = 0.20
_W_PHOTO       = 0.15

# Base win-rate by dispute reason (conservative estimates)
_BASE_WIN_RATES = {
    "INR":    0.55,  # Item Not Received — tracking evidence usually wins
    "SNAD":   0.35,  # Significantly Not As Described — harder for merchant
    "UNAUTH": 0.20,  # Unauthorized — almost always buyer wins
    "OTHER":  0.40,
}

_STRONG_THRESHOLD  = 0.75
_ADEQUATE_THRESHOLD = 0.50


class ResponseGrade(str, Enum):
    STRONG   = "STRONG"    # ready to submit
    ADEQUATE = "ADEQUATE"  # will likely win but could be better
    WEAK     = "WEAK"      # high risk — needs improvement before submission


@dataclass
class ResponseQualityInput:
    dispute_id: str
    dispute_reason: str                 # INR, SNAD, UNAUTH, OTHER
    has_tracking_number: bool
    has_delivery_confirmation: bool
    has_buyer_communications: bool      # emails, chat logs, messages
    has_policy_citation: bool           # explicitly cites refund/return policy
    product_description_completeness: float   # 0–1, merchant self-assessment
    has_photo_or_video_evidence: bool
    response_word_count: int            # rough proxy for thoroughness
    days_until_deadline: int


@dataclass
class ResponseQualityReport:
    dispute_id: str
    tracking_score: float
    communication_score: float
    policy_score: float
    product_score: float
    photo_score: float
    overall_score: float
    grade: ResponseGrade
    estimated_win_probability: float
    missing_items: list
    recommendations: list
    paypal_signal: str


def score_response(inp: ResponseQualityInput) -> ResponseQualityReport:
    # Dimension scores
    track_score = 1.0 if (inp.has_tracking_number and inp.has_delivery_confirmation) \
                  else (0.5 if inp.has_tracking_number else 0.0)
    comm_score  = 1.0 if inp.has_buyer_communications else 0.0
    policy_score = 1.0 if inp.has_policy_citation else 0.0
    prod_score  = round(max(0.0, min(1.0, inp.product_description_completeness)), 4)
    photo_score = 1.0 if inp.has_photo_or_video_evidence else 0.0

    # Slight word-count bonus (caps at 1.0)
    if inp.response_word_count >= 300:
        prod_score = min(1.0, prod_score + 0.10)

    overall = round(
        _W_TRACKING * track_score
        + _W_COMMS  * comm_score
        + _W_POLICY * policy_score
        + _W_PRODUCT * prod_score
        + _W_PHOTO  * photo_score,
        4,
    )

    grade = (
        ResponseGrade.STRONG if overall >= _STRONG_THRESHOLD
        else ResponseGrade.ADEQUATE if overall >= _ADEQUATE_THRESHOLD
        else ResponseGrade.WEAK
    )

    # Estimated win probability: base rate × scaled by score quality
    base = _BASE_WIN_RATES.get(inp.dispute_reason.upper(), _BASE_WIN_RATES["OTHER"])
    score_multiplier = 0.5 + overall   # maps [0,1] → [0.5, 1.5]
    win_prob = round(min(0.95, base * score_multiplier), 4)

    # Missing items
    missing = []
    if not inp.has_tracking_number:
        missing.append("Tracking number")
    if not inp.has_delivery_confirmation:
        missing.append("Delivery confirmation")
    if not inp.has_buyer_communications:
        missing.append("Buyer communication log")
    if not inp.has_policy_citation:
        missing.append("Refund/return policy citation")
    if inp.product_description_completeness < 0.70:
        missing.append("Detailed product description")
    if not inp.has_photo_or_video_evidence:
        missing.append("Photo or video evidence")

    # Actionable recommendations
    recs = []
    if not inp.has_tracking_number:
        recs.append("Add the shipment tracking number and carrier name.")
    elif not inp.has_delivery_confirmation:
        recs.append("Attach the carrier delivery confirmation screenshot.")
    if not inp.has_buyer_communications:
        recs.append("Include all relevant buyer–seller messages and emails.")
    if not inp.has_policy_citation:
        recs.append("Quote your return/refund policy and state the buyer agreed to it at checkout.")
    if inp.product_description_completeness < 0.70:
        recs.append("Expand product description: include specs, photos, and listing URL.")
    if not inp.has_photo_or_video_evidence:
        recs.append("Attach photos of the item before shipping or a packing video.")
    if inp.response_word_count < 150:
        recs.append("Response is too brief — aim for 200+ words with specific details.")
    if inp.days_until_deadline <= 2 and grade == ResponseGrade.WEAK:
        recs.insert(0, "URGENT: deadline in ≤2 days — submit what you have immediately, then supplement.")

    paypal_signal = (
        f"Dispute {inp.dispute_id}: response_grade={grade.value}, "
        f"quality={overall:.2f}, win_prob={win_prob:.0%}, "
        f"reason={inp.dispute_reason}, missing={len(missing)} items."
    )

    return ResponseQualityReport(
        dispute_id=inp.dispute_id,
        tracking_score=track_score,
        communication_score=comm_score,
        policy_score=policy_score,
        product_score=prod_score,
        photo_score=photo_score,
        overall_score=overall,
        grade=grade,
        estimated_win_probability=win_prob,
        missing_items=missing,
        recommendations=recs,
        paypal_signal=paypal_signal,
    )
