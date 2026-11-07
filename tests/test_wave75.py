"""Wave 75 — Chargeback Threshold Alert tests."""
import pytest
from oracle.chargeback_threshold_alert import (
    CMPRiskLevel,
    MonthlyStats,
    ThresholdAlertInput,
    calculate_threshold_alert,
)


def _m(label, transactions, chargebacks):
    return MonthlyStats(month_label=label, transactions=transactions, chargebacks=chargebacks)


def _inp(monthly_stats, reason_code_counts=None, avg_amount=100.0, can_refund=True):
    return ThresholdAlertInput(
        merchant_id="MERCH_CMP",
        monthly_stats=monthly_stats,
        reason_code_counts=reason_code_counts or {"INR": 5, "UNAUTHORIZED": 2},
        avg_transaction_amount=avg_amount,
        can_preemptive_refund=can_refund,
    )


# ─── Risk level classification ────────────────────────────────────────────────

class TestRiskLevels:
    def test_safe_rate(self):
        # 0.3% → SAFE
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 3)]))
        assert r.risk_level == CMPRiskLevel.SAFE

    def test_approaching_rate(self):
        # 0.6% → APPROACHING
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 6)]))
        assert r.risk_level == CMPRiskLevel.APPROACHING

    def test_near_threshold_rate(self):
        # 0.8% → NEAR_THRESHOLD
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 8)]))
        assert r.risk_level == CMPRiskLevel.NEAR_THRESHOLD

    def test_in_breach(self):
        # 1.5% → IN_BREACH
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 15)]))
        assert r.risk_level == CMPRiskLevel.IN_BREACH

    def test_exact_threshold_is_breach(self):
        # exactly 1.0% → IN_BREACH
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 10)]))
        assert r.risk_level == CMPRiskLevel.IN_BREACH

    def test_just_below_threshold_is_near(self):
        # 0.99% → NEAR_THRESHOLD
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 9)]))
        assert r.risk_level == CMPRiskLevel.NEAR_THRESHOLD


# ─── Current rate calculation ─────────────────────────────────────────────────

class TestCurrentRate:
    def test_rate_formula(self):
        # 10 / 1000 * 100 = 1.0%
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 10)]))
        assert abs(r.current_rate_pct - 1.0) < 0.001

    def test_zero_transactions_safe(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 0, 0)]))
        assert r.risk_level == CMPRiskLevel.SAFE
        assert r.current_rate_pct == 0.0

    def test_rate_range(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 500, 3)]))
        assert 0.0 <= r.current_rate_pct <= 100.0


# ─── Trend direction ──────────────────────────────────────────────────────────

class TestTrend:
    def test_rising_trend(self):
        stats = [
            _m("2026-08", 1000, 3),   # 0.3%
            _m("2026-09", 1000, 5),   # 0.5%
            _m("2026-10", 1000, 8),   # 0.8%
        ]
        r = calculate_threshold_alert(_inp(stats))
        assert r.trend_direction == "RISING"

    def test_falling_trend(self):
        stats = [
            _m("2026-08", 1000, 8),   # 0.8%
            _m("2026-09", 1000, 5),   # 0.5%
            _m("2026-10", 1000, 2),   # 0.2%
        ]
        r = calculate_threshold_alert(_inp(stats))
        assert r.trend_direction == "FALLING"

    def test_stable_trend_single_month(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 5)]))
        assert r.trend_direction == "STABLE"

    def test_stable_trend_no_delta(self):
        stats = [
            _m("2026-09", 1000, 5),
            _m("2026-10", 1000, 5),
        ]
        r = calculate_threshold_alert(_inp(stats))
        assert r.trend_direction == "STABLE"


# ─── Days-to-breach projection ────────────────────────────────────────────────

