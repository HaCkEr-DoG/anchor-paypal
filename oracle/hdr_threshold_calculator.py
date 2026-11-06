"""
HDR Threshold Calculator — compute how many disputes can be opened before
crossing each PayPal High Dispute Rate threshold, and project future rates
under various dispute volume scenarios.

HDR thresholds (rolling 90-day):
  HEALTHY   < 0.50%
  ELEVATED  0.50% – 0.99%   (warning, monitoring)
  LIMITED   1.00% – 1.49%   (account restrictions)
  PENALIZED 1.50% – 2.99%   ($30/dispute chargeback fee)
  SEVERE    ≥ 3.00%          (account termination risk)

This module answers:
  1. How many more disputes until we cross into the next tier?
  2. At the current dispute open rate, when do we cross?
  3. If we win / settle N disputes per week, when do we return to healthy?
  4. What is the maximum sustainable dispute rate for a given volume?

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class HDRTier(str, Enum):
    HEALTHY   = "HEALTHY"
    ELEVATED  = "ELEVATED"
    LIMITED   = "LIMITED"
    PENALIZED = "PENALIZED"
    SEVERE    = "SEVERE"


_THRESHOLDS_PCT: dict[HDRTier, float] = {
    HDRTier.HEALTHY:   0.00,
    HDRTier.ELEVATED:  0.50,
    HDRTier.LIMITED:   1.00,
    HDRTier.PENALIZED: 1.50,
    HDRTier.SEVERE:    3.00,
}

_TIER_ORDER = [
    HDRTier.HEALTHY,
    HDRTier.ELEVATED,
    HDRTier.LIMITED,
    HDRTier.PENALIZED,
    HDRTier.SEVERE,
]


@dataclass
class HDRThresholdInput:
    """Input for HDR threshold calculation."""
    merchant_id: str

    # Current state (rolling 90-day window)
    current_disputes_90d: int
    current_transactions_90d: int

    # Dispute flow rates (per day)
    new_disputes_per_day: float = 0.0       # Rate at which new disputes are opening
    new_transactions_per_day: float = 0.0   # Daily transaction volume

    # Resolution rates (per day — disputes being resolved/closed)
    disputes_resolved_per_day: float = 0.0  # Disputes closing per day

    # Scenario overrides (optional)
    scenario_disputes_per_day: Optional[float] = None   # For "what if" scenarios
    scenario_transactions_per_day: Optional[float] = None


def _rate_pct(disputes: float, transactions: float) -> float:
    if transactions <= 0:
        return 0.0
    return round(disputes / transactions * 100, 4)


def _classify(rate_pct: float) -> HDRTier:
    for tier in reversed(_TIER_ORDER):
        if rate_pct >= _THRESHOLDS_PCT[tier]:
            return tier
    return HDRTier.HEALTHY


def _disputes_at_threshold(transactions: int, tier: HDRTier) -> int:
    """How many disputes brings a merchant to exactly this tier boundary."""
    pct = _THRESHOLDS_PCT[tier]
    return int(pct / 100.0 * transactions)


@dataclass
class TierThreshold:
    """Details for one tier's threshold crossing."""
    tier: HDRTier
    threshold_pct: float
    disputes_at_boundary: int
    disputes_remaining: int      # Positive = room before crossing; negative = already past
    already_crossed: bool


@dataclass
class DaysToTierCrossing:
    """Projected days until a tier boundary is crossed at current dispute rate."""
    tier: HDRTier
    days_until_crossing: Optional[float]   # None if won't cross (rate too low)
    description: str


@dataclass
class RecoveryScenario:
    """How long to recover to HEALTHY under a given resolution rate."""
    disputes_to_resolve: int
    resolution_rate_per_day: float
    days_to_healthy: Optional[float]       # None if resolution rate = 0
    description: str


@dataclass
class HDRThresholdReport:
    """Full HDR threshold analysis."""
    merchant_id: str

    # Current position
    current_rate_pct: float
    current_tier: HDRTier

    # Transactions total
    transactions_90d: int

    # Per-tier thresholds
    tier_thresholds: list[TierThreshold]

    # Closest upcoming threshold
    next_tier: Optional[HDRTier]
    disputes_to_next_tier: Optional[int]

    # Rate projections
    days_to_limited: Optional[float]      # Days until LIMITED if disputes keep opening
    days_to_penalized: Optional[float]    # Days until PENALIZED
    days_to_severe: Optional[float]       # Days until SEVERE

    # Recovery projections
    recovery_at_current_rate: RecoveryScenario
    recovery_scenarios: list[RecoveryScenario]

    # Max safe disputes
    max_disputes_healthy: int        # Max disputes to stay HEALTHY
    max_disputes_elevated: int       # Max disputes to stay below LIMITED
    headroom_disputes: int           # Current room before next tier

    # Guidance
    immediate_actions: list[str]
    summary: str


