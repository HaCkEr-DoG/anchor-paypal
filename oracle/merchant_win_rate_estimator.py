"""
Merchant Win Rate Estimator — estimate the probability that a merchant
will win a dispute given all available signals.

The estimator uses a base win rate by dispute type, then applies
additive/multiplicative adjustments from:
  - Evidence strength (fulfillment, identity, product)
  - Seller Protection eligibility
  - CE3.0 applicability
  - Network / reason code characteristics
  - Buyer behavior signals
  - Refund policy quality
  - HDR tier (network favors merchants with clean HDR)

Output: a probability (0.0–1.0) with confidence and a breakdown.

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ConfidenceLevel(str, Enum):
    HIGH    = "HIGH"    # Many signals available, narrow CI
    MEDIUM  = "MEDIUM"  # Moderate data
    LOW     = "LOW"     # Few signals — wide uncertainty


class WinLikelihood(str, Enum):
    VERY_LIKELY    = "VERY_LIKELY"    # ≥ 75%
    LIKELY         = "LIKELY"         # 60–74%
    UNCERTAIN      = "UNCERTAIN"      # 40–59%
    UNLIKELY       = "UNLIKELY"       # 25–39%
    VERY_UNLIKELY  = "VERY_UNLIKELY"  # < 25%


# Base win rates by dispute reason code (from industry data)
_BASE_WIN_RATE: dict[str, float] = {
    # PayPal
    "INR":           0.52,
    "SNAD":          0.38,
    "UNAUTHORIZED":  0.45,
    "SUBSCRIPTION":  0.55,
    # Visa
    "10.4":          0.40,
    "13.1":          0.50,
    "13.3":          0.35,
    "12.6":          0.65,
    "12.7":          0.60,
    # Mastercard
    "4853":          0.42,
    "4863":          0.38,
    "4834":          0.62,
    "4807":          0.70,
    # Amex
    "C02":           0.55,
    "C08":           0.50,
    "C14":           0.58,
    "C31":           0.35,
    "FR2":           0.38,
    "FR6":           0.52,
    # Discover
    "AP":            0.60,
    "NF":            0.48,
    "RG":            0.50,
    "RM":            0.40,
}


@dataclass
class WinRateInput:
    """All signals used to estimate win probability."""
    dispute_id: str
    reason_code: str
    network: str
    transaction_amount: float

    # From evidence_package_builder (strength enum as string)
    evidence_strength: Optional[str] = None      # COMPELLING/STRONG/ADEQUATE/WEAK/INSUFFICIENT

    # From seller_protection_checker
    seller_protection_eligible: bool = False
    sp_eligibility: Optional[str] = None         # ELIGIBLE/ELIGIBLE_WITH_GAPS/PARTIAL/INELIGIBLE

    # CE3.0 (Visa unauthorized only)
    ce30_applicable: bool = False

    # From fulfillment
    has_delivery_confirmation: bool = False
    has_signature_confirmation: bool = False
    tracking_shows_delivered: bool = False

    # From refund_policy_advisor
    policy_defensibility: Optional[str] = None   # STRONG/ADEQUATE/WEAK/UNUSABLE

    # From buyer_communication_analyzer
    fraud_signal_level: Optional[str] = None     # VERY_HIGH/HIGH/MODERATE/LOW/MINIMAL
    buyer_threatened_legal: bool = False

    # From dispute history
    merchant_prior_wins_on_similar: int = 0
    buyer_prior_disputes: int = 0

    # HDR context
    hdr_tier: Optional[str] = None               # NORMAL/ELEVATED/LIMITED/FEE_TIER/SEVERE


@dataclass
class WinRateFactor:
    factor_name: str
    adjustment: float    # Signed additive adjustment to win rate
    rationale: str


@dataclass
class WinRateEstimate:
    """Estimated win probability with breakdown."""
    dispute_id: str
    reason_code: str

    base_win_rate: float          # Baseline before adjustments
    adjusted_win_rate: float      # After all factors (clipped 0–1)
    confidence: ConfidenceLevel
    win_likelihood: WinLikelihood

    factors: list[WinRateFactor]  # Each factor's contribution
    positive_factors: list[str]
    negative_factors: list[str]

    expected_value: float         # EV = p_win × amount − fight_cost − (1−p) × cb_fee
    # Uses fight_cost=15, cb_fee=20 defaults
    recommendation: str
    summary: str


_EVIDENCE_ADJUSTMENTS: dict[str, float] = {
    "COMPELLING":    +0.18,
    "STRONG":        +0.10,
    "ADEQUATE":      +0.02,
    "WEAK":          -0.08,
    "INSUFFICIENT":  -0.20,
}

_SP_ADJUSTMENTS: dict[str, float] = {
    "ELIGIBLE":           +0.12,
    "ELIGIBLE_WITH_GAPS": +0.07,
    "PARTIAL":            +0.03,
    "INELIGIBLE":         -0.05,
}

_POLICY_ADJUSTMENTS: dict[str, float] = {
    "STRONG":   +0.08,
    "ADEQUATE": +0.02,
    "WEAK":     -0.08,
    "UNUSABLE": -0.15,
}

_FRAUD_SIGNAL_ADJUSTMENTS: dict[str, float] = {
    "VERY_HIGH": +0.05,  # Buyer has fraud signals → better for merchant
    "HIGH":      +0.03,
    "MODERATE":  +0.01,
    "LOW":        0.00,
    "MINIMAL":    0.00,
}


def _win_likelihood(p: float) -> WinLikelihood:
    if p >= 0.75:
        return WinLikelihood.VERY_LIKELY
    if p >= 0.60:
        return WinLikelihood.LIKELY
    if p >= 0.40:
        return WinLikelihood.UNCERTAIN
    if p >= 0.25:
        return WinLikelihood.UNLIKELY
    return WinLikelihood.VERY_UNLIKELY


def estimate_win_rate(inp: WinRateInput) -> WinRateEstimate:
    """Estimate merchant win probability for a dispute."""
    base = _BASE_WIN_RATE.get(inp.reason_code, 0.45)
    factors: list[WinRateFactor] = []
    pos_factors: list[str] = []
    neg_factors: list[str] = []
    signal_count = 0

    # ── Evidence strength ────────────────────────────────────────
    if inp.evidence_strength:
        adj = _EVIDENCE_ADJUSTMENTS.get(inp.evidence_strength, 0.0)
        factors.append(WinRateFactor("Evidence strength", adj,
                                      f"Evidence rated {inp.evidence_strength}"))
        signal_count += 1
        if adj > 0:
            pos_factors.append(f"Strong evidence package ({inp.evidence_strength})")
        elif adj < 0:
            neg_factors.append(f"Weak evidence ({inp.evidence_strength})")

    # ── Seller Protection ────────────────────────────────────────
    if inp.sp_eligibility:
        adj = _SP_ADJUSTMENTS.get(inp.sp_eligibility, 0.0)
        factors.append(WinRateFactor("Seller Protection", adj,
                                      f"SP status: {inp.sp_eligibility}"))
        signal_count += 1
        if adj > 0:
            pos_factors.append(f"Seller Protection {inp.sp_eligibility.lower()}")
        elif adj < 0:
            neg_factors.append("Not eligible for Seller Protection")
    elif inp.seller_protection_eligible:
        adj = 0.10
        factors.append(WinRateFactor("Seller Protection", adj, "SP eligible"))
        signal_count += 1
        pos_factors.append("Seller Protection eligible")

    # ── CE3.0 ────────────────────────────────────────────────────
    if inp.ce30_applicable:
        adj = +0.18
        factors.append(WinRateFactor("CE3.0 (Compelling Evidence 3.0)", adj,
                                      "Prior non-disputed transactions qualify for CE3.0 boost"))
        signal_count += 1
        pos_factors.append("CE3.0 applicable — +18pp win boost")

    # ── Fulfillment ──────────────────────────────────────────────
    if inp.tracking_shows_delivered and inp.has_delivery_confirmation:
        adj = +0.08
        factors.append(WinRateFactor("Delivery confirmed", adj,
                                      "Tracking shows delivered with confirmation"))
        signal_count += 1
        pos_factors.append("Package confirmed delivered")
    elif inp.tracking_shows_delivered:
        adj = +0.04
        factors.append(WinRateFactor("Tracking delivered (no conf)", adj,
                                      "Tracking shows delivered but no formal confirmation"))
        signal_count += 1

    if inp.has_signature_confirmation and inp.transaction_amount >= 750:
        adj = +0.06
        factors.append(WinRateFactor("Signature confirmation", adj,
                                      "Signature obtained for high-value shipment"))
        signal_count += 1
        pos_factors.append("Signature confirmation on high-value order")

    # ── Policy ───────────────────────────────────────────────────
    if inp.policy_defensibility:
        adj = _POLICY_ADJUSTMENTS.get(inp.policy_defensibility, 0.0)
        factors.append(WinRateFactor("Policy defensibility", adj,
                                      f"Refund policy rated {inp.policy_defensibility}"))
        signal_count += 1
        if adj > 0:
            pos_factors.append(f"Strong refund policy ({inp.policy_defensibility})")
        elif adj < 0:
            neg_factors.append(f"Weak refund policy ({inp.policy_defensibility})")

    # ── Buyer fraud signals ───────────────────────────────────────
    if inp.fraud_signal_level:
        adj = _FRAUD_SIGNAL_ADJUSTMENTS.get(inp.fraud_signal_level, 0.0)
        if adj != 0:
            factors.append(WinRateFactor("Buyer fraud signals", adj,
                                          f"Buyer shows {inp.fraud_signal_level} fraud signals"))
            signal_count += 1
            pos_factors.append(f"Buyer communication flags fraud ({inp.fraud_signal_level})")

    if inp.buyer_threatened_legal:
        adj = +0.03
        factors.append(WinRateFactor("Legal threat", adj,
                                      "Buyer made legal threats — often signals awareness of weak case"))
        pos_factors.append("Buyer legal threat suggests awareness of weak claim")

    # ── Buyer dispute history ─────────────────────────────────────
    if inp.buyer_prior_disputes >= 3:
        adj = +0.05
        factors.append(WinRateFactor("Repeat buyer disputer", adj,
                                      f"Buyer has {inp.buyer_prior_disputes} prior disputes"))
        signal_count += 1
        pos_factors.append(f"Buyer is repeat disputer ({inp.buyer_prior_disputes} disputes)")

    # ── Merchant win history ──────────────────────────────────────
    if inp.merchant_prior_wins_on_similar >= 2:
        adj = +0.04
        factors.append(WinRateFactor("Merchant win history", adj,
                                      "Merchant has won similar disputes before"))
        pos_factors.append("Merchant has prior wins on similar disputes")

    # ── HDR tier ────────────────────────────────────────────────
    if inp.hdr_tier in ("FEE_TIER", "SEVERE"):
        adj = -0.03
        factors.append(WinRateFactor("HDR tier", adj,
                                      f"Elevated HDR ({inp.hdr_tier}) may signal systemic issues to network"))
        neg_factors.append(f"Elevated HDR tier ({inp.hdr_tier})")

    # ── Compute final rate ───────────────────────────────────────
    total_adj = sum(f.adjustment for f in factors)
    adjusted = max(0.02, min(0.98, base + total_adj))

    # ── Confidence ───────────────────────────────────────────────
    if signal_count >= 4:
        confidence = ConfidenceLevel.HIGH
    elif signal_count >= 2:
        confidence = ConfidenceLevel.MEDIUM
    else:
        confidence = ConfidenceLevel.LOW

    # EV: fight_cost=15, cb_fee=20
    fight_cost = 15.0
    cb_fee = 20.0
    ev = adjusted * inp.transaction_amount - fight_cost - (1 - adjusted) * cb_fee

    likelihood = _win_likelihood(adjusted)

    if adjusted >= 0.60:
        rec = "FIGHT — positive EV and good win odds"
    elif adjusted >= 0.45 and ev > 0:
        rec = "FIGHT — borderline but positive expected value"
    elif ev < -10:
        rec = "ACCEPT — negative EV, not worth fighting"
    else:
        rec = "REVIEW — borderline; weigh dispute cost vs. concession"

    summary = (
        f"WinRate {inp.dispute_id}: "
        f"p={adjusted:.2f} | {likelihood.value} | "
        f"confidence={confidence.value} | ev=${ev:+.0f}"
    )

    return WinRateEstimate(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        base_win_rate=base,
        adjusted_win_rate=round(adjusted, 3),
        confidence=confidence,
        win_likelihood=likelihood,
        factors=factors,
        positive_factors=pos_factors,
        negative_factors=neg_factors,
        expected_value=round(ev, 2),
        recommendation=rec,
        summary=summary,
    )
