"""
HDR Forecast Engine — forecast High Dispute Rate (HDR) trajectory and
estimate when thresholds will be crossed based on current rolling rate,
pending disputes, and fight/accept strategy.

PayPal HDR thresholds (rolling 90-day window):
  Normal:   < 0.50%
  Elevated: 0.50% – 0.99%   (warning)
  Limited:  1.00% – 1.49%   (account restrictions)
  FEE_TIER: 1.50% – 2.99%   ($30 per dispute fee)
  Severe:   ≥ 3.00%          (account suspension risk)

The engine answers:
  1. Current HDR tier
  2. Projected HDR if all pending disputes are filed
  3. How many more disputes can be filed before next threshold
  4. Recommendation: fight, accept, or negotiate based on HDR budget

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class HDRTier(str, Enum):
    NORMAL   = "NORMAL"
    ELEVATED = "ELEVATED"
    LIMITED  = "LIMITED"
    FEE_TIER = "FEE_TIER"
    SEVERE   = "SEVERE"


_THRESHOLDS = [
    (0.0030, HDRTier.SEVERE),
    (0.0150, HDRTier.FEE_TIER),
    (0.0100, HDRTier.LIMITED),
    (0.0050, HDRTier.ELEVATED),
    (0.0,    HDRTier.NORMAL),
]

# Fee schedule (USD per dispute at or above this tier)
_FEE_PER_DISPUTE: dict[HDRTier, float] = {
    HDRTier.NORMAL:   0.0,
    HDRTier.ELEVATED: 0.0,
    HDRTier.LIMITED:  0.0,
    HDRTier.FEE_TIER: 30.0,
    HDRTier.SEVERE:   30.0,
}


def _tier(rate: float) -> HDRTier:
    if rate >= 0.0300:
        return HDRTier.SEVERE
    if rate >= 0.0150:
        return HDRTier.FEE_TIER
    if rate >= 0.0100:
        return HDRTier.LIMITED
    if rate >= 0.0050:
        return HDRTier.ELEVATED
    return HDRTier.NORMAL


def _next_threshold(rate: float) -> Optional[float]:
    """Return the next tier's minimum rate above current, or None if SEVERE."""
    tiers_asc = [0.0050, 0.0100, 0.0150, 0.0300]
    for t in tiers_asc:
        if rate < t:
            return t
    return None


@dataclass
class HDRForecastInput:
    """Input for HDR forecasting."""
    merchant_id: str

    # Current state
    transactions_last_90_days: int
    disputes_last_90_days: int           # Already-counted disputes

    # Pending disputes (not yet decided / filed)
    pending_disputes_count: int = 0
    # Of pending disputes, how many will be "fought" (filed)?
    pending_will_fight: int = 0
    # Of fought disputes, expected wins (wins don't count against HDR)
    pending_fight_expected_wins: int = 0

    # Strategy context
    can_defer_some_disputes: bool = False
    max_deferrable: int = 0


@dataclass
class HDRScenario:
    """An HDR projection under a specific strategy."""
    label: str
    disputes_added: int
    projected_disputes_total: int
    projected_rate: float
    projected_tier: HDRTier
    tier_fee_per_dispute: float
    total_tier_fees: float    # On all disputes in that tier scenario


@dataclass
class HDRForecastReport:
    """Full HDR forecast."""
    merchant_id: str

    current_dispute_count: int
    current_transaction_count: int
    current_rate: float
    current_tier: HDRTier

    # Headroom before next tier
    disputes_until_next_tier: Optional[int]
    next_tier: Optional[HDRTier]
    next_tier_rate: Optional[float]

    # Scenarios
    scenario_fight_all: HDRScenario
    scenario_accept_all: HDRScenario
    scenario_strategic: Optional[HDRScenario]   # Only if deferral possible

    # Recommendations
    hdr_budget_remaining: int     # Disputes that can be filed without tier change
    recommendation: str
    summary: str


