"""
Win Probability Calibrator — produce a calibrated win probability
estimate for a dispute by combining base rates with signal adjustments.

Starts from base rates by reason code, then applies modifiers for:
  - Evidence quality and completeness
  - CE3.0 eligibility (Visa unauthorized only)
  - Buyer risk profile
  - Network-specific tendencies
  - Deadline pressure (last-minute responses win less)
  - Seller protection eligibility
  - Prior dispute outcomes with this buyer

Final probability clamped to [0.02, 0.97].

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# Base win rates by reason code (empirical medians from PayPal/network data)
_BASE_WIN_RATES: dict[str, float] = {
    "INR":           0.52,
    "ITEM_NOT_RECEIVED": 0.52,
    "SNAD":          0.38,
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": 0.38,
    "UNAUTHORIZED":  0.45,
    "10.4":          0.40,
    "4807":          0.70,
    "SUBSCRIPTION":  0.55,
    "12.6":          0.65,
    "13.1":          0.50,
    "13.3":          0.35,
    "4853":          0.38,
    "DEFAULT":       0.45,
}

# Network multipliers (some networks favor merchants/buyers more)
_NETWORK_MULTIPLIERS: dict[str, float] = {
    "PAYPAL":   1.00,
    "VISA":     1.05,   # Visa process slightly favors merchants with strong evidence
    "MC":       1.00,
    "AMEX":     0.92,   # Amex historically more buyer-friendly
    "DISCOVER": 1.03,
}


class CalibrationConfidence(str, Enum):
    HIGH   = "HIGH"    # Many signals → tight confidence interval
    MEDIUM = "MEDIUM"  # Some signals → moderate confidence
    LOW    = "LOW"     # Few signals → wide confidence interval


@dataclass
class WinProbabilityInput:
    """All signals used to calibrate win probability."""
    dispute_id: str
    reason_code: str
    network: str

    # Evidence quality
    evidence_completeness_score: int = 50     # 0–100
    has_tracking_with_delivery: bool = False
    has_signature_confirmation: bool = False
    has_3ds_authentication: bool = False
    has_avs_match: bool = False
    has_buyer_communication: bool = False

    # CE3.0
    ce3_eligible: bool = False                # +18pp if eligible (Visa unauthorized)

    # Buyer profile
    buyer_risk_class: str = "NORMAL"          # TRUSTED → boosts; FRAUDSTER → boosts fight too
    buyer_dispute_count: int = 0

    # Timeline
    days_remaining_in_window: int = 10
    response_window_days: int = 20

    # SP
    seller_protection_eligible: bool = False

    # Historical performance
    merchant_win_rate_90d: Optional[float] = None   # Merchant's actual 90d win rate
    prior_wins_this_reason: int = 0
    prior_disputes_this_reason: int = 0


@dataclass
class ProbabilityAdjustment:
    """One signal's contribution to the final probability."""
    signal: str
    adjustment: float    # Positive = boosts win prob; negative = reduces
    rationale: str


@dataclass
class WinProbabilityReport:
    """Calibrated win probability with breakdown."""
    dispute_id: str
    reason_code: str
    network: str

    base_rate: float
    network_multiplied: float
    final_probability: float

    adjustments: list[ProbabilityAdjustment]
    total_adjustment: float

    confidence: CalibrationConfidence
    confidence_interval_low: float    # 95% CI lower
    confidence_interval_high: float   # 95% CI upper

    key_drivers: list[str]            # Top positive and negative factors
    summary: str


