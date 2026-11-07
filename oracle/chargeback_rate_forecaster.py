"""
Chargeback Rate Forecaster — predicts next-30-day chargeback rate for a merchant.

Uses three trend signals:
  1. Historical dispute rate (disputes / transactions) over trailing windows
  2. Trend direction: is the rate rising, flat, or falling?
  3. Claim-type risk mix: more UNAUTH disputes → higher forecast multiplier

Output:
  - forecast_rate: predicted chargeback rate for the next 30 days (disputes/txn)
  - confidence_band: (low, high) based on rate volatility
  - trend: RISING / FLAT / FALLING
  - alert_level: CRITICAL / WARNING / NORMAL / HEALTHY
  - contributing_factors: list of human-readable drivers
  - recommendation: concrete action to reduce risk

PayPal benefit:
  - Merchants can intervene before chargeback rate triggers PayPal thresholds
  - PayPal chargeback threshold is ~1.5% (above triggers monitoring programme)
  - Early-warning forecasts give 30-day runway to course-correct
  - Trend analysis separates seasonal noise from real deterioration
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


# ── Constants ─────────────────────────────────────────────────────────────────

_PAYPAL_WARNING_THRESHOLD = 0.015    # 1.5% — PayPal chargeback monitoring level
_PAYPAL_CRITICAL_THRESHOLD = 0.020   # 2.0% — programme entry
_TREND_RISE_THRESHOLD = 0.20         # >20% relative increase → RISING
_TREND_FALL_THRESHOLD = -0.20        # >20% relative decrease → FALLING

# Claim-type risk multipliers applied to base forecast
_CLAIM_RISK_MULTIPLIERS = {
    "UNAUTH": 1.30,  # unauthorized disputes inflate future CB risk the most
    "INR":    1.10,
    "SNAD":   1.05,
    "OTHER":  1.00,
}


# ── Enums ─────────────────────────────────────────────────────────────────────

class RateTrend(str, Enum):
    RISING  = "RISING"
    FLAT    = "FLAT"
    FALLING = "FALLING"


class AlertLevel(str, Enum):
    HEALTHY  = "HEALTHY"   # < 0.5%
    NORMAL   = "NORMAL"    # 0.5–1.0%
    WARNING  = "WARNING"   # 1.0–1.5%
    CRITICAL = "CRITICAL"  # ≥ 1.5%


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class MonthlyRecord:
    """Dispute and transaction counts for a single calendar month (oldest first)."""
    month_label: str          # e.g. "2025-01"
    total_transactions: int
    total_disputes: int
    unauth_disputes: int = 0
    inr_disputes: int = 0
    snad_disputes: int = 0


@dataclass
class ChargebackForecastInput:
    merchant_id: str
    monthly_records: List[MonthlyRecord]   # chronological, oldest first, ≥1 month
    high_season_active: bool = False       # e.g. Black Friday / holiday spike expected
    new_product_launch: bool = False       # launches correlate with higher SNAD rates


@dataclass
class ChargebackForecastReport:
    merchant_id: str
    base_rate: float                           # trailing-3-month average dispute rate
    trend: RateTrend
    trend_magnitude: float                     # relative change, e.g. 0.25 = +25%
    forecast_rate: float                       # adjusted prediction for next 30 days
    confidence_low: float                      # 10th-percentile estimate
    confidence_high: float                     # 90th-percentile estimate
    alert_level: AlertLevel
    contributing_factors: List[str] = field(default_factory=list)
    recommendation: str = ""
    days_to_threshold: int = -1               # -1 if not on track to breach


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dispute_rate(record: MonthlyRecord) -> float:
    if record.total_transactions <= 0:
        return 0.0
    return record.total_disputes / record.total_transactions


def _trailing_avg(records: List[MonthlyRecord], n: int) -> float:
    """Average dispute rate over the last n months."""
    tail = records[-n:] if len(records) >= n else records
    if not tail:
        return 0.0
    return sum(_dispute_rate(r) for r in tail) / len(tail)


def _unauth_fraction(records: List[MonthlyRecord]) -> float:
    """Fraction of recent disputes that are UNAUTH (last 3 months)."""
    tail = records[-3:]
    total_disputes = sum(r.total_disputes for r in tail)
    unauth = sum(r.unauth_disputes for r in tail)
    if total_disputes == 0:
        return 0.0
    return unauth / total_disputes


def _claim_risk_multiplier(records: List[MonthlyRecord]) -> float:
    """Weighted average risk multiplier based on claim mix."""
    tail = records[-3:]
    total = sum(r.total_disputes for r in tail)
    if total == 0:
        return 1.0
    unauth = sum(r.unauth_disputes for r in tail)
    inr    = sum(r.inr_disputes for r in tail)
    snad   = sum(r.snad_disputes for r in tail)
    other  = total - unauth - inr - snad
    return (
        (unauth * _CLAIM_RISK_MULTIPLIERS["UNAUTH"] +
         inr    * _CLAIM_RISK_MULTIPLIERS["INR"]   +
         snad   * _CLAIM_RISK_MULTIPLIERS["SNAD"]  +
         max(0, other) * _CLAIM_RISK_MULTIPLIERS["OTHER"])
        / total
    )


def _alert_level(rate: float) -> AlertLevel:
    if rate >= _PAYPAL_CRITICAL_THRESHOLD:
        return AlertLevel.CRITICAL
    if rate >= _PAYPAL_WARNING_THRESHOLD:
        return AlertLevel.WARNING
    if rate >= 0.005:   # 0.5%
        return AlertLevel.NORMAL
    return AlertLevel.HEALTHY


def _days_to_threshold(current_rate: float, trend_magnitude: float) -> int:
    """Estimate days until rate hits 1.5% threshold, given current rate + trend."""
    if current_rate >= _PAYPAL_WARNING_THRESHOLD:
        return 0
    if trend_magnitude <= 0:
        return -1  # rate is flat or falling
    # Approximate: rate grows proportionally; daily rate = current * (1 + trend_magnitude/30)^d
    # Solve for d: d = 30 * log(1.5% / current) / log(1 + trend_magnitude/30)
    import math
    if current_rate <= 0:
        return -1
    daily_growth = trend_magnitude / 30.0
    if daily_growth <= 0:
        return -1
    try:
        d = math.log(_PAYPAL_WARNING_THRESHOLD / current_rate) / math.log(1 + daily_growth)
        return max(0, int(d))
    except (ValueError, ZeroDivisionError):
        return -1


# ── Public API ────────────────────────────────────────────────────────────────

def forecast_chargeback_rate(inp: ChargebackForecastInput) -> ChargebackForecastReport:
    """Forecast next-30-day chargeback rate and return a risk report."""
    records = inp.monthly_records

    if not records:
        return ChargebackForecastReport(
            merchant_id=inp.merchant_id,
            base_rate=0.0,
            trend=RateTrend.FLAT,
            trend_magnitude=0.0,
            forecast_rate=0.0,
            confidence_low=0.0,
            confidence_high=0.0,
            alert_level=AlertLevel.HEALTHY,
            recommendation="Insufficient history to forecast. Submit at least 1 month of data.",
        )

    # Base rate: trailing 3-month average
    base_rate = _trailing_avg(records, 3)

    # Trend: compare recent 1-month avg vs 3-month avg
    recent_rate = _trailing_avg(records, 1)
    if base_rate > 0:
        relative_change = (recent_rate - base_rate) / base_rate
    else:
        relative_change = 0.0

    if relative_change > _TREND_RISE_THRESHOLD:
        trend = RateTrend.RISING
    elif relative_change < _TREND_FALL_THRESHOLD:
        trend = RateTrend.FALLING
    else:
        trend = RateTrend.FLAT

    # Forecast = base × claim_risk_multiplier × seasonal/launch adjustments
    multiplier = _claim_risk_multiplier(records)
    if inp.high_season_active:
        multiplier *= 1.20
    if inp.new_product_launch:
        multiplier *= 1.10
    if trend == RateTrend.RISING:
        multiplier *= (1 + min(0.30, relative_change))
    elif trend == RateTrend.FALLING:
        multiplier *= (1 + max(-0.20, relative_change))

    forecast_rate = round(min(1.0, base_rate * multiplier), 6)

    # Confidence band: ±30% of forecast (wider when less history)
    band_width = 0.30 if len(records) >= 3 else 0.50
    confidence_low  = round(max(0.0, forecast_rate * (1 - band_width)), 6)
    confidence_high = round(forecast_rate * (1 + band_width), 6)

    alert = _alert_level(forecast_rate)
    days = _days_to_threshold(forecast_rate, relative_change if trend == RateTrend.RISING else 0.0)

    # Contributing factors
    factors: List[str] = []
    if trend == RateTrend.RISING:
        pct = round(relative_change * 100, 1)
        factors.append(f"Dispute rate trending UP {pct}% vs 3-month average.")
    elif trend == RateTrend.FALLING:
        pct = round(abs(relative_change) * 100, 1)
        factors.append(f"Dispute rate trending DOWN {pct}% vs 3-month average.")
    else:
        factors.append("Dispute rate is stable vs 3-month average.")

    unauth_frac = _unauth_fraction(records)
    if unauth_frac > 0.30:
        factors.append(
            f"High unauthorized dispute fraction ({unauth_frac:.0%}) — indicates potential fraud or account takeover."
        )

    if inp.high_season_active:
        factors.append("High-season flag active — 20% volume uplift applied.")
    if inp.new_product_launch:
        factors.append("New product launch flag — 10% SNAD risk uplift applied.")

    # Recommendation
    if alert == AlertLevel.CRITICAL:
        rec = (
            f"CRITICAL: Forecast rate {forecast_rate:.2%} exceeds PayPal threshold ({_PAYPAL_CRITICAL_THRESHOLD:.1%}). "
            "Contact your PayPal account manager immediately. Activate fraud filters, "
            "require address verification, and review recent UNAUTH disputes for fraud patterns."
        )
    elif alert == AlertLevel.WARNING:
        rec = (
            f"WARNING: Forecast rate {forecast_rate:.2%} is approaching PayPal threshold ({_PAYPAL_WARNING_THRESHOLD:.1%}). "
            "Review UNAUTH and INR disputes for systemic issues. "
            "Consider tightening order review rules and improving shipment tracking coverage."
        )
    elif alert == AlertLevel.NORMAL:
        rec = (
            f"Rate is within normal range ({forecast_rate:.2%}). "
            "Monitor monthly and investigate any emerging UNAUTH patterns."
        )
    else:
        rec = (
            f"Healthy chargeback rate ({forecast_rate:.2%}). "
            "Maintain current practices. Review evidence quality to keep win rate high."
        )

    return ChargebackForecastReport(
        merchant_id=inp.merchant_id,
        base_rate=round(base_rate, 6),
        trend=trend,
        trend_magnitude=round(relative_change, 4),
        forecast_rate=forecast_rate,
        confidence_low=confidence_low,
        confidence_high=confidence_high,
        alert_level=alert,
        contributing_factors=factors,
        recommendation=rec,
        days_to_threshold=days,
    )
