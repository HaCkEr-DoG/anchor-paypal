"""
HDR Forecast Modeler — project Dispute Rate over the rolling 90-day window.

PayPal calculates Dispute Rate (DR) as:
  DR = disputes_opened_last_90d / transactions_completed_last_90d

Thresholds (PayPal 2024):
  < 0.50%  — Healthy
  0.50%    — Elevated (warning)
  1.00%    — Limited program access
  1.50%    — $30/dispute surcharge begins
  3.00%    — Severe restrictions
  5.00%    — Terminal / account closure risk

Each dispute that closes (refund issued, case won, or case lost but closed)
DROPS OUT of the rolling window once its opening date is >90 days ago.
New disputes ADD immediately on opening.

This module projects the DR forward by:
  1. Dropping disputes whose 90-day window expires day-by-day
  2. Adding projected new disputes based on daily transaction rate
  3. Identifying which open disputes to resolve fastest to stay below thresholds
  4. Recommending a "close priority order" to minimize peak DR

This is a pure oracle module — no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math


class HDRZone(str, Enum):
    HEALTHY       = "HEALTHY"      # DR < 0.50%
    ELEVATED      = "ELEVATED"     # 0.50% <= DR < 1.00%
    LIMITED       = "LIMITED"      # 1.00% <= DR < 1.50%
    SURCHARGE     = "SURCHARGE"    # 1.50% <= DR < 3.00%
    SEVERE        = "SEVERE"       # 3.00% <= DR < 5.00%
    TERMINAL      = "TERMINAL"     # DR >= 5.00%


_ZONE_THRESHOLDS = [
    (0.0050, HDRZone.ELEVATED),
    (0.0100, HDRZone.LIMITED),
    (0.0150, HDRZone.SURCHARGE),
    (0.0300, HDRZone.SEVERE),
    (0.0500, HDRZone.TERMINAL),
]


def _zone_from_rate(dr: float) -> HDRZone:
    for threshold, zone in _ZONE_THRESHOLDS:
        if dr < threshold:
            return HDRZone.HEALTHY if zone == HDRZone.ELEVATED else _prev_zone(zone)
    return HDRZone.TERMINAL


def _prev_zone(zone: HDRZone) -> HDRZone:
    order = [HDRZone.HEALTHY, HDRZone.ELEVATED, HDRZone.LIMITED, HDRZone.SURCHARGE, HDRZone.SEVERE, HDRZone.TERMINAL]
    idx = order.index(zone)
    return order[max(0, idx - 1)]


def _zone_from_rate_direct(dr: float) -> HDRZone:
    if dr < 0.0050:
        return HDRZone.HEALTHY
    if dr < 0.0100:
        return HDRZone.ELEVATED
    if dr < 0.0150:
        return HDRZone.LIMITED
    if dr < 0.0300:
        return HDRZone.SURCHARGE
    if dr < 0.0500:
        return HDRZone.SEVERE
    return HDRZone.TERMINAL


@dataclass
class OpenDispute:
    """An open dispute in the merchant's portfolio."""
    dispute_id: str
    opened_days_ago: int         # How many days ago this was opened (0 = today)
    dispute_amount: float
    claim_type: str
    is_hdr_contributing: bool = True
    days_to_resolve: int = 7     # How long it will take to resolve if prioritized


@dataclass
class HDRForecastInput:
    """
    Current portfolio state for HDR projection.
    """
    # Current window counts
    disputes_last_90d: int        # Count of disputes opened in past 90 days
    transactions_last_90d: int    # Count of transactions in past 90 days

    # Open disputes with timing (for expiry projection)
    open_disputes: list[OpenDispute] = field(default_factory=list)

    # Transaction velocity
    daily_transaction_rate: float = 0.0   # Transactions per day
    daily_new_dispute_rate: float = 0.0   # New disputes per day (baseline)

    # Forecast horizon
    forecast_days: int = 30

    # Disputes already scheduled to close (refund issued, response submitted)
    disputes_resolving_in_days: list[tuple[str, int]] = field(default_factory=list)
    # [(dispute_id, days_until_closed)]