def forecast_hdr(inp: HDRForecastInput) -> HDRForecastReport:
    """Forecast HDR trajectory under multiple strategies."""
    if inp.transactions_last_90_days <= 0:
        txn_count = 1   # Avoid division by zero
    else:
        txn_count = inp.transactions_last_90_days

    current_rate = inp.disputes_last_90_days / txn_count
    current_tier = _tier(current_rate)
    next_thresh  = _next_threshold(current_rate)

    # Disputes until next tier
    if next_thresh:
        disputes_at_next = int(next_thresh * txn_count)
        headroom = max(0, disputes_at_next - inp.disputes_last_90_days - 1)
        next_tier = _tier(next_thresh)
    else:
        headroom = 0
        next_tier = HDRTier.SEVERE
        disputes_at_next = None

    # ── Scenario: fight all pending ───────────────────────────────
    # Only losses count against HDR (wins remove dispute from count)
    fight_losses = max(0, inp.pending_will_fight - inp.pending_fight_expected_wins)
    fight_accepted = inp.pending_disputes_count - inp.pending_will_fight
    # Accepts do count against HDR (chargebacks filed by buyer count)
    fight_all_added = fight_losses + fight_accepted
    fight_all_total = inp.disputes_last_90_days + fight_all_added
    fight_all_rate  = fight_all_total / txn_count
    fight_all_tier  = _tier(fight_all_rate)
    fight_fee = _FEE_PER_DISPUTE[fight_all_tier]
    fight_scenario = HDRScenario(
        label="fight_all",
        disputes_added=fight_all_added,
        projected_disputes_total=fight_all_total,
        projected_rate=round(fight_all_rate, 6),
        projected_tier=fight_all_tier,
        tier_fee_per_dispute=fight_fee,
        total_tier_fees=round(fight_fee * fight_all_total, 2),
    )

    # ── Scenario: accept all pending ──────────────────────────────
    # All pending chargebacks land — all count against HDR
    accept_all_total = inp.disputes_last_90_days + inp.pending_disputes_count
    accept_all_rate  = accept_all_total / txn_count
    accept_all_tier  = _tier(accept_all_rate)
    accept_fee = _FEE_PER_DISPUTE[accept_all_tier]
    accept_scenario = HDRScenario(
        label="accept_all",
        disputes_added=inp.pending_disputes_count,
        projected_disputes_total=accept_all_total,
        projected_rate=round(accept_all_rate, 6),
        projected_tier=accept_all_tier,
        tier_fee_per_dispute=accept_fee,
        total_tier_fees=round(accept_fee * accept_all_total, 2),
    )

    # ── Scenario: strategic (defer some) ─────────────────────────
    strategic_scenario = None
    if inp.can_defer_some_disputes and inp.max_deferrable > 0:
        deferred = min(inp.max_deferrable, fight_all_added)
        strategic_added = max(0, fight_all_added - deferred)
        strategic_total = inp.disputes_last_90_days + strategic_added
        strategic_rate  = strategic_total / txn_count
        strategic_tier  = _tier(strategic_rate)
        strat_fee = _FEE_PER_DISPUTE[strategic_tier]
        strategic_scenario = HDRScenario(
            label="strategic_deferral",
            disputes_added=strategic_added,
            projected_disputes_total=strategic_total,
            projected_rate=round(strategic_rate, 6),
            projected_tier=strategic_tier,
            tier_fee_per_dispute=strat_fee,
            total_tier_fees=round(strat_fee * strategic_total, 2),
        )

    # ── Recommendation ────────────────────────────────────────────
    if current_tier == HDRTier.SEVERE:
        rec = "URGENT: Immediately accept and refund all disputes — HDR at SEVERE tier risks account suspension"
    elif headroom == 0:
        rec = "Accept high-value disputes to prevent tier escalation; fight only ironclad cases"
    elif headroom <= 2:
        rec = f"HDR budget tight ({headroom} disputes remaining) — prioritize only strongest cases"
    elif fight_all_tier.value != current_tier.value:
        rec = (f"Fighting all pending would push HDR to {fight_all_tier.value} — "
               "selectively accept disputes near threshold")
    else:
        rec = "HDR headroom sufficient — fight disputes with positive EV as normal"

    summary = (
        f"HDR {inp.merchant_id}: "
        f"rate={current_rate:.3%} ({current_tier.value}) | "
        f"headroom={headroom} | "
        f"fight_all→{fight_all_tier.value} | accept_all→{accept_all_tier.value}"
    )

    return HDRForecastReport(
        merchant_id=inp.merchant_id,
        current_dispute_count=inp.disputes_last_90_days,
        current_transaction_count=txn_count,
        current_rate=round(current_rate, 6),
        current_tier=current_tier,
        disputes_until_next_tier=headroom if next_thresh else None,
        next_tier=next_tier if next_thresh else None,
        next_tier_rate=next_thresh,
        scenario_fight_all=fight_scenario,
        scenario_accept_all=accept_scenario,
        scenario_strategic=strategic_scenario,
        hdr_budget_remaining=headroom,
        recommendation=rec,
        summary=summary,
    )