class TestDaysToBreach:
    def test_rising_rate_projects_breach(self):
        stats = [
            _m("2026-09", 1000, 5),   # 0.5%
            _m("2026-10", 1000, 7),   # 0.7%  delta=0.2%/mo → 1.5 months → 45d
        ]
        r = calculate_threshold_alert(_inp(stats))
        assert r.projected_days_to_breach is not None
        assert r.projected_days_to_breach > 0

    def test_falling_rate_no_projection(self):
        stats = [
            _m("2026-09", 1000, 8),
            _m("2026-10", 1000, 5),
        ]
        r = calculate_threshold_alert(_inp(stats))
        assert r.projected_days_to_breach is None

    def test_already_in_breach_zero_days(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 15)]))
        # IN_BREACH: days_to_breach may be 0 or not projected via _days_to_breach
        # Either None (single month) or 0
        assert r.projected_days_to_breach in (0, None)

    def test_stable_rate_no_projection(self):
        stats = [
            _m("2026-09", 1000, 5),
            _m("2026-10", 1000, 5),
        ]
        r = calculate_threshold_alert(_inp(stats))
        assert r.projected_days_to_breach is None


# ─── Preemptive refund recommendations ───────────────────────────────────────

class TestPreemptiveRefunds:
    def test_breach_case_recommends_refunds(self):
        # 1.5% rate — 15 CBs on 1000 txns; target = 7 (0.75%) → excess = 8
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 15)]))
        assert r.preemptive_refunds_recommended > 0

    def test_safe_case_no_refunds(self):
        # 0.3% rate → 3 CBs, target = 7 → no excess
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 3)]))
        assert r.preemptive_refunds_recommended == 0

    def test_cannot_refund_gives_zero(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 15)], can_refund=False))
        assert r.preemptive_refunds_recommended == 0

    def test_chargebacks_to_add_before_breach(self):
        # 0.3% → 3 CBs on 1000; threshold = 10; room = 7
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 3)]))
        assert r.chargebacks_to_add_before_breach == 7


# ─── Reason code breakdown ────────────────────────────────────────────────────

class TestReasonCodes:
    def test_top_reason_codes_returned(self):
        rc = {"INR": 10, "UNAUTHORIZED": 5, "SNAD": 2}
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 17)], reason_code_counts=rc))
        assert len(r.top_reason_codes) > 0

    def test_reason_codes_sorted_by_count_descending(self):
        rc = {"INR": 10, "UNAUTHORIZED": 5, "SNAD": 2}
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 17)], reason_code_counts=rc))
        counts = [rc.count for rc in r.top_reason_codes]
        assert counts == sorted(counts, reverse=True)

    def test_top_4_reason_codes_max(self):
        rc = {"INR": 10, "UNAUTHORIZED": 5, "SNAD": 2, "SUBSCRIPTION": 3, "OTHER": 1}
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 21)], reason_code_counts=rc))
        assert len(r.top_reason_codes) <= 4

    def test_reason_code_pct_sums_to_approx_100(self):
        rc = {"INR": 10, "UNAUTHORIZED": 5, "SNAD": 2, "SUBSCRIPTION": 3}
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 20)], reason_code_counts=rc))
        total_pct = sum(x.pct_of_chargebacks for x in r.top_reason_codes)
        assert abs(total_pct - 100.0) < 0.5


# ─── PayPal overhead savings ──────────────────────────────────────────────────

class TestPayPalOverhead:
    def test_no_savings_when_safe(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 3)]))
        assert r.paypal_overhead_savings_usd == 0.0

    def test_savings_when_in_breach(self):
        # 15 CBs on 1000; threshold = 10; breach_cases = 5; savings = 5*30 = 150
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 15)]))
        assert r.paypal_overhead_savings_usd == pytest.approx(150.0, abs=0.01)

    def test_savings_non_negative(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 5)]))
        assert r.paypal_overhead_savings_usd >= 0.0


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_merchant_id_echoed(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 5)]))
        assert r.merchant_id == "MERCH_CMP"

    def test_recommendation_non_empty(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 15)]))
        assert len(r.recommendation) > 20

    def test_paypal_signal_contains_merchant_id(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 5)]))
        assert "MERCH_CMP" in r.paypal_signal

    def test_paypal_signal_contains_risk_level(self):
        r = calculate_threshold_alert(_inp([_m("2026-10", 1000, 15)]))
        assert "IN_BREACH" in r.paypal_signal

    def test_no_data_returns_safe(self):
        r = calculate_threshold_alert(_inp([]))
        assert r.risk_level == CMPRiskLevel.SAFE
        assert r.current_rate_pct == 0.0
