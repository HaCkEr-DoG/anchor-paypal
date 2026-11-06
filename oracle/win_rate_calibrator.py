"""
Win Rate Calibrator — compute calibrated dispute win probability.

The raw win probability for a dispute depends on:
  1. Claim type (INR has higher win rate than SNAD)
  2. Evidence quality (tracking + signature boosts win rate significantly)
  3. Carrier / delivery confirmation
  4. Buyer signals (repeat filer, fraud network member)
  5. CE3.0 eligibility (Visa unauthorized — +15-23pp)
  6. PayPal Seller Protection coverage (SP covers INR + UNAUTHORIZED, not SNAD)
  7. Order amount (high-value orders have different dynamics)
  8. Dispute age (older disputes have higher win rate if not escalated)

Base win rates by claim type (empirical PayPal merchant data):
  INR with tracking + signature: ~82%
  INR with tracking only:        ~65%
  INR no proof:                  ~25%
  SNAD:                          ~38%
  UNAUTHORIZED (non-CE3.0):      ~55%
  UNAUTHORIZED (CE3.0 eligible): ~72-78%
  SUBSCRIPTION:                  ~60%
  DUPLICATE:                     ~70%

Adjustments (additive pp):
  Signature confirmation:         +8pp
  Delivery photo:                 +5pp
  Carrier affidavit:              +6pp
  Tier 1 carrier (UPS/FedEx/USPS): +4pp
  Tier 3 carrier (LaserShip etc):  -8pp
  Buyer dispute_count_90d >= 3:   +12pp (serial filer, easier to discredit)
  CE3.0 eligible:                 +18pp
  High value (>= $750):           -5pp  (PayPal scrutinizes more)
  Formal dispute (not inquiry):   -3pp  (harder to win than informal)
  SP covered claim:               +10pp (PayPal backstops)
  Evidence tier EXCELLENT:        +22pp
  Evidence tier GOOD:             +14pp
  Evidence tier ADEQUATE:         +7pp
  Evidence tier WEAK:             +2pp
  Evidence tier INSUFFICIENT:     -5pp

Final probability is clamped to [0.02, 0.97].

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ClaimType(str, Enum):
    INR           = "INR"
    SNAD          = "SNAD"
    UNAUTHORIZED  = "UNAUTHORIZED"
    SUBSCRIPTION  = "SUBSCRIPTION"
    DUPLICATE     = "DUPLICATE"
    OTHER         = "OTHER"


class CarrierTier(str, Enum):
    TIER_1  = "TIER_1"   # USPS, UPS, FedEx
    TIER_2  = "TIER_2"   # DHL Express, OnTrac
    TIER_3  = "TIER_3"   # LaserShip, DHL eCommerce, Pitney Bowes
    UNKNOWN = "UNKNOWN"


class EvidenceTier(str, Enum):
    EXCELLENT    = "EXCELLENT"
    GOOD         = "GOOD"
    ADEQUATE     = "ADEQUATE"
    WEAK         = "WEAK"
    INSUFFICIENT = "INSUFFICIENT"


# Base win rates per claim type (no adjustments)
_BASE_WIN_RATES: dict[ClaimType, float] = {
    ClaimType.INR:          0.65,   # With basic tracking
    ClaimType.SNAD:         0.38,
    ClaimType.UNAUTHORIZED: 0.55,
    ClaimType.SUBSCRIPTION: 0.60,
    ClaimType.DUPLICATE:    0.70,
    ClaimType.OTHER:        0.45,
}

# Adjustments (additive, as probability deltas)
_ADJ_SIGNATURE_CONFIRMATION = 0.08
_ADJ_DELIVERY_PHOTO         = 0.05
_ADJ_CARRIER_AFFIDAVIT      = 0.06
_ADJ_TIER_1_CARRIER         = 0.04
_ADJ_TIER_2_CARRIER         = 0.00
_ADJ_TIER_3_CARRIER         = -0.08
_ADJ_SERIAL_FILER           = 0.12   # buyer_dispute_count_90d >= 3
_ADJ_CE30_ELIGIBLE          = 0.18
_ADJ_HIGH_VALUE             = -0.05  # dispute_amount >= 750
_ADJ_FORMAL_DISPUTE         = -0.03
_ADJ_SP_COVERED             = 0.10
_ADJ_NO_TRACKING            = -0.20  # INR with no tracking number

_EVIDENCE_ADJUSTMENTS: dict[EvidenceTier, float] = {
    EvidenceTier.EXCELLENT:    0.22,
    EvidenceTier.GOOD:         0.14,
    EvidenceTier.ADEQUATE:     0.07,
    EvidenceTier.WEAK:         0.02,
    EvidenceTier.INSUFFICIENT: -0.05,
}


@dataclass
class WinRateInput:
    """Input factors for win rate calibration."""
    dispute_id: str
    claim_type: ClaimType

    # Evidence
    has_tracking_number: bool = False
    has_tracking_delivered: bool = False  # Tracking shows "Delivered"
    has_signature_confirmation: bool = False
    has_delivery_photo: bool = False
    has_carrier_affidavit: bool = False
    evidence_tier: Optional[EvidenceTier] = None  # From evidence_quality_scorer

    # Carrier
    carrier_tier: CarrierTier = CarrierTier.UNKNOWN

    # Buyer signals
    buyer_dispute_count_90d: int = 0
    buyer_is_in_fraud_network: bool = False

    # Chargeback specifics
    ce30_eligible: bool = False          # Visa CE3.0

    # Order / dispute facts
    dispute_amount: float = 100.0
    is_formal_dispute: bool = True       # True = formal dispute, False = inquiry

    # Seller protection
    sp_covered: bool = False


@dataclass
class AdjustmentFactor:
    """One win-rate adjustment applied."""
    name: str
    delta: float               # Additive pp change
    applied: bool


@dataclass
class WinRateReport:
    """
    Calibrated win probability with full adjustment breakdown.
    """
    dispute_id: str
    claim_type: ClaimType

    base_win_rate: float                 # Before adjustments
    calibrated_win_rate: float           # After all adjustments, clamped [0.02, 0.97]
    calibrated_win_pct: float            # As percentage

    # Detailed breakdown
    adjustments: list[AdjustmentFactor]
    total_adjustment: float              # Sum of applied deltas

    # Classification
    confidence: str                      # HIGH / MEDIUM / LOW
    summary: str


def _sp_covered(claim_type: ClaimType) -> bool:
    """PayPal Seller Protection covers INR + UNAUTHORIZED, never SNAD."""
    return claim_type in (ClaimType.INR, ClaimType.UNAUTHORIZED)


def calibrate_win_rate(inp: WinRateInput) -> WinRateReport:
    """
    Compute a calibrated win probability for a dispute.
    """
    # Normalize claim type string if needed
    ct = inp.claim_type

    base = _BASE_WIN_RATES.get(ct, _BASE_WIN_RATES[ClaimType.OTHER])

    adjustments: list[AdjustmentFactor] = []

    def add_adj(name: str, delta: float, condition: bool) -> None:
        adjustments.append(AdjustmentFactor(name=name, delta=delta, applied=condition))

    # INR no-tracking penalty
    if ct == ClaimType.INR and not inp.has_tracking_number and not inp.has_tracking_delivered:
        add_adj("no_tracking_inr", _ADJ_NO_TRACKING, True)
    else:
        add_adj("no_tracking_inr", _ADJ_NO_TRACKING, False)

    add_adj("signature_confirmation", _ADJ_SIGNATURE_CONFIRMATION, inp.has_signature_confirmation)
    add_adj("delivery_photo",         _ADJ_DELIVERY_PHOTO,          inp.has_delivery_photo)
    add_adj("carrier_affidavit",      _ADJ_CARRIER_AFFIDAVIT,       inp.has_carrier_affidavit)

    carrier_adj = {
        CarrierTier.TIER_1: _ADJ_TIER_1_CARRIER,
        CarrierTier.TIER_2: _ADJ_TIER_2_CARRIER,
        CarrierTier.TIER_3: _ADJ_TIER_3_CARRIER,
        CarrierTier.UNKNOWN: 0.0,
    }[inp.carrier_tier]
    add_adj(f"carrier_tier_{inp.carrier_tier.value.lower()}", carrier_adj, carrier_adj != 0.0)

    is_serial = inp.buyer_dispute_count_90d >= 3 or inp.buyer_is_in_fraud_network
    add_adj("serial_filer_buyer", _ADJ_SERIAL_FILER, is_serial)

    add_adj("ce30_eligible", _ADJ_CE30_ELIGIBLE, inp.ce30_eligible and ct == ClaimType.UNAUTHORIZED)

    add_adj("high_value_order", _ADJ_HIGH_VALUE, inp.dispute_amount >= 750)
    add_adj("formal_dispute",   _ADJ_FORMAL_DISPUTE, inp.is_formal_dispute)

    # SP coverage — only if not already baked into base and claim qualifies
    sp_applies = _sp_covered(ct) and inp.sp_covered
    add_adj("seller_protection", _ADJ_SP_COVERED, sp_applies)

    # Evidence tier
    if inp.evidence_tier is not None:
        ev_delta = _EVIDENCE_ADJUSTMENTS[inp.evidence_tier]
        add_adj(f"evidence_{inp.evidence_tier.value.lower()}", ev_delta, True)
    else:
        add_adj("evidence_unknown", 0.0, False)

    total_adj = sum(a.delta for a in adjustments if a.applied)
    raw = base + total_adj
    calibrated = max(0.02, min(0.97, raw))

    # Confidence: based on how many factors we know
    known_factors = sum(1 for a in adjustments if a.applied)
    if known_factors >= 4:
        confidence = "HIGH"
    elif known_factors >= 2:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    summary = (
        f"WinRate {inp.dispute_id}: {ct.value} | "
        f"base={base:.2%} + adj={total_adj:+.2%} = {calibrated:.2%} | "
        f"confidence={confidence} | "
        f"applied_factors={known_factors}"
    )

    return WinRateReport(
        dispute_id=inp.dispute_id,
        claim_type=ct,
        base_win_rate=round(base, 4),
        calibrated_win_rate=round(calibrated, 4),
        calibrated_win_pct=round(calibrated * 100, 2),
        adjustments=adjustments,
        total_adjustment=round(total_adj, 4),
        confidence=confidence,
        summary=summary,
    )
