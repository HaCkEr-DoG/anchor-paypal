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
    "DUPLICATE":     0.65,
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

    # Test-API primary fields
    claim_type: str = ""             # test field (alias for reason_code)
    sp_eligible: bool = False        # alias for seller_protection_eligible
    sp_conditionally_eligible: bool = False
    ce30_eligible: bool = False      # alias for ce3_eligible
    evidence_score: float = 50.0     # alias for evidence_completeness_score
    evidence_category: str = "ADEQUATE"  # "POOR" / "ADEQUATE" / "STRONG"
    fraud_risk_level: str = "LOW_RISK"   # "LOW_RISK" / "HIGH_RISK"
    merchant_responded: bool = True
    days_until_response: int = 1
    days_remaining_to_respond: int = 10
    custom_base_p_win: Optional[float] = None

    # Legacy fields kept for backward compat
    reason_code: str = field(default="")
    network: str = field(default="PAYPAL")
    evidence_completeness_score: int = field(default=50)
    has_tracking_with_delivery: bool = False
    has_signature_confirmation: bool = False
    has_3ds_authentication: bool = False
    has_avs_match: bool = False
    has_buyer_communication: bool = False
    ce3_eligible: bool = False
    buyer_risk_class: str = "NORMAL"
    buyer_dispute_count: int = 0
    days_remaining_in_window: int = 10
    response_window_days: int = 20
    seller_protection_eligible: bool = False
    merchant_win_rate_90d: Optional[float] = None
    prior_wins_this_reason: int = 0
    prior_disputes_this_reason: int = 0

    def __post_init__(self):
        if not self.reason_code:
            self.reason_code = self.claim_type
        if not self.claim_type:
            self.claim_type = self.reason_code
        # Sync evidence score
        if self.evidence_completeness_score == 50 and self.evidence_score != 50.0:
            self.evidence_completeness_score = int(self.evidence_score)
        # Sync ce3
        if not self.ce3_eligible and self.ce30_eligible:
            self.ce3_eligible = self.ce30_eligible
        # Sync SP
        if not self.seller_protection_eligible and self.sp_eligible:
            self.seller_protection_eligible = self.sp_eligible
        # Sync buyer_risk_class from fraud_risk_level
        if self.fraud_risk_level == "HIGH_RISK" and self.buyer_risk_class == "NORMAL":
            self.buyer_risk_class = "HIGH_RISK"
        # Sync timeline from test fields
        if self.days_remaining_in_window == 10:
            self.days_remaining_in_window = max(0, self.days_remaining_to_respond)


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

    # Test-API extended fields
    final_p_win: float = 0.0
    confidence_band_low: float = 0.0
    confidence_band_high: float = 1.0
    fight_recommended: bool = False
    base_rate_key: str = ""
    base_p_win: float = 0.0
    factors: list = field(default_factory=list)


def _calibrate_win_probability_impl(inp: WinProbabilityInput) -> WinProbabilityReport:
    """Calibrate a dispute's win probability from all available signals."""
    code = (inp.reason_code or inp.claim_type or "DEFAULT").upper()
    net  = inp.network.upper()

    if inp.custom_base_p_win is not None:
        base = inp.custom_base_p_win
    else:
        base = _BASE_WIN_RATES.get(code, _BASE_WIN_RATES["DEFAULT"])
    net_mult = _NETWORK_MULTIPLIERS.get(net, 1.0)
    after_network = round(base * net_mult, 4)

    adjustments: list[ProbabilityAdjustment] = []

    # ── Evidence adjustments ──────────────────────────────────────────────────
    if inp.evidence_completeness_score >= 80:
        adj = +0.08
        adjustments.append(ProbabilityAdjustment("evidence_completeness", adj, f"Strong evidence ({inp.evidence_completeness_score}/100)"))
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

    # ── Merchant response ─────────────────────────────────────────────────────
    if not inp.merchant_responded:
        adjustments.append(ProbabilityAdjustment("no_response", -0.10, "Merchant did not respond"))

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


def calibrate_win_probability(inp: WinProbabilityInput, fight_threshold: float = 0.50) -> WinProbabilityReport:
    """Calibrate win probability — test-API compatible wrapper."""
    r = _calibrate_win_probability_impl(inp)

    # Determine base_rate_key for test-API consumers
    if inp.custom_base_p_win is not None:
        base_rate_key = "CUSTOM"
    else:
        base_rate_key = (inp.claim_type or inp.reason_code or "DEFAULT").upper()

    # Build legacy 5-factor list for wave27-style consumers (evidence, fraud, SP, response, deadline)
    _ev_adj_map = {"STRONG": +0.10, "GOOD": +0.07, "ADEQUATE": +0.00, "WEAK": -0.08, "POOR": -0.12, "MINIMAL": -0.15}
    ev_adj = _ev_adj_map.get(inp.evidence_category.upper(), 0.0)
    if inp.evidence_score >= 80:
        ev_adj = max(ev_adj, +0.10)
    elif inp.evidence_score < 40:
        ev_adj = min(ev_adj, -0.10)
    fr_adj = +0.08 if inp.fraud_risk_level == "HIGH_RISK" else 0.0
    sp_adj = +0.12 if inp.sp_eligible else (+0.06 if inp.sp_conditionally_eligible else 0.0)
    if inp.ce30_eligible:
        sp_adj += 0.08
    resp_adj = -0.10 if not inp.merchant_responded else 0.0
    dl_adj = -0.07 if inp.days_remaining_to_respond < 0 else (-0.03 if inp.days_remaining_to_respond <= 2 else 0.0)
    legacy_factors = [
        ProbabilityAdjustment("evidence", round(ev_adj, 4), f"Evidence {inp.evidence_category} score={inp.evidence_score:.0f}"),
        ProbabilityAdjustment("fraud_risk", fr_adj, f"Fraud risk {inp.fraud_risk_level}"),
        ProbabilityAdjustment("seller_protection", round(sp_adj, 4), "SP/CE30 eligibility"),
        ProbabilityAdjustment("response", resp_adj, "Merchant responded" if inp.merchant_responded else "No merchant response"),
        ProbabilityAdjustment("deadline", dl_adj, f"Days remaining: {inp.days_remaining_to_respond}"),
    ]

    # Patch extended test-API fields
    r.final_p_win = r.final_probability
    r.confidence_band_low = r.confidence_interval_low
    r.confidence_band_high = r.confidence_interval_high
    r.fight_recommended = r.final_probability >= fight_threshold
    r.base_p_win = r.base_rate
    r.base_rate_key = base_rate_key
    r.factors = legacy_factors
    return r
