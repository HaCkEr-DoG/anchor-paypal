"""
Bayesian Win Rate Calibrator — your actual win rates, not industry averages.

The fundamental problem with every fixed-rule dispute system: they use industry
average win rates. Industry average for INR is 68%. But YOUR win rate might be
85% (great carrier, great evidence) or 40% (drop-shipper, no tracking).

Using industry averages for EV calculations means:
  - Merchants with better-than-average win rates UNDER-fight (too often accept)
  - Merchants with worse-than-average win rates OVER-fight (too often fight, lose, pay fees)

This module maintains a Bayesian model of your actual win rates:
  - Per claim type (INR, SNAD, UNAUTHORIZED)
  - Per evidence quality tier (excellent / good / poor)
  - Per buyer risk tier (normal / elevated / high / serial)

After 20+ disputes of a type, the oracle uses YOUR calibrated rates.
Before 20 disputes, it blends industry priors with observed outcomes (Laplace smoothing).

The calibration is directional: each update shifts the estimate toward the truth
but never overfits to small samples. With 5 disputes, you get a 20% correction
toward your data. With 50 disputes, you get an 80% correction.

This is the kind of feedback loop that turns a good tool into a great one over time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math


class EvidenceQuality(str, Enum):
    EXCELLENT = "EXCELLENT"  # Tracking + delivery confirmation + photos
    GOOD      = "GOOD"       # Tracking + at least one more signal
    FAIR      = "FAIR"       # Tracking only
    POOR      = "POOR"       # No tracking


# Industry baseline win rates (from decision.py — Anchor's default priors)
_INDUSTRY_PRIORS = {
    "ITEM_NOT_RECEIVED":          0.68,
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": 0.38,
    "UNAUTHORIZED":               0.10,
    "BILLING_ISSUE":              0.72,
}

# Evidence quality multipliers on win rate
_EVIDENCE_MULTIPLIERS = {
    EvidenceQuality.EXCELLENT: 1.25,
    EvidenceQuality.GOOD:      1.10,
    EvidenceQuality.FAIR:      1.00,
    EvidenceQuality.POOR:      0.70,
}

# Minimum disputes of a type before calibration replaces prior
_MIN_DISPUTES_FOR_CALIBRATION = 10
# Full calibration weight reached at this many disputes
_FULL_CALIBRATION_AT = 50


@dataclass
class DisputeOutcomeRecord:
    """One resolved dispute for the calibration dataset."""
    claim_type: str
    won: bool                              # True = merchant won
    evidence_quality: EvidenceQuality = EvidenceQuality.FAIR
    buyer_risk_tier: str = "TIER_1_NORMAL"
    dispute_amount: float = 0.0
    # Optional: for stratified analysis
    carrier_confirmed_delivery: bool = False
    is_digital_goods: bool = False


@dataclass
class ClaimTypeStats:
    """Calibration statistics for one claim type × evidence tier."""
    claim_type: str
    evidence_quality: EvidenceQuality
    wins: int = 0
    losses: int = 0
    # Laplace-smoothed rate
    calibrated_win_rate: float = 0.0
    industry_prior: float = 0.0
    blend_weight: float = 0.0  # 0 = pure prior, 1 = pure observed
    sample_size: int = 0
    confidence_interval_low: float = 0.0
    confidence_interval_high: float = 0.0


@dataclass
class CalibrationModel:
    """
    Merchant-specific win rate model, blending priors with observed outcomes.
    Use calibrated_win_rate() to get the best estimate for oracle decisions.
    """
    merchant_id: str
    total_disputes_observed: int
    # Calibration stats per (claim_type, evidence_quality)
    stats: dict[str, ClaimTypeStats] = field(default_factory=dict)
    # Overall calibration health
    is_well_calibrated: bool = False   # True when enough data to trust
    calibration_note: str = ""
    # Buyer tier adjustments (from observed data)
    tier_adjustments: dict[str, float] = field(default_factory=dict)


def build_calibration_model(
    merchant_id: str,
    history: list[DisputeOutcomeRecord],
) -> CalibrationModel:
    """
    Build a calibrated win-rate model from dispute outcome history.

    Uses Laplace (additive) smoothing: adds α=2 pseudo-observations at the
    prior rate to prevent overfitting on small samples.
    """
    total = len(history)
    is_calibrated = total >= _MIN_DISPUTES_FOR_CALIBRATION

    # Group by (claim_type, evidence_quality)
    buckets: dict[str, list[bool]] = {}
    for record in history:
        key = f"{record.claim_type}::{record.evidence_quality.value}"
        buckets.setdefault(key, []).append(record.won)

    stats: dict[str, ClaimTypeStats] = {}
    for key, outcomes in buckets.items():
        claim_type, eq_str = key.split("::")
        evidence_quality = EvidenceQuality(eq_str)
        prior = _INDUSTRY_PRIORS.get(claim_type, 0.50)
        # Apply evidence multiplier to prior
        ev_mult = _EVIDENCE_MULTIPLIERS.get(evidence_quality, 1.0)
        adjusted_prior = min(prior * ev_mult, 0.95)

        wins = sum(outcomes)
        n = len(outcomes)

        # Laplace smoothing: blend = observed_weight * observed + (1-weight) * prior
        blend_weight = _blend_weight(n)
        # Pseudo-counts: α wins + (1-α) losses at prior rate, with α = 2
        alpha = 2.0
        smoothed_wins = wins + alpha * adjusted_prior
        smoothed_n = n + alpha
        smoothed_rate = smoothed_wins / smoothed_n

        calibrated_rate = blend_weight * smoothed_rate + (1 - blend_weight) * adjusted_prior

        # Wilson confidence interval (95%)
        ci_low, ci_high = _wilson_ci(wins, n) if n >= 5 else (0.0, 1.0)

        stats[key] = ClaimTypeStats(
            claim_type=claim_type,
            evidence_quality=evidence_quality,
            wins=wins,
            losses=n - wins,
            calibrated_win_rate=round(min(calibrated_rate, 0.95), 4),
            industry_prior=round(adjusted_prior, 4),
            blend_weight=round(blend_weight, 3),
            sample_size=n,
            confidence_interval_low=round(ci_low, 4),
            confidence_interval_high=round(ci_high, 4),
        )

    # Buyer tier adjustments: compare win rate by tier vs overall
    tier_adjustments = _compute_tier_adjustments(history)

    note = (
        f"Calibrated on {total} disputes. "
        f"{'Using calibrated rates.' if is_calibrated else f'Only {total} disputes — still blending with industry priors.'}"
    )

    return CalibrationModel(
        merchant_id=merchant_id,
        total_disputes_observed=total,
        stats=stats,
        is_well_calibrated=is_calibrated,
        calibration_note=note,
        tier_adjustments=tier_adjustments,
    )


def calibrated_win_rate(
    model: CalibrationModel,
    claim_type: str,
    evidence_quality: EvidenceQuality = EvidenceQuality.FAIR,
    buyer_risk_tier: str = "TIER_1_NORMAL",
) -> float:
    """
    Get the best win-probability estimate for oracle decisions.

    Priority: calibrated model > adjusted prior > raw prior.
    Also applies buyer-tier adjustment from observed tier-specific outcomes.
    """
    key = f"{claim_type}::{evidence_quality.value}"
    if key in model.stats:
        base = model.stats[key].calibrated_win_rate
    else:
        # No calibration data — use adjusted industry prior
        prior = _INDUSTRY_PRIORS.get(claim_type, 0.50)
        ev_mult = _EVIDENCE_MULTIPLIERS.get(evidence_quality, 1.0)
        base = min(prior * ev_mult, 0.95)

    # Apply buyer tier adjustment
    tier_adj = model.tier_adjustments.get(buyer_risk_tier, 0.0)
    adjusted = min(max(base + tier_adj, 0.02), 0.95)
    return round(adjusted, 4)


def _blend_weight(n: int) -> float:
    """
    Sigmoid blend: 0 at n=0, ~0.5 at n=_MIN, ~0.9 at n=_FULL.
    """
    if n == 0:
        return 0.0
    return 1.0 - 1.0 / (1.0 + (n / _FULL_CALIBRATION_AT) ** 1.5)


def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a proportion."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z ** 2 / n
    center = (p + z ** 2 / (2 * n)) / denom
    spread = (z * math.sqrt(p * (1 - p) / n + z ** 2 / (4 * n ** 2))) / denom
    return max(0.0, center - spread), min(1.0, center + spread)


def _compute_tier_adjustments(history: list[DisputeOutcomeRecord]) -> dict[str, float]:
    """
    For each buyer risk tier with >= 5 observations, compute win-rate delta
    vs overall win rate. Used as an additive adjustment.
    """
    overall_wins = sum(1 for r in history if r.won)
    overall_rate = overall_wins / len(history) if history else 0.5

    tier_buckets: dict[str, list[bool]] = {}
    for r in history:
        tier_buckets.setdefault(r.buyer_risk_tier, []).append(r.won)

    adjustments: dict[str, float] = {}
    for tier, outcomes in tier_buckets.items():
        if len(outcomes) >= 5:
            tier_rate = sum(outcomes) / len(outcomes)
            adjustments[tier] = round(tier_rate - overall_rate, 4)
    return adjustments


def calibration_summary(model: CalibrationModel) -> str:
    """Human-readable summary of calibration status."""
    lines = [f"Calibration for merchant {model.merchant_id}:"]
    lines.append(f"  Disputes observed: {model.total_disputes_observed}")
    lines.append(f"  Well-calibrated: {model.is_well_calibrated}")
    for key, s in sorted(model.stats.items()):
        lines.append(
            f"  {s.claim_type}/{s.evidence_quality.value}: "
            f"{s.wins}W/{s.losses}L → "
            f"calibrated={s.calibrated_win_rate:.0%} "
            f"(prior={s.industry_prior:.0%}, weight={s.blend_weight:.0%})"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # Simulate a merchant with strong INR win rate (good carrier)
    history = [
        DisputeOutcomeRecord("ITEM_NOT_RECEIVED", won=True,
                             evidence_quality=EvidenceQuality.EXCELLENT) for _ in range(18)
    ] + [
        DisputeOutcomeRecord("ITEM_NOT_RECEIVED", won=False,
                             evidence_quality=EvidenceQuality.EXCELLENT) for _ in range(2)
    ] + [
        DisputeOutcomeRecord("SIGNIFICANTLY_NOT_AS_DESCRIBED", won=True,
                             evidence_quality=EvidenceQuality.FAIR) for _ in range(5)
    ] + [
        DisputeOutcomeRecord("SIGNIFICANTLY_NOT_AS_DESCRIBED", won=False,
                             evidence_quality=EvidenceQuality.FAIR) for _ in range(10)
    ]
    model = build_calibration_model("merchant_123", history)
    print(calibration_summary(model))
    print()
    win_rate = calibrated_win_rate(model, "ITEM_NOT_RECEIVED", EvidenceQuality.EXCELLENT)
    print(f"Calibrated INR+EXCELLENT win rate: {win_rate:.0%}")
    print(f"Industry prior: {_INDUSTRY_PRIORS['ITEM_NOT_RECEIVED']:.0%}")