def calculate_hdr_thresholds(inp: HDRThresholdInput) -> HDRThresholdReport:
    """
    Compute HDR tier thresholds and project crossing times.
    """
    transactions = inp.current_transactions_90d
    disputes = inp.current_disputes_90d
    current_rate = _rate_pct(disputes, transactions)
    current_tier = _classify(current_rate)

    # Per-tier threshold details
    tier_thresholds: list[TierThreshold] = []
    for tier in _TIER_ORDER:
        if tier == HDRTier.HEALTHY:
            continue  # 0% threshold, always "crossed" or starting point
        boundary = _disputes_at_threshold(transactions, tier)
        remaining = boundary - disputes
        crossed = disputes >= boundary
        tier_thresholds.append(TierThreshold(
            tier=tier,
            threshold_pct=_THRESHOLDS_PCT[tier],
            disputes_at_boundary=boundary,
            disputes_remaining=remaining,
            already_crossed=crossed,
        ))

    # Next tier
    next_tier = None
    to_next: Optional[int] = None
    for tt in tier_thresholds:
        if not tt.already_crossed and tt.disputes_remaining > 0:
            next_tier = tt.tier
            to_next = tt.disputes_remaining
            break

    # Days to tier crossings (if dispute rate > 0)
    ndpd = inp.new_disputes_per_day
    days_to_limited: Optional[float] = None
    days_to_penalized: Optional[float] = None
    days_to_severe: Optional[float] = None

    if ndpd > 0:
        for tt in tier_thresholds:
            if not tt.already_crossed:
                days = tt.disputes_remaining / ndpd
                if tt.tier == HDRTier.LIMITED:
                    days_to_limited = round(days, 1)
                elif tt.tier == HDRTier.PENALIZED:
                    days_to_penalized = round(days, 1)
                elif tt.tier == HDRTier.SEVERE:
                    days_to_severe = round(days, 1)

    # Recovery scenarios
    max_healthy = _disputes_at_threshold(transactions, HDRTier.ELEVATED) - 1
    max_elevated = _disputes_at_threshold(transactions, HDRTier.LIMITED) - 1

    disputes_to_healthy = max(0, disputes - max_healthy)

    def make_recovery(rate_per_day: float, label: str) -> RecoveryScenario:
        if rate_per_day <= 0:
            return RecoveryScenario(
                disputes_to_resolve=disputes_to_healthy,
                resolution_rate_per_day=0.0,
                days_to_healthy=None,
                description=f"{label}: no resolution rate — cannot project",
            )
        days = disputes_to_healthy / rate_per_day
        return RecoveryScenario(
            disputes_to_resolve=disputes_to_healthy,
            resolution_rate_per_day=rate_per_day,
            days_to_healthy=round(days, 1),
            description=f"{label}: resolve {disputes_to_healthy} disputes @ {rate_per_day:.1f}/day → healthy in {days:.0f}d",
        )

    recovery_current = make_recovery(inp.disputes_resolved_per_day, "Current resolution rate")
    recovery_scenarios = [
        make_recovery(1.0, "1 dispute/day"),
        make_recovery(2.0, "2 disputes/day"),
        make_recovery(5.0, "5 disputes/day"),
        make_recovery(10.0, "10 disputes/day"),
    ]

    headroom = to_next if to_next is not None else 9999

    # Immediate actions
    actions: list[str] = []
    if current_tier == HDRTier.SEVERE:
        actions.append("SEVERE: Immediate PayPal intervention required")
    elif current_tier == HDRTier.PENALIZED:
        actions.append(f"Fee tier — resolve {disputes_to_healthy} disputes to escape $30 fee")
    elif current_tier == HDRTier.LIMITED:
        actions.append(f"Account limited — {disputes_to_healthy} disputes to age out for HEALTHY")
    elif current_tier == HDRTier.ELEVATED:
        actions.append(f"Warning: {to_next} disputes until LIMITED — take preventive action")
    else:
        if headroom <= 10:
            actions.append(f"Low headroom: only {headroom} disputes before ELEVATED zone")
        else:
            actions.append(f"Healthy: {headroom} dispute buffer before ELEVATED zone")

    if ndpd > 0 and days_to_limited is not None:
        actions.append(f"At current rate ({ndpd:.1f}/day), reach LIMITED in ~{days_to_limited:.0f} days")

    if disputes_to_healthy > 0 and inp.disputes_resolved_per_day > 0:
        d2h = disputes_to_healthy / inp.disputes_resolved_per_day
        actions.append(f"At current resolution rate ({inp.disputes_resolved_per_day:.1f}/day), recover in ~{d2h:.0f} days")

    summary = (
        f"HDRCalc {inp.merchant_id}: "
        f"rate={current_rate:.3f}% ({current_tier.value}) | "
        f"disputes={disputes}/{transactions} | "
        f"to_next={to_next} | to_safe={disputes_to_healthy} | "
        f"max_healthy={max_healthy}"
    )

    return HDRThresholdReport(
        merchant_id=inp.merchant_id,
        current_rate_pct=current_rate,
        current_tier=current_tier,
        transactions_90d=transactions,
        tier_thresholds=tier_thresholds,
        next_tier=next_tier,
        disputes_to_next_tier=to_next,
        days_to_limited=days_to_limited,
        days_to_penalized=days_to_penalized,
        days_to_severe=days_to_severe,
        recovery_at_current_rate=recovery_current,
        recovery_scenarios=recovery_scenarios,
        max_disputes_healthy=max_healthy,
        max_disputes_elevated=max_elevated,
        headroom_disputes=headroom,
        immediate_actions=actions,
        summary=summary,
    )
