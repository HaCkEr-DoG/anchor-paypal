"""
Chargeback Rate Forecaster — project your dispute rate 30/60/90 days forward.

The fundamental problem with reactive dispute management: merchants react to
threshold crossings after they happen. By then, the fees are already piling up,
and recovering takes 90 days (the rolling window length).

This module projects forward: given your current dispute trend, your open order
pipeline, and seasonal patterns, when will you cross each threshold? What specific
interventions will prevent it?

Forecast methodology:
  1. Linear trend: fit a simple slope to weekly dispute rates
  2. Pipeline risk: score open orders (using order_risk.py signals) to estimate
     how many will generate disputes
  3. Seasonal adjustment: Jan (post-holiday returns), Aug (back-to-school),
     Nov (holiday spike) are known high-dispute periods
  4. Intervention impact: if you add signature confirmation, estimate how much
     the dispute rate drops

The output is actionable: "At current trajectory you'll cross 1.50% in 18 days.
If you require signature confirmation on ELECTRONICS orders today, you'll stay
under the threshold for 90+ days."

This is the difference between getting hit with the HDR fee vs preventing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ForecastRisk(str, Enum):
    LOW      = "LOW"      # Projected to stay well below all thresholds
    MODERATE = "MODERATE" # May approach threshold within 90 days
    HIGH     = "HIGH"     # Will cross a threshold within 60 days
    CRITICAL = "CRITICAL" # Will cross a threshold within 30 days


@dataclass
class WeeklySnapshot:
    """One week of transaction and dispute data."""
    week_number: int         # 1 = most recent, 13 = 13 weeks ago
    transactions: int
    disputes_filed: int
    dispute_rate: float = 0.0  # Computed if 0


@dataclass
class OpenOrderPipeline:
    """In-flight orders that could generate disputes."""
    total_open_orders: int = 0
    high_risk_orders: int = 0     # From order_risk.py CRITICAL/HIGH
    medium_risk_orders: int = 0   # MEDIUM
    low_risk_orders: int = 0      # LOW
    # Average order value in pipeline
    avg_order_value: float = 50.0


@dataclass
class InterventionScenario:
    """A specific action and its projected rate impact."""
    name: str
    description: str
    dispute_rate_reduction: float   # Absolute percentage reduction (0.0–1.0)
    cost_per_order: float           # Cost to implement per order
    orders_affected_per_month: int  # How many orders this applies to


@dataclass
class ThresholdCrossing:
    """Projected threshold crossing event."""
    threshold_name: str
    threshold_rate: float
    projected_crossing_day: int     # Days from today (999 = won't cross in window)
    crossing_risk: ForecastRisk
    disputes_causing_crossing: int  # How many disputes push us over


@dataclass
class ChargebackForecast:
    """30/60/90-day chargeback rate forecast with intervention analysis."""
    # Current state
    current_dispute_rate: float
    trend_slope_per_week: float      # Rate change per week (negative = improving)
    trend_direction: str             # "IMPROVING" / "STABLE" / "WORSENING"
    # Projections (baseline — no interventions)
    projected_rate_30d: float
    projected_rate_60d: float
    projected_rate_90d: float
    # Threshold crossings
    threshold_crossings: list[ThresholdCrossing]
    next_crossing: Optional[ThresholdCrossing]
    # Pipeline risk
    pipeline_dispute_estimate_30d: int
    pipeline_dispute_estimate_90d: int
    # Intervention scenarios
    interventions: list[InterventionScenario]
    # With best intervention applied
    best_intervention_name: str
    projected_rate_30d_with_intervention: float
    projected_rate_90d_with_intervention: float
    forecast_risk: ForecastRisk
    # Financial projection
    hdr_fees_projected_30d: float    # 0 if below 1.50% threshold
    hdr_fees_projected_90d: float
    hdr_fees_avoidable: float        # How much intervention saves in fees
    # Summary
    summary: str
    action_items: list[str]
    reasoning: list[str] = field(default_factory=list)


# PayPal thresholds (rate → fee)
_THRESHOLDS = [
    (0.0050, "ELEVATED (0.50%)",   0.0),
    (0.0100, "HIGH (1.00%)",       0.0),
    (0.0150, "EXCESSIVE (1.50%)", 30.0),
    (0.0300, "CRITICAL (3.00%)",  30.0),
]

# Seasonal dispute multipliers by month (1–12)
_SEASONAL_MULTIPLIERS = {
    1: 1.35,  # January: post-holiday returns + chargebacks
    2: 1.10,  # February: holiday dispute window closing
    3: 1.00,
    4: 0.95,
    5: 0.95,
    6: 1.00,
    7: 0.95,
    8: 1.10,  # August: back-to-school
    9: 1.00,
    10: 1.05,
    11: 1.20,  # November: holiday shopping surge
    12: 1.25,  # December: holiday disputes starting
}

# Default intervention scenarios (merchant can customize)
_DEFAULT_INTERVENTIONS = [
    InterventionScenario(
        name="Signature Confirmation (All)",
        description="Require signature on all orders ≥ $100",
        dispute_rate_reduction=0.0025,
        cost_per_order=5.0,
        orders_affected_per_month=200,
    ),
    InterventionScenario(
        name="CRITICAL Order Pause",
        description="Pause fulfillment of CRITICAL risk orders pending manual review",
        dispute_rate_reduction=0.0040,
        cost_per_order=15.0,
        orders_affected_per_month=50,
    ),
    InterventionScenario(
        name="Alert Interceptor Activation",
        description="Activate Ethoca/Verifi alert handling — refund before chargeback hits count",
        dispute_rate_reduction=0.0015,
        cost_per_order=3.5,
        orders_affected_per_month=150,
    ),
    InterventionScenario(
        name="Evidence Documentation Protocol",
        description="Pre-shipment photos + tracking for all orders, reduces INR disputes",
        dispute_rate_reduction=0.0020,
        cost_per_order=2.0,
        orders_affected_per_month=300,
    ),
]


def forecast_chargeback_rate(
    weekly_snapshots: list[WeeklySnapshot],
    pipeline: OpenOrderPipeline,
    current_month: int = 11,
    weekly_transactions_avg: int = 150,
    interventions: Optional[list[InterventionScenario]] = None,
    hdr_active: bool = False,
) -> ChargebackForecast:
    """
    Project dispute rate 30/60/90 days forward.

    weekly_snapshots: ordered most-recent first (week 1 = last week)
    current_month: 1–12 for seasonal adjustment
    """
    reasoning: list[str] = []
    if interventions is None:
        interventions = _DEFAULT_INTERVENTIONS

    # Normalize snapshots
    for snap in weekly_snapshots:
        if snap.dispute_rate == 0.0 and snap.transactions > 0:
            snap.dispute_rate = snap.disputes_filed / snap.transactions

    # ── 1. Current rate and trend ─────────────────────────────────────────────
    if not weekly_snapshots:
        current_rate = 0.0
        slope = 0.0
    else:
        recent = weekly_snapshots[:8]  # Last 8 weeks for trend
        current_rate = recent[0].dispute_rate if recent else 0.0

        # Linear regression slope (dispute rate vs time)
        n = len(recent)
        if n >= 3:
            xs = list(range(n))
            ys = [s.dispute_rate for s in recent]
            mean_x = sum(xs) / n
            mean_y = sum(ys) / n
            cov_xy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
            var_x = sum((x - mean_x) ** 2 for x in xs) or 1
            slope = cov_xy / var_x  # Rate change per week (negative = improving)
        else:
            slope = 0.0

    reasoning.append(f"Current dispute rate: {current_rate:.3%}")
    reasoning.append(f"Weekly trend: {slope:+.4%} per week")

    # Trend direction: snapshots are ordered most-recent-first (index 0 = now).
    # Negative slope means rate decreases as we look further back = rate was lower
    # in the past, is higher now = WORSENING. Positive slope = IMPROVING.
    if slope < -0.00005:
        trend = "WORSENING"
    elif slope > 0.00005:
        trend = "IMPROVING"
    else:
        trend = "STABLE"

    # ── 2. Seasonal adjustment ────────────────────────────────────────────────
    seasonal_mult = _SEASONAL_MULTIPLIERS.get(current_month, 1.0)
    reasoning.append(f"Seasonal multiplier for month {current_month}: ×{seasonal_mult:.2f}")

    # ── 3. Project baseline rates ─────────────────────────────────────────────
    # Simple linear projection with seasonal adjustment
    # 30d ≈ 4.3 weeks, 60d ≈ 8.6 weeks, 90d ≈ 13 weeks
    def project_rate(weeks_ahead: float) -> float:
        raw = current_rate + slope * weeks_ahead * seasonal_mult
        return max(0.001, min(raw, 0.15))  # Clamp to realistic range

    proj_30d = project_rate(4.3)
    proj_60d = project_rate(8.6)
    proj_90d = project_rate(13.0)

    reasoning.append(
        f"Baseline projection: 30d={proj_30d:.3%}, 60d={proj_60d:.3%}, 90d={proj_90d:.3%}"
    )

    # ── 4. Pipeline risk contribution ────────────────────────────────────────
    # Estimate disputes from open pipeline
    pipeline_dispute_rate = (
        pipeline.high_risk_orders * 0.18 +
        pipeline.medium_risk_orders * 0.04 +
        pipeline.low_risk_orders * 0.01
    ) / max(pipeline.total_open_orders, 1)

    pipeline_disputes_30d = int(
        pipeline.high_risk_orders * 0.18 +
        pipeline.medium_risk_orders * 0.04 +
        pipeline.low_risk_orders * 0.01
    )
    pipeline_disputes_90d = pipeline_disputes_30d * 2  # Rough 90d estimate

    reasoning.append(
        f"Pipeline: {pipeline.high_risk_orders} high-risk orders → "
        f"~{pipeline_disputes_30d} disputes expected"
    )

    # ── 5. Threshold crossing analysis ───────────────────────────────────────
    crossings: list[ThresholdCrossing] = []
    for thresh_rate, thresh_name, _ in _THRESHOLDS:
        if current_rate >= thresh_rate:
            continue  # Already crossed
        # Binary search for crossing day
        crossing_day = _find_crossing_day(current_rate, slope, seasonal_mult, thresh_rate)
        if crossing_day <= 90:
            risk = (
                ForecastRisk.CRITICAL if crossing_day <= 30 else
                ForecastRisk.HIGH     if crossing_day <= 60 else
                ForecastRisk.MODERATE
            )
            # Disputes that push us over
            disputes_to_crossing = max(0, int(
                (thresh_rate - current_rate) * weekly_transactions_avg * (crossing_day / 7)
            ))
            crossings.append(ThresholdCrossing(
                threshold_name=thresh_name,
                threshold_rate=thresh_rate,
                projected_crossing_day=crossing_day,
                crossing_risk=risk,
                disputes_causing_crossing=disputes_to_crossing,
            ))

    next_crossing = crossings[0] if crossings else None

    # ── 6. Intervention analysis ──────────────────────────────────────────────
    # Find best single intervention
    best_intervention = max(
        interventions,
        key=lambda i: i.dispute_rate_reduction,
        default=None,
    )
    best_name = best_intervention.name if best_intervention else "None"
    best_reduction = best_intervention.dispute_rate_reduction if best_intervention else 0.0

    proj_30d_with_intervention = max(0.001, proj_30d - best_reduction)
    proj_90d_with_intervention = max(0.001, proj_90d - best_reduction)

    # ── 7. Financial projection ───────────────────────────────────────────────
    # HDR fees if rate stays at projected level
    weekly_disputes_projected = slope * 4.3 + (current_rate * weekly_transactions_avg)
    monthly_disputes = max(0, weekly_disputes_projected * 4.3)

    hdr_30d = monthly_disputes * 30.0 if proj_30d >= 0.015 else 0.0
    hdr_90d = monthly_disputes * 30.0 * 3 if proj_90d >= 0.015 else 0.0
    hdr_avoidable = hdr_90d if best_reduction > 0 and proj_30d_with_intervention < 0.015 else 0.0

    # ── 8. Forecast risk ──────────────────────────────────────────────────────
    if next_crossing and next_crossing.crossing_risk == ForecastRisk.CRITICAL:
        forecast_risk = ForecastRisk.CRITICAL
    elif next_crossing and next_crossing.crossing_risk == ForecastRisk.HIGH:
        forecast_risk = ForecastRisk.HIGH
    elif next_crossing:
        forecast_risk = ForecastRisk.MODERATE
    else:
        forecast_risk = ForecastRisk.LOW

    # ── 9. Summary and actions ────────────────────────────────────────────────
    summary = _build_summary(
        current_rate, proj_30d, proj_90d, next_crossing, trend, hdr_30d
    )
    action_items = _build_action_items(
        forecast_risk, next_crossing, best_intervention, hdr_avoidable
    )

    return ChargebackForecast(
        current_dispute_rate=round(current_rate, 5),
        # Negate so positive = rate going UP over time (WORSENING), negative = IMPROVING
        trend_slope_per_week=round(-slope, 6),
        trend_direction=trend,
        projected_rate_30d=round(proj_30d, 5),
        projected_rate_60d=round(proj_60d, 5),
        projected_rate_90d=round(proj_90d, 5),
        threshold_crossings=crossings,
        next_crossing=next_crossing,
        pipeline_dispute_estimate_30d=pipeline_disputes_30d,
        pipeline_dispute_estimate_90d=pipeline_disputes_90d,
        interventions=interventions,
        best_intervention_name=best_name,
        projected_rate_30d_with_intervention=round(proj_30d_with_intervention, 5),
        projected_rate_90d_with_intervention=round(proj_90d_with_intervention, 5),
        forecast_risk=forecast_risk,
        hdr_fees_projected_30d=round(hdr_30d, 2),
        hdr_fees_projected_90d=round(hdr_90d, 2),
        hdr_fees_avoidable=round(hdr_avoidable, 2),
        summary=summary,
        action_items=action_items,
        reasoning=reasoning,
    )


def _find_crossing_day(
    current_rate: float,
    slope: float,
    seasonal_mult: float,
    threshold: float,
) -> int:
    """Binary search for the first day the projected rate crosses threshold."""
    if current_rate >= threshold:
        return 0
    for day in range(1, 91):
        weeks = day / 7
        projected = current_rate + slope * weeks * seasonal_mult
        if projected >= threshold:
            return day
    return 999  # Won't cross in 90 days


def _build_summary(
    current_rate: float,
    proj_30d: float,
    proj_90d: float,
    next_crossing: Optional[ThresholdCrossing],
    trend: str,
    hdr_30d: float,
) -> str:
    if next_crossing and next_crossing.projected_crossing_day <= 30:
        return (
            f"CRITICAL: At current trajectory, you will cross {next_crossing.threshold_name} "
            f"in {next_crossing.projected_crossing_day} days. "
            f"{'HDR fee will add $30/dispute on all disputes.' if next_crossing.threshold_rate >= 0.015 else ''}"
        )
    elif next_crossing and next_crossing.projected_crossing_day <= 60:
        return (
            f"WARNING: Trending toward {next_crossing.threshold_name} "
            f"({next_crossing.projected_crossing_day} days). "
            f"Trend is {trend.lower()}."
        )
    elif hdr_30d > 0:
        return (
            f"HDR fee active: ${hdr_30d:.0f}/month projected. "
            f"Rate trending {trend.lower()} from {current_rate:.2%} to {proj_90d:.2%} in 90 days."
        )
    else:
        return (
            f"Healthy: Rate {current_rate:.2%} → {proj_90d:.2%} (90d forecast). "
            f"Trend: {trend}. No threshold crossings projected."
        )


def _build_action_items(
    risk: ForecastRisk,
    next_crossing: Optional[ThresholdCrossing],
    best_intervention: Optional[InterventionScenario],
    hdr_avoidable: float,
) -> list[str]:
    items: list[str] = []
    if risk == ForecastRisk.CRITICAL:
        items.append("IMMEDIATE: Pause high-risk orders pending review")
        if best_intervention:
            items.append(
                f"IMMEDIATE: Activate '{best_intervention.name}' "
                f"(−{best_intervention.dispute_rate_reduction:.2%} rate)"
            )
    elif risk == ForecastRisk.HIGH:
        if best_intervention:
            items.append(
                f"This week: Activate '{best_intervention.name}' to prevent "
                f"{next_crossing.threshold_name if next_crossing else 'threshold'} crossing"
            )
    if hdr_avoidable > 0:
        items.append(
            f"Intervention can save ${hdr_avoidable:.0f} in HDR fees over 90 days"
        )
    items.append("Weekly: Review merchant_health.py report for rate trajectory")
    items.append("Monthly: Run dispute_dna.py to identify systemic dispute sources")
    return items


if __name__ == "__main__":
    # Scenario: worsening trend, approaching 1.50% threshold
    snapshots = [
        WeeklySnapshot(1, transactions=160, disputes_filed=3, dispute_rate=0.0),
        WeeklySnapshot(2, transactions=155, disputes_filed=2),
        WeeklySnapshot(3, transactions=158, disputes_filed=2),
        WeeklySnapshot(4, transactions=150, disputes_filed=2),
        WeeklySnapshot(5, transactions=152, disputes_filed=2),
        WeeklySnapshot(6, transactions=148, disputes_filed=1),
        WeeklySnapshot(7, transactions=155, disputes_filed=1),
        WeeklySnapshot(8, transactions=160, disputes_filed=1),
    ]
    # Pre-compute rates
    for s in snapshots:
        if s.dispute_rate == 0.0:
            s.dispute_rate = s.disputes_filed / s.transactions

    pipeline = OpenOrderPipeline(
        total_open_orders=400,
        high_risk_orders=25,
        medium_risk_orders=80,
        low_risk_orders=295,
    )

    forecast = forecast_chargeback_rate(
        snapshots, pipeline, current_month=11, weekly_transactions_avg=155
    )
    print(f"Current rate: {forecast.current_dispute_rate:.3%}")
    print(f"Trend: {forecast.trend_direction} ({forecast.trend_slope_per_week:+.4%}/week)")
    print(f"30d projection: {forecast.projected_rate_30d:.3%}")
    print(f"90d projection: {forecast.projected_rate_90d:.3%}")
    print(f"\nForecast risk: {forecast.forecast_risk.value}")
    print(f"\nSummary: {forecast.summary}")
    if forecast.threshold_crossings:
        print(f"\nThreshold crossings:")
        for c in forecast.threshold_crossings:
            print(f"  {c.threshold_name}: day {c.projected_crossing_day} ({c.crossing_risk.value})")
    print(f"\nBest intervention: {forecast.best_intervention_name}")
    print(f"With intervention: 30d={forecast.projected_rate_30d_with_intervention:.3%}")