@dataclass
class DayForecast:
    """Projected DR for a single future day."""
    day: int                     # Days from today
    dispute_count: int
    transaction_count: int
    dispute_rate: float          # 0.0–1.0 (not percentage)
    dispute_rate_pct: float      # As percentage (0.0–100.0)
    zone: HDRZone
    disputes_expiring_today: int
    new_disputes_today: float


@dataclass
class ThresholdCrossing:
    """When the DR will cross a threshold."""
    day: int
    from_zone: HDRZone
    to_zone: HDRZone
    dispute_rate_pct: float
    direction: str               # "UP" or "DOWN"


@dataclass
class CloseRecommendation:
    """A dispute to close to stay below a threshold."""
    dispute_id: str
    claim_type: str
    dispute_amount: float
    priority_rank: int           # 1 = highest priority
    rationale: str
    dr_impact_pct: float         # How much closing this reduces DR (in pp)
    days_to_resolve: int


@dataclass
class HDRForecastReport:
    """
    Complete HDR forecast with projections and recommendations.
    """
    # Current state
    current_dispute_rate: float
    current_dispute_rate_pct: float
    current_zone: HDRZone
    disputes_in_window: int
    transactions_in_window: int

    # Projections
    daily_forecast: list[DayForecast]
    peak_rate_pct: float
    peak_rate_day: int
    peak_zone: HDRZone

    # Threshold crossings
    threshold_crossings: list[ThresholdCrossing]

    # Days until specific thresholds (None if not crossed in forecast horizon)
    days_until_elevated: Optional[int]
    days_until_limited: Optional[int]
    days_until_surcharge: Optional[int]

    # Close recommendations
    close_recommendations: list[CloseRecommendation]

    # Capacity
    disputes_can_absorb_before_elevated: int   # How many new disputes before crossing 0.5%
    disputes_can_absorb_before_limited: int    # How many new disputes before crossing 1.0%

    # Summary
    trajectory: str              # IMPROVING / STABLE / WORSENING
    summary: str
    recommendations: list[str]


def _compute_dr(disputes: int, transactions: int) -> float:
    if transactions <= 0:
        return 0.0
    return disputes / transactions


def _days_until_threshold_crossed(
    daily_forecast: list[DayForecast],
    threshold: float,
    direction: str = "UP",
) -> Optional[int]:
    for df in daily_forecast:
        if direction == "UP" and df.dispute_rate >= threshold:
            return df.day
        if direction == "DOWN" and df.dispute_rate < threshold:
            return df.day
    return None


