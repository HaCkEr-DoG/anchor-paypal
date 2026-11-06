"""
Win Probability Calibrator — combine all available signals into a final
calibrated P(win) for a dispute, incorporating fraud signals, evidence quality,
CE3.0 eligibility, and claim-type base rates.

This module serves as the central aggregator that takes outputs from multiple
other oracle modules and produces a single, well-calibrated win probability
for use in the EV model.

Base win rates (empirical PayPal merchant averages):
  INR + SP eligible     → 0.72
  INR + no SP           → 0.55
  SNAD                  → 0.42
  UNAUTHORIZED + CE3.0  → 0.76 (CE3.0 adds ~18pp)
  UNAUTHORIZED          → 0.58
  DUPLICATE             → 0.78
  SUBSCRIPTION          → 0.60

Adjustment factors (additive):
  Evidence quality score:
    STRONG   (≥80) → +0.12
    ADEQUATE (≥60) → +0.05
    WEAK     (≥40) → 0.00
    POOR     (<40) → −0.10

  Fraud risk level:
    HIGH_RISK   → +0.15
    MEDIUM_RISK → +0.07
    LOW_RISK    → 0.00

  Seller Protection status:
    FULLY_ELIGIBLE       → +0.08
    CONDITIONALLY_ELIGIBLE → +0.03
    INELIGIBLE           → −0.05  (absence hurts for INR)

  Response speed:
    Responded ≤ 2 days   → +0.04
    Responded 3–7 days   → 0.00
    No response yet      → −0.08

  Deadline proximity:
    > 7 days remaining   → 0.00
    4–7 days             → −0.02
    1–3 days             → −0.06
    Overdue              → −0.15

Final probability is clamped to [0.02, 0.97].

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ClaimTypeBase(str, Enum):
    INR_WITH_SP    = "INR_WITH_SP"
    INR_NO_SP      = "INR_NO_SP"
    SNAD           = "SNAD"
    UNAUTHORIZED_CE30 = "UNAUTHORIZED_CE30"
    UNAUTHORIZED   = "UNAUTHORIZED"
    DUPLICATE      = "DUPLICATE"
    SUBSCRIPTION   = "SUBSCRIPTION"
    OTHER          = "OTHER"


@dataclass
class CalibrationFactor:
    """One adjustment factor in the calibration."""
    name: str
    base_value: float    # Starting or current value this factor acts on
    adjustment: float    # Delta to win probability (signed)
    description: str


@dataclass
class WinProbabilityInput:
    """Input for win probability calibration."""
    dispute_id: str
    claim_type: str               # INR / SNAD / UNAUTHORIZED / DUPLICATE / SUBSCRIPTION

    # SP status (from seller_protection_checker)
    sp_eligible: bool = False
    sp_conditionally_eligible: bool = False

    # CE3.0 status (from CE3.0 checker, Visa + UNAUTHORIZED only)
    ce30_eligible: bool = False

    # Evidence quality (from evidence_quality_scorer — 0–100 or category string)
    evidence_score: float = 60.0   # 0–100
    evidence_category: str = "ADEQUATE"  # STRONG / ADEQUATE / WEAK / POOR

    # Fraud signals (from buyer_fraud_detector)
    fraud_risk_level: str = "LOW_RISK"  # HIGH_RISK / MEDIUM_RISK / LOW_RISK

    # Response timing
    merchant_responded: bool = True
    days_until_response: int = 1       # Days from dispute open to merchant response

    # Deadline proximity
    days_remaining_to_respond: int = 10  # Days left before deadline

    # Custom base override (optional — skips claim-type lookup)
    custom_base_p_win: Optional[float] = None


_BASE_RATES: dict[str, float] = {
    "INR_WITH_SP":    0.72,
    "INR_NO_SP":      0.55,
    "INR":            0.55,  # Fallback when SP status unknown
    "SNAD":           0.42,
    "UNAUTHORIZED_CE30": 0.76,
    "UNAUTHORIZED":   0.58,
    "DUPLICATE":      0.78,
    "SUBSCRIPTION":   0.60,
    "OTHER":          0.50,
}


def _base_rate(inp: WinProbabilityInput) -> tuple[float, str]:
    """Return (base_p_win, key_used)."""
    if inp.custom_base_p_win is not None:
        return inp.custom_base_p_win, "CUSTOM"

    ct = inp.claim_type.upper()
    if ct == "INR":
        if inp.sp_eligible:
            return _BASE_RATES["INR_WITH_SP"], "INR_WITH_SP"
        return _BASE_RATES["INR_NO_SP"], "INR_NO_SP"
    if ct == "UNAUTHORIZED":
        if inp.ce30_eligible:
            return _BASE_RATES["UNAUTHORIZED_CE30"], "UNAUTHORIZED_CE30"
        return _BASE_RATES["UNAUTHORIZED"], "UNAUTHORIZED"

    return _BASE_RATES.get(ct, _BASE_RATES["OTHER"]), ct


def _evidence_adjustment(inp: WinProbabilityInput) -> CalibrationFactor:
    cat = inp.evidence_category.upper()
    if cat == "STRONG" or inp.evidence_score >= 80:
        adj = 0.12
        desc = f"Strong evidence quality (score={inp.evidence_score:.0f}) → +12pp"
    elif cat == "ADEQUATE" or inp.evidence_score >= 60:
        adj = 0.05
        desc = f"Adequate evidence quality (score={inp.evidence_score:.0f}) → +5pp"
    elif cat == "WEAK" or inp.evidence_score >= 40:
        adj = 0.00
        desc = f"Weak evidence quality (score={inp.evidence_score:.0f}) → 0pp"
    else:
        adj = -0.10
        desc = f"Poor evidence quality (score={inp.evidence_score:.0f}) → −10pp"
    return CalibrationFactor("evidence_quality", inp.evidence_score, adj, desc)


def _fraud_adjustment(inp: WinProbabilityInput) -> CalibrationFactor:
    fl = inp.fraud_risk_level.upper()
    if fl == "HIGH_RISK":
        adj = 0.15
        desc = "High fraud risk detected → +15pp (buyer likely fabricating)"
    elif fl == "MEDIUM_RISK":
        adj = 0.07
        desc = "Medium fraud risk detected → +7pp (mixed signals)"
    else:
        adj = 0.00
        desc = "Low fraud risk → no adjustment"
    return CalibrationFactor("fraud_risk", 0.0, adj, desc)


def _sp_adjustment(inp: WinProbabilityInput) -> CalibrationFactor:
    ct = inp.claim_type.upper()
    if ct == "SNAD":
        return CalibrationFactor("seller_protection", 0.0, 0.0,
                                 "SP does not apply to SNAD — no adjustment")
    if inp.sp_eligible:
        adj = 0.08
        desc = "SP fully eligible → +8pp (chargeback fee waived + stronger case)"
    elif inp.sp_conditionally_eligible:
        adj = 0.03
        desc = "SP conditionally eligible → +3pp (pending gap resolution)"
    else:
        adj = -0.05
        desc = "SP ineligible → −5pp (no fee protection, weaker position)"
    return CalibrationFactor("seller_protection", 0.0, adj, desc)


def _response_adjustment(inp: WinProbabilityInput) -> CalibrationFactor:
    if not inp.merchant_responded:
        adj = -0.08
        desc = "No merchant response yet → −8pp (weakens case)"
    elif inp.days_until_response <= 2:
        adj = 0.04
        desc = f"Quick response ({inp.days_until_response}d) → +4pp"
    else:
        adj = 0.00
        desc = f"Response in {inp.days_until_response} days → 0pp"
    return CalibrationFactor("response_speed", 0.0, adj, desc)


def _deadline_adjustment(inp: WinProbabilityInput) -> CalibrationFactor:
    d = inp.days_remaining_to_respond
    if d < 0:
        adj = -0.15
        desc = "Deadline overdue → −15pp (critical evidence submission risk)"
    elif d <= 3:
        adj = -0.06
        desc = f"{d}d remaining → −6pp (deadline pressure)"
    elif d <= 7:
        adj = -0.02
        desc = f"{d}d remaining → −2pp (mild deadline pressure)"
    else:
        adj = 0.00
        desc = f"{d}d remaining → 0pp (comfortable timeline)"
    return CalibrationFactor("deadline_proximity", d, adj, desc)


@dataclass
class CalibratedWinProbability:
    """Fully calibrated win probability report."""
    dispute_id: str

    base_p_win: float              # Starting value from base rates
    base_rate_key: str             # Which base rate was used

    factors: list[CalibrationFactor]
    total_adjustment: float        # Sum of all factor adjustments

    final_p_win: float             # Clamped final probability
    confidence_band_low: float     # 90% confidence interval low
    confidence_band_high: float    # 90% confidence interval high

    # Key drivers
    strongest_positive_factor: str
    strongest_negative_factor: str

    # Guidance
    fight_recommended: bool        # True if P(win) makes fighting economical
    fight_threshold: float         # Breakeven P(win) for context

    summary: str


def calibrate_win_probability(
        inp: WinProbabilityInput,
        fight_threshold: float = 0.45) -> CalibratedWinProbability:
    """
    Produce a calibrated win probability by combining all available signals.
    """
    base_p, base_key = _base_rate(inp)

    factors = [
        _evidence_adjustment(inp),
        _fraud_adjustment(inp),
        _sp_adjustment(inp),
        _response_adjustment(inp),
        _deadline_adjustment(inp),
    ]

    total_adj = sum(f.adjustment for f in factors)
    raw_final = base_p + total_adj
    final_p   = round(max(0.02, min(0.97, raw_final)), 4)

    # Confidence band: ±10pp typical calibration uncertainty
    uncertainty = 0.10
    band_low  = round(max(0.0, final_p - uncertainty), 4)
    band_high = round(min(1.0, final_p + uncertainty), 4)

    # Strongest drivers
    positive_factors = [(f.name, f.adjustment) for f in factors if f.adjustment > 0]
    negative_factors = [(f.name, f.adjustment) for f in factors if f.adjustment < 0]
    strongest_pos = max(positive_factors, key=lambda x: x[1])[0] if positive_factors else "none"
    strongest_neg = min(negative_factors, key=lambda x: x[1])[0] if negative_factors else "none"

    fight_rec = final_p >= fight_threshold

    summary = (
        f"WinProb {inp.dispute_id}: "
        f"base={base_p:.0%} ({base_key}) | adj={total_adj:+.0%} | "
        f"final={final_p:.0%} [{band_low:.0%}–{band_high:.0%}] | "
        f"fight={'YES' if fight_rec else 'NO'}"
    )

    return CalibratedWinProbability(
        dispute_id=inp.dispute_id,
        base_p_win=base_p,
        base_rate_key=base_key,
        factors=factors,
        total_adjustment=round(total_adj, 4),
        final_p_win=final_p,
        confidence_band_low=band_low,
        confidence_band_high=band_high,
        strongest_positive_factor=strongest_pos,
        strongest_negative_factor=strongest_neg,
        fight_recommended=fight_rec,
        fight_threshold=fight_threshold,
        summary=summary,
    )
