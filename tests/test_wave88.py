"""Wave 88 — Chargeback Rate Forecaster tests."""
import pytest
from oracle.chargeback_rate_forecaster import (
    RateTrend, AlertLevel,
    MonthlyRecord, ChargebackForecastInput, ChargebackForecastReport,
    forecast_chargeback_rate,
    _dispute_rate, _trailing_avg, _unauth_fraction, _claim_risk_multiplier,
    _alert_level,
)


def _rec(month="2025-01", transactions=1000, disputes=10,
         unauth=0, inr=5, snad=3):
    return MonthlyRecord(
        month_label=month,
        total_transactions=transactions,
        total_disputes=disputes,
        unauth_disputes=unauth,
        inr_disputes=inr,
        snad_disputes=snad,
    )


def _inp(records, high_season=False, launch=False):
    return ChargebackForecastInput(
        merchant_id="M-001",
        monthly_records=records,
        high_season_active=high_season,
        new_product_launch=launch,
    )


# ─── Helper: dispute rate ──────────────────────────────────────────────────────

class TestDisputeRate:
    def test_zero_transactions_returns_zero(self):
        assert _dispute_rate(_rec(transactions=0, disputes=0)) == 0.0

    def test_basic_rate(self):
        assert abs(_dispute_rate(_rec(transactions=1000, disputes=10)) - 0.01) < 1e-9

    def test_zero_disputes_zero_rate(self):
        assert _dispute_rate(_rec(disputes=0)) == 0.0


# ─── Helper: trailing average ──────────────────────────────────────────────────

class TestTrailingAvg:
    def test_single_record(self):
        r = _rec(transactions=1000, disputes=10)
        assert abs(_trailing_avg([r], 3) - 0.01) < 1e-9

    def test_averages_last_n(self):
        records = [
            _rec("2025-01", 1000, 10),   # rate 0.01
            _rec("2025-02", 1000, 20),   # rate 0.02
            _rec("2025-03", 1000, 30),   # rate 0.03
        ]
        avg = _trailing_avg(records, 3)
        assert abs(avg - 0.02) < 1e-9

    def test_n_larger_than_records(self):
        records = [_rec(transactions=1000, disputes=10)]
        avg = _trailing_avg(records, 6)
        assert abs(avg - 0.01) < 1e-9


# ─── Trend detection ─────────────────────────────────────────────────────────

class TestTrendDetection:
    def _forecast(self, records, **kw):
        return forecast_chargeback_rate(_inp(records, **kw))

    def test_stable_history_flat_trend(self):
        records = [_rec(f"2025-{i:02d}", 1000, 10) for i in range(1, 4)]
        r = self._forecast(records)
        assert r.trend == RateTrend.FLAT

    def test_rising_rate_detected(self):
        records = [
            _rec("2025-01", 1000, 10),   # 1.0%
            _rec("2025-02", 1000, 10),   # 1.0%
            _rec("2025-03", 1000, 20),   # 2.0% — big jump
        ]
        r = self._forecast(records)
        assert r.trend == RateTrend.RISING

    def test_falling_rate_detected(self):
        records = [
            _rec("2025-01", 1000, 30),   # 3.0%
            _rec("2025-02", 1000, 30),   # 3.0%
            _rec("2025-03", 1000, 10),   # 1.0% — big drop
        ]
        r = self._forecast(records)
        assert r.trend == RateTrend.FALLING


# ─── Alert levels ─────────────────────────────────────────────────────────────

class TestAlertLevel:
    def test_zero_rate_healthy(self):
        assert _alert_level(0.0) == AlertLevel.HEALTHY

    def test_half_pct_healthy(self):
        assert _alert_level(0.004) == AlertLevel.HEALTHY

    def test_one_pct_normal(self):
        assert _alert_level(0.008) == AlertLevel.NORMAL

    def test_above_1_5_pct_warning(self):
        assert _alert_level(0.015) == AlertLevel.WARNING

    def test_above_2_pct_critical(self):
        assert _alert_level(0.021) == AlertLevel.CRITICAL