def calibrate_win_probability(inp: WinProbabilityInput) -> WinProbabilityReport:
    """Calibrate a dispute's win probability from all available signals."""
    code = inp.reason_code.upper()
    net  = inp.network.upper()

    base = _BASE_WIN_RATES.get(code, _BASE_WIN_RATES["DEFAULT"])
    net_mult = _NETWORK_MULTIPLIERS.get(net, 1.0)
    after_network = round(base * net_mult, 4)

    adjustments: list[ProbabilityAdjustment] = []

    # ── Evidence adjustments ──────────────────────────────────────────────────
    if inp.evidence_completeness_score >= 80:
        adj = +0.08
        adjustments.append(ProbabilityAdjustment("evidence_completeness", adj, f"Strong evidence ({inp.evidence_completeness_score}/100)"))
    elif inp.evidence_completeness_score >= 60:
        adj = +0.03
        adjustments.append(ProbabilityAdjustment("evidence_completeness", adj, f"Adequate evidence ({inp.evidence_completeness_score}/100)"))
    elif inp.evidence_completeness_score < 40:
        adj = -0.10
        adjustments.append(ProbabilityAdjustment("evidence_completeness", adj, f"Weak evidence ({inp.evidence_completeness_score}/100)"))

    if inp.has_tracking_with_delivery:
        adjustments.append(ProbabilityAdjustment("tracking_delivery", +0.08, "Delivery confirmation available"))

    if inp.has_signature_confirmation:
        adjustments.append(ProbabilityAdjustment("signature", +0.06, "Signature confirmation available"))

    if inp.has_3ds_authentication:
        adjustments.append(ProbabilityAdjustment("3ds_auth", +0.10, "3DS authentication passed"))
    elif code in ("UNAUTHORIZED", "10.4", "4807") and not inp.has_3ds_authentication:
        adjustments.append(ProbabilityAdjustment("no_3ds_unauth", -0.08, "No 3DS for unauthorized dispute"))

    if inp.has_avs_match:
        adjustments.append(ProbabilityAdjustment("avs_match", +0.04, "AVS match confirms cardholder address"))

    if inp.has_buyer_communication:
        adjustments.append(ProbabilityAdjustment("buyer_comms", +0.05, "Communication log documents buyer interaction"))

    # ── CE3.0 boost ───────────────────────────────────────────────────────────
    if inp.ce3_eligible:
        adjustments.append(ProbabilityAdjustment("ce3_eligible", +0.18, "CE3.0 package available — +18pp win rate boost"))

    # ── Seller protection ─────────────────────────────────────────────────────
    if inp.seller_protection_eligible:
        adjustments.append(ProbabilityAdjustment("seller_protection", +0.12, "Seller Protection eligible — network backs merchant"))

    # ── Buyer risk ────────────────────────────────────────────────────────────
    risk_adj = {
        "TRUSTED":   +0.05,
        "NORMAL":     0.00,
        "ELEVATED":  -0.05,
        "HIGH_RISK": +0.08,    # Networks favor merchants fighting serial abusers
        "FRAUDSTER": +0.10,
    }
    buyer_adj = risk_adj.get(inp.buyer_risk_class.upper(), 0.0)
    if buyer_adj != 0:
        adjustments.append(ProbabilityAdjustment(
            "buyer_risk", buyer_adj,
            f"Buyer risk class {inp.buyer_risk_class}: {'serial abuser → fight bonus' if buyer_adj > 0 and inp.buyer_risk_class not in ('TRUSTED',) else 'trusted buyer' if buyer_adj > 0 else 'elevated risk buyer'}",
        ))

    # ── Deadline pressure ─────────────────────────────────────────────────────
    if inp.days_remaining_in_window <= 2:
        adjustments.append(ProbabilityAdjustment("deadline_pressure", -0.07, "Response filed at last minute — less persuasive"))
    elif inp.days_remaining_in_window >= inp.response_window_days * 0.8:
        adjustments.append(ProbabilityAdjustment("early_response", +0.03, "Filed early in window — thoroughness signal"))

    # ── Historical merchant win rate ──────────────────────────────────────────
    if inp.merchant_win_rate_90d is not None:
        diff = inp.merchant_win_rate_90d - base
        if abs(diff) > 0.05:
            clamped = max(-0.10, min(0.10, diff * 0.5))
            adjustments.append(ProbabilityAdjustment(
                "merchant_history", round(clamped, 4),
                f"Merchant 90d win rate {inp.merchant_win_rate_90d:.0%} vs base {base:.0%}",
            ))

    # ── Prior outcomes with same reason code ─────────────────────────────────
    if inp.prior_disputes_this_reason >= 3:
        local_win_rate = inp.prior_wins_this_reason / inp.prior_disputes_this_reason
        diff = local_win_rate - base
        if abs(diff) > 0.05:
            clamped = max(-0.08, min(0.08, diff * 0.4))
            adjustments.append(ProbabilityAdjustment(
                "local_history", round(clamped, 4),
                f"Prior {inp.reason_code} win rate: {local_win_rate:.0%} ({inp.prior_wins_this_reason}/{inp.prior_disputes_this_reason})",
            ))

    # ── Final probability ─────────────────────────────────────────────────────
    total_adj = round(sum(a.adjustment for a in adjustments), 4)
    final = after_network + total_adj
    final = max(0.02, min(0.97, round(final, 4)))

    # ── Confidence ────────────────────────────────────────────────────────────
    signal_count = len(adjustments)
    if signal_count >= 6:
        conf = CalibrationConfidence.HIGH
        ci_half = 0.08
    elif signal_count >= 3:
        conf = CalibrationConfidence.MEDIUM
        ci_half = 0.13
    else:
        conf = CalibrationConfidence.LOW
        ci_half = 0.18

    ci_low  = max(0.01, round(final - ci_half, 2))
    ci_high = min(0.99, round(final + ci_half, 2))

    # ── Key drivers ───────────────────────────────────────────────────────────
    sorted_adjs = sorted(adjustments, key=lambda a: abs(a.adjustment), reverse=True)
    key_drivers = [f"{'+' if a.adjustment >= 0 else ''}{a.adjustment:+.0%} {a.signal}: {a.rationale}"
                   for a in sorted_adjs[:4]]

    summary = (
        f"WinProb {inp.dispute_id} [{net}|{inp.reason_code}]: "
        f"base={base:.0%} → final={final:.0%} (adj={total_adj:+.0%}) | "
        f"CI=[{ci_low:.0%},{ci_high:.0%}] conf={conf.value}"
    )

    return WinProbabilityReport(
        dispute_id=inp.dispute_id,
        reason_code=inp.reason_code,
        network=net,
        base_rate=base,
        network_multiplied=after_network,
        final_probability=final,
        adjustments=adjustments,
        total_adjustment=total_adj,
        confidence=conf,
        confidence_interval_low=ci_low,
        confidence_interval_high=ci_high,
        key_drivers=key_drivers,
        summary=summary,
    )
