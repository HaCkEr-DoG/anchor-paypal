"""
Merchant Reputation Score — composite trust score for dispute routing.

PayPal uses internal risk signals to decide how much scrutiny to apply to
a merchant's disputes. This oracle synthesizes 6 merchant health dimensions
into a single 0–100 composite reputation score and maps it to a routing tier:

  GOLD     (85–100) — auto-resolve eligible; minimal manual review
  SILVER   (65–84)  — standard processing; occasional spot-check
  BRONZE   (40–64)  — enhanced review; consider requiring documentation
  REVIEW   (20–39)  — flag for proactive account health check
  SUSPEND  (0–19)   — escalate to PayPal merchant risk team

The 6 dimensions and their weights:
  1. chargeback_rate_pct      (25%) — lower is better; score 100 at 0%, 0 at 2%
  2. dispute_win_rate_pct     (20%) — higher = better merchant evidence quality
  3. response_timeliness_pct  (20%) — % of disputes answered before deadline
  4. refund_rate_pct          (15%) — refund ratio (high may indicate abuse)
  5. account_age_months       (10%) — older = more trust, caps at 36 months
  6. verified_seller          (10%) — PayPal verified seller badge / identity

PayPal benefit:
  - GOLD merchants can be fast-tracked, reducing manual review cost (~$12/case)
  - SUSPEND merchants get early intervention before costly chargeback program entry
  - Score delta month-over-month surfaces merchants improving or deteriorating
  - Routing tier can gate which dispute products (Seller Protection, Advance) a
    merchant accesses
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ReputationTier(str, Enum):
    GOLD    = "GOLD"     # 85–100
    SILVER  = "SILVER"   # 65–84
    BRONZE  = "BRONZE"   # 40–64
    REVIEW  = "REVIEW"   # 20–39
    SUSPEND = "SUSPEND"  # 0–19


_TIER_THRESHOLDS = [
    (85, ReputationTier.GOLD),
    (65, ReputationTier.SILVER),
    (40, ReputationTier.BRONZE),
    (20, ReputationTier.REVIEW),
    (0,  ReputationTier.SUSPEND),
]

_FAST_TRACK_REVIEW_COST_USD = 12.0  # review cost saved per GOLD dispute


@dataclass
class MerchantReputationInput:
    merchant_id: str
    chargeback_rate_pct: float          # 0.0 – 100.0 (usually 0.0–2.0)
    dispute_win_rate_pct: float         # 0.0 – 100.0
    response_timeliness_pct: float      # 0.0 – 100.0
    refund_rate_pct: float              # 0.0 – 100.0
    account_age_months: int             # 0 – ∞
    is_verified_seller: bool
    total_disputes_last_90d: int = 0    # used for context / signal strength
    previous_score: Optional[float] = None  # last month's score for delta


@dataclass
class DimensionBreakdown:
    name: str
    raw_value: float
    normalized_score: float     # 0–100 on this dimension
    weight: float
    weighted_contribution: float


@dataclass
class MerchantReputationReport:
    merchant_id: str
    composite_score: float              # 0–100
    tier: ReputationTier
    dimensions: list
    score_delta: Optional[float]        # vs previous_score (None if no prior)
    signal_strength: str                # STRONG / MODERATE / WEAK (based on volume)
    routing_recommendation: str
    paypal_savings_per_dispute_usd: float
    paypal_signal: str


# ─── Dimension normalizers ────────────────────────────────────────────────────

def _score_chargeback_rate(rate: float) -> float:
    """0% → 100, 1% → 50, 2% → 0."""
    return max(0.0, 100.0 - (rate / 2.0) * 100.0)


def _score_dispute_win_rate(rate: float) -> float:
    """Direct: win_rate maps to score linearly."""
    return max(0.0, min(100.0, rate))


def _score_response_timeliness(rate: float) -> float:
    """Direct: timeliness % maps to score."""
    return max(0.0, min(100.0, rate))


def _score_refund_rate(rate: float) -> float:
    """
    Low refund rate is neutral; high refund rate can indicate dispute avoidance
    OR abuse. 0–10% → 100; 10–30% → declining; >50% → concern.
    """
    if rate <= 10.0:
        return 100.0
    if rate <= 30.0:
        return 100.0 - ((rate - 10.0) / 20.0) * 50.0  # 100 → 50
    if rate <= 50.0:
        return 50.0 - ((rate - 30.0) / 20.0) * 30.0   # 50 → 20
    return max(0.0, 20.0 - ((rate - 50.0) / 50.0) * 20.0)  # 20 → 0


def _score_account_age(months: int) -> float:
    """Caps at 36 months (3 years) = 100."""
    return min(100.0, (months / 36.0) * 100.0)


def _score_verified(is_verified: bool) -> float:
    return 100.0 if is_verified else 0.0


# ─── Main function ────────────────────────────────────────────────────────────

def score_merchant_reputation(inp: MerchantReputationInput) -> MerchantReputationReport:
    dims_def = [
        ("chargeback_rate",     inp.chargeback_rate_pct,      _score_chargeback_rate,     0.25),
        ("dispute_win_rate",    inp.dispute_win_rate_pct,      _score_dispute_win_rate,    0.20),
        ("response_timeliness", inp.response_timeliness_pct,   _score_response_timeliness, 0.20),
        ("refund_rate",         inp.refund_rate_pct,           _score_refund_rate,         0.15),
        ("account_age_months",  float(inp.account_age_months), _score_account_age,         0.10),
        ("verified_seller",     1.0 if inp.is_verified_seller else 0.0, _score_verified,   0.10),
    ]

    dimensions = []
    composite = 0.0
    for name, raw, scorer, weight in dims_def:
        norm = scorer(raw)
        contribution = norm * weight
        composite += contribution
        dimensions.append(DimensionBreakdown(
            name=name,
            raw_value=round(raw, 4),
            normalized_score=round(norm, 2),
            weight=weight,
            weighted_contribution=round(contribution, 2),
        ))

    composite = round(min(100.0, max(0.0, composite)), 2)

    # Tier
    tier = ReputationTier.SUSPEND
    for threshold, t in _TIER_THRESHOLDS:
        if composite >= threshold:
            tier = t
            break

    # Score delta
    delta = None
    if inp.previous_score is not None:
        delta = round(composite - inp.previous_score, 2)

    # Signal strength based on dispute volume
    if inp.total_disputes_last_90d >= 20:
        strength = "STRONG"
    elif inp.total_disputes_last_90d >= 5:
        strength = "MODERATE"
    else:
        strength = "WEAK"

    # Routing recommendation
    if tier == ReputationTier.GOLD:
        routing = (
            f"GOLD tier — eligible for fast-track auto-resolution. "
            f"Minimal manual review required. Potential saving: "
            f"${_FAST_TRACK_REVIEW_COST_USD:.0f}/dispute in review cost."
        )
        savings = _FAST_TRACK_REVIEW_COST_USD
    elif tier == ReputationTier.SILVER:
        routing = "SILVER tier — standard processing. Spot-check 10% of disputes."
        savings = _FAST_TRACK_REVIEW_COST_USD * 0.5
    elif tier == ReputationTier.BRONZE:
        routing = "BRONZE tier — enhanced review. Require documentation for disputes > $100."
        savings = 0.0
    elif tier == ReputationTier.REVIEW:
        routing = "REVIEW tier — flag for proactive account health check. Consider outreach."
        savings = 0.0
    else:  # SUSPEND
        routing = "SUSPEND tier — escalate to PayPal merchant risk team immediately."
        savings = 0.0

    # Delta annotation
    if delta is not None:
        direction = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        routing += f" Score {direction}{abs(delta):.1f} vs last month."

    paypal_signal = (
        f"Merchant {inp.merchant_id}: score={composite:.1f}, tier={tier.value}, "
        f"signal={strength}"
        + (f", delta={delta:+.1f}" if delta is not None else "")
        + f", savings=${savings:.0f}/dispute."
    )

    return MerchantReputationReport(
        merchant_id=inp.merchant_id,
        composite_score=composite,
        tier=tier,
        dimensions=dimensions,
        score_delta=delta,
        signal_strength=strength,
        routing_recommendation=routing,
        paypal_savings_per_dispute_usd=round(savings, 2),
        paypal_signal=paypal_signal,
    )