# ─── Claim risk multiplier ────────────────────────────────────────────────────

class TestClaimRiskMultiplier:
    def test_all_unauth_max_multiplier(self):
        records = [_rec(disputes=10, unauth=10, inr=0, snad=0)]
        m = _claim_risk_multiplier(records)
        assert abs(m - 1.30) < 1e-9

    def test_no_disputes_returns_one(self):
        records = [_rec(disputes=0, unauth=0, inr=0, snad=0)]
        m = _claim_risk_multiplier(records)
        assert m == 1.0

    def test_mixed_mix_between_one_and_1_3(self):
        records = [_rec(disputes=10, unauth=5, inr=5, snad=0)]
        m = _claim_risk_multiplier(records)
        assert 1.0 < m < 1.30


# ─── Forecast: seasonal / launch flags ───────────────────────────────────────

class TestForecastFlags:
    def _base_records(self):
        return [_rec(f"2025-{i:02d}", 1000, 10) for i in range(1, 4)]

    def test_high_season_increases_forecast(self):
        base = forecast_chargeback_rate(_inp(self._base_records()))
        seasonal = forecast_chargeback_rate(_inp(self._base_records(), high_season=True))
        assert seasonal.forecast_rate > base.forecast_rate

    def test_launch_increases_forecast(self):
        base = forecast_chargeback_rate(_inp(self._base_records()))
        launch = forecast_chargeback_rate(_inp(self._base_records(), launch=True))
        assert launch.forecast_rate > base.forecast_rate


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_empty_records_returns_report(self):
        r = forecast_chargeback_rate(_inp([]))
        assert isinstance(r, ChargebackForecastReport)
        assert r.forecast_rate == 0.0

    def test_forecast_rate_non_negative(self):
        records = [_rec(f"2025-{i:02d}") for i in range(1, 4)]
        r = forecast_chargeback_rate(_inp(records))
        assert r.forecast_rate >= 0.0

    def test_confidence_band_ordered(self):
        records = [_rec(f"2025-{i:02d}") for i in range(1, 4)]
        r = forecast_chargeback_rate(_inp(records))
        assert r.confidence_low <= r.forecast_rate <= r.confidence_high

    def test_recommendation_non_empty(self):
        records = [_rec(f"2025-{i:02d}") for i in range(1, 4)]
        r = forecast_chargeback_rate(_inp(records))
        assert len(r.recommendation) > 10

    def test_critical_alert_recommendation_mentions_paypal(self):
        # 3% rate consistently — above critical threshold
        records = [_rec(f"2025-{i:02d}", 1000, 30) for i in range(1, 4)]
        r = forecast_chargeback_rate(_inp(records))
        assert r.alert_level == AlertLevel.CRITICAL
        assert "paypal" in r.recommendation.lower() or "critical" in r.recommendation.lower()

    def test_contributing_factors_non_empty(self):
        records = [_rec(f"2025-{i:02d}") for i in range(1, 4)]
        r = forecast_chargeback_rate(_inp(records))
        assert len(r.contributing_factors) > 0

    def test_rising_trend_in_factors(self):
        records = [
            _rec("2025-01", 1000, 10),
            _rec("2025-02", 1000, 10),
            _rec("2025-03", 1000, 25),
        ]
        r = forecast_chargeback_rate(_inp(records))
        assert any("up" in f.lower() or "rising" in f.lower() or "trending" in f.lower()
                   for f in r.contributing_factors)

    def test_base_rate_matches_trailing_3mo_avg(self):
        records = [
            _rec("2025-01", 1000, 10),   # 1.0%
            _rec("2025-02", 1000, 20),   # 2.0%
            _rec("2025-03", 1000, 30),   # 3.0%
        ]
        r = forecast_chargeback_rate(_inp(records))
        assert abs(r.base_rate - 0.02) < 1e-5