def forecast_hdr(inp: HDRForecastInput) -> HDRForecastReport:
    """
    Project the HDR over the next N days and recommend which disputes to close.
    """
    base_disputes = inp.disputes_last_90d
    base_txns = inp.transactions_last_90d

    current_dr = _compute_dr(base_disputes, base_txns)
    current_zone = _zone_from_rate_direct(current_dr)

    # Build expiry schedule: disputes expiring by day (they drop out when >90d old)
    # A dispute opened X days ago will expire in (90 - X) days
    expiry_by_day: dict[int, int] = {}
    for d in inp.open_disputes:
        if not d.is_hdr_contributing:
            continue
        days_until_expiry = 90 - d.opened_days_ago
        if days_until_expiry <= 0:
            continue  # Already expired (should not be in window)
        if days_until_expiry <= inp.forecast_days:
            expiry_by_day[days_until_expiry] = expiry_by_day.get(days_until_expiry, 0) + 1

    # Build resolution schedule (disputes being closed)
    resolution_by_day: dict[int, int] = {}
    resolve_set = {d_id for d_id, _ in inp.disputes_resolving_in_days}
    for d_id, days_to_close in inp.disputes_resolving_in_days:
        if days_to_close <= inp.forecast_days:
            resolution_by_day[days_to_close] = resolution_by_day.get(days_to_close, 0) + 1

    daily_forecast: list[DayForecast] = []
    running_disputes = base_disputes
    running_txns = base_txns
    daily_new = inp.daily_new_dispute_rate
    daily_txn = inp.daily_transaction_rate if inp.daily_transaction_rate > 0 else (base_txns / 90.0)

    for day in range(1, inp.forecast_days + 1):
        # Transactions grow each day (rolling window adds today, drops 90d ago)
        running_txns = running_txns + daily_txn - daily_txn  # Net zero in steady state
        # Actually: rolling 90d window adds today's txns, drops txns from 91 days ago
        # Approximate: steady state → running_txns ≈ base_txns
        # For simplicity keep it stable (the more important variable is disputes)

        # Disputes expiring today (opened 90+ days ago → drop out)
        expiring = expiry_by_day.get(day, 0)
        # Disputes being resolved today
        resolving = resolution_by_day.get(day, 0)
        # New disputes arriving today
        new_today = daily_new

        running_disputes = max(0, running_disputes - expiring - resolving + new_today)
        dr = _compute_dr(int(running_disputes + 0.5), int(running_txns + 0.5))
        zone = _zone_from_rate_direct(dr)

        daily_forecast.append(DayForecast(
            day=day,
            dispute_count=int(running_disputes + 0.5),
            transaction_count=int(running_txns + 0.5),
            dispute_rate=dr,
            dispute_rate_pct=round(dr * 100, 4),
            zone=zone,
            disputes_expiring_today=expiring,
            new_disputes_today=new_today,
        ))

    # Peak
    peak_day_forecast = max(daily_forecast, key=lambda x: x.dispute_rate)
    peak_rate = peak_day_forecast.dispute_rate
    peak_day = peak_day_forecast.day
    peak_zone = peak_day_forecast.zone

    # Threshold crossings
    crossings: list[ThresholdCrossing] = []
    prev_zone = current_zone
    for df in daily_forecast:
        if df.zone != prev_zone:
            crossings.append(ThresholdCrossing(
                day=df.day,
                from_zone=prev_zone,
                to_zone=df.zone,
                dispute_rate_pct=df.dispute_rate_pct,
                direction="UP" if df.dispute_rate > _compute_dr(base_disputes, base_txns) else "DOWN",
            ))
            prev_zone = df.zone

    days_until_elevated = _days_until_threshold_crossed(daily_forecast, 0.0050)
    days_until_limited = _days_until_threshold_crossed(daily_forecast, 0.0100)
    days_until_surcharge = _days_until_threshold_crossed(daily_forecast, 0.0150)

    # Absorb capacity
    # disputes_can_absorb_before_X = floor(txns × threshold) - current_disputes
    absorb_elevated = max(0, int(base_txns * 0.0050) - base_disputes)
    absorb_limited = max(0, int(base_txns * 0.0100) - base_disputes)

    # Close recommendations: rank open disputes by DR impact
    close_recs: list[CloseRecommendation] = []
    dr_per_dispute = 1.0 / max(base_txns, 1) * 100  # pp impact per dispute closed
    sortable = sorted(
        [d for d in inp.open_disputes if d.is_hdr_contributing and d.dispute_id not in resolve_set],
        key=lambda d: d.opened_days_ago,  # Oldest first — will expire soonest anyway
        reverse=True,
    )
    for rank, d in enumerate(sortable[:10], 1):
        days_left_in_window = 90 - d.opened_days_ago
        if days_left_in_window <= 0:
            rationale = "Already expired from window"
        elif days_left_in_window <= 14:
            rationale = f"Expires from window in {days_left_in_window}d — deprioritize"
        elif current_dr >= 0.0150:
            rationale = "Portfolio in SURCHARGE zone — close immediately to reduce fees"
        elif current_dr >= 0.0100:
            rationale = "Portfolio in LIMITED zone — close to restore account access"
        elif current_dr >= 0.0050:
            rationale = "Portfolio in ELEVATED zone — close to stay below 1% threshold"
        else:
            rationale = "Close proactively to maintain healthy DR"

        close_recs.append(CloseRecommendation(
            dispute_id=d.dispute_id,
            claim_type=d.claim_type,
            dispute_amount=d.dispute_amount,
            priority_rank=rank,
            rationale=rationale,
            dr_impact_pct=round(dr_per_dispute, 4),
            days_to_resolve=d.days_to_resolve,
        ))

    # Trajectory
    if len(daily_forecast) >= 7:
        early_avg = sum(d.dispute_rate for d in daily_forecast[:7]) / 7
        late_avg = sum(d.dispute_rate for d in daily_forecast[-7:]) / 7
        if late_avg < early_avg * 0.95:
            trajectory = "IMPROVING"
        elif late_avg > early_avg * 1.05:
            trajectory = "WORSENING"
        else:
            trajectory = "STABLE"
    else:
        trajectory = "STABLE"

    # Recommendations
    recs: list[str] = []
    if current_zone == HDRZone.TERMINAL:
        recs.append("CRITICAL: DR >= 5% — immediate bulk refund of all closable disputes required")
    elif current_zone == HDRZone.SEVERE:
        recs.append(f"SEVERE: DR={current_dr*100:.2f}% — close {absorb_limited} disputes to reach LIMITED zone")
    elif current_zone == HDRZone.SURCHARGE:
        recs.append(f"SURCHARGE fees active at DR={current_dr*100:.2f}% — close {absorb_limited} disputes to exit surcharge zone")
    elif current_zone == HDRZone.LIMITED:
        recs.append(f"Account access LIMITED at DR={current_dr*100:.2f}% — close disputes to reach ELEVATED")
    elif current_zone == HDRZone.ELEVATED:
        recs.append(f"DR={current_dr*100:.2f}% elevated — {absorb_limited} disputes buffer before 1% threshold")
    else:
        recs.append(f"DR={current_dr*100:.2f}% healthy — {absorb_elevated} new disputes before ELEVATED threshold")

    if daily_new > 0 and absorb_elevated > 0:
        days_to_elevated = absorb_elevated / daily_new if daily_new > 0 else 999
        if days_to_elevated < 14:
            recs.append(f"At current dispute velocity ({daily_new:.1f}/day), will reach ELEVATED in ~{days_to_elevated:.0f} days")

    if peak_zone in (HDRZone.SURCHARGE, HDRZone.SEVERE, HDRZone.TERMINAL):
        recs.append(f"Forecast peak of {peak_rate*100:.2f}% ({peak_zone.value}) reached on day {peak_day}")

    summary = (
        f"HDR Forecast: current={current_dr*100:.3f}% ({current_zone.value}) | "
        f"peak={peak_rate*100:.3f}% ({peak_zone.value}) on day {peak_day} | "
        f"trajectory={trajectory} | "
        f"absorb_before_elevated={absorb_elevated} disputes | "
        f"crossings={len(crossings)}"
    )

    return HDRForecastReport(
        current_dispute_rate=current_dr,
        current_dispute_rate_pct=round(current_dr * 100, 4),
        current_zone=current_zone,
        disputes_in_window=base_disputes,
        transactions_in_window=base_txns,
        daily_forecast=daily_forecast,
        peak_rate_pct=round(peak_rate * 100, 4),
        peak_rate_day=peak_day,
        peak_zone=peak_zone,
        threshold_crossings=crossings,
        days_until_elevated=days_until_elevated,
        days_until_limited=days_until_limited,
        days_until_surcharge=days_until_surcharge,
        close_recommendations=close_recs,
        disputes_can_absorb_before_elevated=absorb_elevated,
        disputes_can_absorb_before_limited=absorb_limited,
        trajectory=trajectory,
        summary=summary,
        recommendations=recs,
    )
