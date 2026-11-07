"""
Chargeback Threshold Alert — predict breach of PayPal's 1% chargeback monitoring threshold.

PayPal's Chargeback Monitoring Program (CMP) triggers enhanced review or
account restrictions when a merchant's chargeback rate exceeds 1.0% of
monthly transactions. Merchants who don't track this metric are often caught
off guard when they hit the threshold.

This oracle:
  1. Computes the merchant's current chargeback rate and rolling 90-day trend
  2. Predicts how many days until they breach the 1% threshold at current trend
  3. Identifies the reason code(s) driving the rate
  4. Recommends a specific dispute volume to preemptively refund to stay under threshold
  5. Issues a risk level: SAFE / APPROACHING / NEAR_THRESHOLD / IN_BREACH

Formula:
  chargeback_rate = total_chargebacks_last_30d / total_transactions_last_30d × 100

Trend projection:
  Uses a linear extrapolation of the monthly rate over the last 3 months.
  If the trend is upward and crosses 1.0%, it computes the projected breach date.

PayPal benefit:
  - Merchants who stay under the 1% threshold require far less manual review
  - Proactive refund recommendations prevent new chargebacks from being filed
  - Trend data lets PayPal's risk team identify at-risk merchants before they breach
  - Merchants in breach cost PayPal ~$30/case in dispute resolution overhead
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

_CMP_THRESHOLD_PCT: float = 1.0     # PayPal 1% chargeback monitoring threshold
_EARLY_WARNING_PCT: float = 0.75    # trigger early warning at 0.75%
_APPROACHING_PCT: float = 0.50      # approaching at 0.5%
_OVERHEAD_PER_BREACH_CASE: float = 30.0  # PayPal review cost per breach-triggered case


class CMPRiskLevel(str, Enum):
    SAFE           = "SAFE"           # < 0.50%
    APPROACHING    = "APPROACHING"    # 0.50 – 0.74%
    NEAR_THRESHOLD = "NEAR_THRESHOLD" # 0.75 – 0.99%
    IN_BREACH      = "IN_BREACH"      # ≥ 1.0%


@dataclass
class MonthlyStats:
    """One month of transaction + chargeback data."""
    month_label: str    # e.g. "2026-10"
    transactions: int
    chargebacks: int


@dataclass
class ReasonCodeBreakdown:
    reason_code: str
    count: int
    pct_of_chargebacks: float


@dataclass
class ThresholdAlertInput:
    merchant_id: str
    monthly_stats: List[MonthlyStats]           # 1–3 months, most recent last
    reason_code_counts: dict                    # {"INR": 5, "UNAUTHORIZED": 3, ...}
    avg_transaction_amount: float = 100.0
    can_preemptive_refund: bool = True          # merchant is willing to preemptively refund


@dataclass
class ThresholdAlertReport:
    merchant_id: str
    current_rate_pct: float
    risk_level: CMPRiskLevel
    trend_direction: str                        # RISING / STABLE / FALLING
    projected_days_to_breach: Optional[int]     # None if not breaching
    chargebacks_to_add_before_breach: int       # 0 if already in breach
    preemptive_refunds_recommended: int         # disputes to refund to reduce rate
    top_reason_codes: List[ReasonCodeBreakdown]
    paypal_overhead_savings_usd: float          # $ PayPal saves if merchant stays under
    recommendation: str
    paypal_signal: str


def _compute_rate(stats: MonthlyStats) -> float:
    if stats.transactions <= 0:
        return 0.0
    return (stats.chargebacks / stats.transactions) * 100.0


def _trend(rates: List[float]) -> str:
    if len(rates) < 2:
        return "STABLE"
    delta = rates[-1] - rates[0]
    if delta > 0.05:
        return "RISING"
    if delta < -0.05:
        return "FALLING"
    return "STABLE"


def _days_to_breach(rates: List[float], current_month_transactions: int) -> Optional[int]:
    """
    Rough linear projection: given the monthly rate trend, how many days until 1%?
    Returns None if not rising or already beyond.
    """
    if len(rates) < 2:
        return None
    latest = rates[-1]
    if latest >= _CMP_THRESHOLD_PCT:
        return 0
    monthly_delta = rates[-1] - rates[-2]
    if monthly_delta <= 0:
        return None  # stable or falling, won't breach
    # At current monthly delta, months to reach 1%
    months_needed = (_CMP_THRESHOLD_PCT - latest) / monthly_delta
    days = round(months_needed * 30)
    return max(1, days)


def calculate_threshold_alert(inp: ThresholdAlertInput) -> ThresholdAlertReport:
    if not inp.monthly_stats:
        # No data — can't assess
        return ThresholdAlertReport(
            merchant_id=inp.merchant_id,
            current_rate_pct=0.0,
            risk_level=CMPRiskLevel.SAFE,
            trend_direction="STABLE",
            projected_days_to_breach=None,
            chargebacks_to_add_before_breach=0,
            preemptive_refunds_recommended=0,
            top_reason_codes=[],
            paypal_overhead_savings_usd=0.0,
            recommendation="No data available. Begin tracking monthly transaction and chargeback counts.",
            paypal_signal=f"Merchant {inp.merchant_id}: no data.",
        )

    latest = inp.monthly_stats[-1]
    current_rate = _compute_rate(latest)
    rates = [_compute_rate(m) for m in inp.monthly_stats]

    trend = _trend(rates)
    days_to_breach = _days_to_breach(rates, latest.transactions)

    # Risk level
    if current_rate >= _CMP_THRESHOLD_PCT:
        risk = CMPRiskLevel.IN_BREACH
    elif current_rate >= _EARLY_WARNING_PCT:
        risk = CMPRiskLevel.NEAR_THRESHOLD
    elif current_rate >= _APPROACHING_PCT:
        risk = CMPRiskLevel.APPROACHING
    else:
        risk = CMPRiskLevel.SAFE

    # How many more chargebacks before breach?
    # 1% of transactions = threshold_chargebacks
    threshold_cb = max(0, int(latest.transactions * (_CMP_THRESHOLD_PCT / 100.0)) - latest.chargebacks)

    # Preemptive refunds to drop back to 0.75% (early warning):
    # target_cb = transactions * 0.75%
    target_cb = int(latest.transactions * (_EARLY_WARNING_PCT / 100.0))
    excess_cb = max(0, latest.chargebacks - target_cb)
    # Each preemptive refund before dispute = 1 less chargeback
    preemptive_refunds = excess_cb if inp.can_preemptive_refund else 0

    # Reason code breakdown
    total_rc = sum(inp.reason_code_counts.values()) or 1
    top_codes = sorted(
        [
            ReasonCodeBreakdown(
                reason_code=rc,
                count=cnt,
                pct_of_chargebacks=round(cnt / total_rc * 100, 1),
            )
            for rc, cnt in inp.reason_code_counts.items()
        ],
        key=lambda x: x.count,
        reverse=True,
    )[:4]

    # PayPal overhead savings if merchant stays under threshold
    breach_cases = max(0, latest.chargebacks - int(latest.transactions * (_CMP_THRESHOLD_PCT / 100.0)))
    overhead_savings = breach_cases * _OVERHEAD_PER_BREACH_CASE

    # Recommendation
    if risk == CMPRiskLevel.IN_BREACH:
        rec = (
            f"You are IN BREACH of PayPal's 1% chargeback threshold "
            f"({current_rate:.2f}% rate). Immediate action required: "
            f"preemptively refund the top {preemptive_refunds} pending disputes "
            f"and reduce {top_codes[0].reason_code if top_codes else 'INR'} disputes urgently."
        )
    elif risk == CMPRiskLevel.NEAR_THRESHOLD:
        rec = (
            f"Near threshold ({current_rate:.2f}%). "
            + (f"At current trend, breach projected in ~{days_to_breach} days. " if days_to_breach else "")
            + f"Recommend preemptive refunds on {preemptive_refunds} disputes to drop below 0.75%."
        )
    elif risk == CMPRiskLevel.APPROACHING:
        rec = (
            f"Approaching threshold ({current_rate:.2f}%). "
            + ("Rate is rising — monitor closely. " if trend == "RISING" else "")
            + "Review top reason codes and tighten fraud controls."
        )
    else:
        rec = f"Safe ({current_rate:.2f}%). " + (
            "Rate is trending upward — continue monitoring." if trend == "RISING"
            else "Rate is stable or falling. No action required."
        )

    paypal_signal = (
        f"Merchant {inp.merchant_id}: chargeback_rate={current_rate:.2f}%, "
        f"risk={risk.value}, trend={trend}, "
        + (f"breach_in={days_to_breach}d, " if days_to_breach else "")
        + f"overhead_savings=${overhead_savings:.0f}."
    )

    return ThresholdAlertReport(
        merchant_id=inp.merchant_id,
        current_rate_pct=round(current_rate, 4),
        risk_level=risk,
        trend_direction=trend,
        projected_days_to_breach=days_to_breach,
        chargebacks_to_add_before_breach=threshold_cb,
        preemptive_refunds_recommended=preemptive_refunds,
        top_reason_codes=top_codes,
        paypal_overhead_savings_usd=round(overhead_savings, 2),
        recommendation=rec,
        paypal_signal=paypal_signal,
    )
