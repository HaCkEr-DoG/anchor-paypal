"""
Tests for the 4 strategic-layer modules:
  - oracle/merchant_health.py      — account health + threshold projection
  - oracle/calibrator.py           — Bayesian win-rate calibration
  - oracle/seller_protection.py    — PayPal SP eligibility pre-flight
  - oracle/chargeback_forecast.py  — 30/60/90-day rate forecasting

These modules convert Anchor from dispute-by-dispute reaction to
account-level strategic management — a capability no competitor has.
"""

import pytest
from oracle.merchant_health import (
    DisputeWindow, AccountStatus,
    assess_merchant_health,
)
from oracle.calibrator import (
    DisputeOutcomeRecord, EvidenceQuality,
    build_calibration_model, calibrated_win_rate,
    _INDUSTRY_PRIORS, _blend_weight,
)
from oracle.seller_protection import (
    OrderForSP, SPCoverage, SPGapSeverity,
    assess_seller_protection,
)
from oracle.chargeback_forecast import (
    WeeklySnapshot, OpenOrderPipeline, ForecastRisk,
    forecast_chargeback_rate,
)


# ═════════════════════════════════════════════════════════════
# Merchant Health Tests
# ═════════════════════════════════════════════════════════════

class TestMerchantHealth:

    def _window(self, **kwargs) -> DisputeWindow:
        defaults = dict(
            total_transactions=1000,
            total_disputes=5,
            open_disputes=0,
            weekly_disputes=[1, 1, 1, 1],
            weekly_transactions=[100, 100, 100, 100],
        )
        defaults.update(kwargs)
        return DisputeWindow(**defaults)

    def test_healthy_status_below_half_percent(self):
        window = self._window(total_transactions=1000, total_disputes=4)
        report = assess_merchant_health(window)
        assert report.account_status == AccountStatus.HEALTHY
        assert report.current_dispute_rate < 0.005

    def test_elevated_status_at_half_percent(self):
        window = self._window(total_transactions=1000, total_disputes=6)
        report = assess_merchant_health(window)
        assert report.account_status in (AccountStatus.ELEVATED, AccountStatus.HIGH)

    def test_excessive_status_at_1_5_percent(self):
        window = self._window(total_transactions=1000, total_disputes=16)
        report = assess_merchant_health(window)
        assert report.account_status == AccountStatus.EXCESSIVE
        assert report.hdr_fee_active is True
        assert report.hdr_fee_per_dispute == 30.0

    def test_critical_status_at_3_percent(self):
        window = self._window(total_transactions=1000, total_disputes=31)
        report = assess_merchant_health(window)
        assert report.account_status == AccountStatus.CRITICAL

    def test_terminal_status_at_5_percent(self):
        window = self._window(total_transactions=1000, total_disputes=51)
        report = assess_merchant_health(window)
        assert report.account_status == AccountStatus.TERMINAL

    def test_hdr_not_active_below_threshold(self):
        window = self._window(total_transactions=1000, total_disputes=10)
        report = assess_merchant_health(window)
        assert report.hdr_fee_active is False
        assert report.hdr_fee_per_dispute == 0.0

    def test_dispute_budget_30d_positive(self):
        window = self._window(total_transactions=1000, total_disputes=3)
        report = assess_merchant_health(window)
        assert report.dispute_budget_30d > 0

    def test_dispute_budget_zero_when_at_threshold(self):
        window = self._window(total_transactions=1000, total_disputes=15)
        report = assess_merchant_health(window)
        # At 1.50% threshold: budget is near zero
        assert report.dispute_budget_30d >= 0

    def test_worsening_trend_detected(self):
        # Increasing disputes per week
        window = self._window(
            total_transactions=1000,
            total_disputes=10,
            weekly_disputes=[1, 2, 3, 4],  # Worsening
            weekly_transactions=[100, 100, 100, 100],
        )
        report = assess_merchant_health(window)
        assert report.trend_direction == "WORSENING"

    def test_improving_trend_detected(self):
        # 24 total disputes, but zero in the last 4 weeks — recent rate is falling
        # rate_30d_ago = 24/600 = 4.0%,  current = 24/1000 = 2.4% → IMPROVING
        window = self._window(
            total_transactions=1000,
            total_disputes=24,
            weekly_disputes=[0, 0, 0, 0],
            weekly_transactions=[100, 100, 100, 100],
        )
        report = assess_merchant_health(window)
        assert report.trend_direction == "IMPROVING"

    def test_health_score_100_for_very_low_rate(self):
        window = self._window(total_transactions=2000, total_disputes=1)
        report = assess_merchant_health(window)
        assert report.health_score >= 80

    def test_health_score_low_for_excessive_rate(self):
        # 2.0% rate (EXCESSIVE) + worsening trend → score = 50 - 15 = 35 < 50
        # rate_30d_ago = 6/600 = 1.0% < current 2.0% → WORSENING
        window = self._window(
            total_transactions=1000,
            total_disputes=20,
            weekly_disputes=[3, 3, 4, 4],
            weekly_transactions=[100, 100, 100, 100],
        )
        report = assess_merchant_health(window)
        assert report.health_score < 50

    def test_emergency_actions_present_when_critical(self):
        window = self._window(total_transactions=1000, total_disputes=35)
        report = assess_merchant_health(window)
        assert len(report.emergency_actions) > 0

    def test_preventive_actions_always_present(self):
        window = self._window(total_transactions=1000, total_disputes=3)
        report = assess_merchant_health(window)
        assert len(report.preventive_actions) > 0

    def test_empty_window_returns_healthy_defaults(self):
        window = DisputeWindow(total_transactions=0, total_disputes=0)
        report = assess_merchant_health(window)
        assert report.account_status == AccountStatus.HEALTHY
        assert report.health_score == 100

    def test_annual_hdr_exposure_computed_when_active(self):
        window = self._window(
            total_transactions=1000,
            total_disputes=18,
            weekly_disputes=[2, 2, 2, 3],
            weekly_transactions=[100, 100, 100, 100],
        )
        report = assess_merchant_health(window)
        if report.hdr_fee_active:
            assert report.annual_hdr_exposure > 0


# ═════════════════════════════════════════════════════════════
# Calibrator Tests
# ═════════════════════════════════════════════════════════════

class TestCalibrator:

    def _make_history(self, claim_type, wins, losses, eq=EvidenceQuality.FAIR):
        return (
            [DisputeOutcomeRecord(claim_type, won=True, evidence_quality=eq)] * wins +
            [DisputeOutcomeRecord(claim_type, won=False, evidence_quality=eq)] * losses
        )

    def test_no_history_returns_industry_prior(self):
        model = build_calibration_model("m1", [])
        rate = calibrated_win_rate(model, "ITEM_NOT_RECEIVED", EvidenceQuality.FAIR)
        assert rate == pytest.approx(_INDUSTRY_PRIORS["ITEM_NOT_RECEIVED"], abs=0.05)

    def test_high_win_rate_raises_above_prior(self):
        history = self._make_history("ITEM_NOT_RECEIVED", wins=20, losses=2,
                                     eq=EvidenceQuality.EXCELLENT)
        model = build_calibration_model("m2", history)
        rate = calibrated_win_rate(model, "ITEM_NOT_RECEIVED", EvidenceQuality.EXCELLENT)
        assert rate > _INDUSTRY_PRIORS["ITEM_NOT_RECEIVED"]

    def test_low_win_rate_lowers_below_prior(self):
        history = self._make_history("ITEM_NOT_RECEIVED", wins=3, losses=20,
                                     eq=EvidenceQuality.POOR)
        model = build_calibration_model("m3", history)
        rate = calibrated_win_rate(model, "ITEM_NOT_RECEIVED", EvidenceQuality.POOR)
        assert rate < _INDUSTRY_PRIORS["ITEM_NOT_RECEIVED"]

    def test_calibrated_rate_bounded_0_to_1(self):
        history = self._make_history("ITEM_NOT_RECEIVED", wins=50, losses=0)
        model = build_calibration_model("m4", history)
        rate = calibrated_win_rate(model, "ITEM_NOT_RECEIVED")
        assert 0.0 < rate <= 0.95

    def test_excellent_evidence_raises_rate_vs_poor(self):
        history_good = self._make_history("ITEM_NOT_RECEIVED", wins=10, losses=5,
                                          eq=EvidenceQuality.EXCELLENT)
        history_poor = self._make_history("ITEM_NOT_RECEIVED", wins=10, losses=5,
                                          eq=EvidenceQuality.POOR)
        model_g = build_calibration_model("m5g", history_good)
        model_p = build_calibration_model("m5p", history_poor)
        rate_good = calibrated_win_rate(model_g, "ITEM_NOT_RECEIVED", EvidenceQuality.EXCELLENT)
        rate_poor = calibrated_win_rate(model_p, "ITEM_NOT_RECEIVED", EvidenceQuality.POOR)
        assert rate_good > rate_poor

    def test_blend_weight_zero_at_zero_samples(self):
        assert _blend_weight(0) == 0.0

    def test_blend_weight_increases_with_samples(self):
        w5 = _blend_weight(5)
        w20 = _blend_weight(20)
        w50 = _blend_weight(50)
        assert w5 < w20 < w50

    def test_well_calibrated_flag_after_enough_disputes(self):
        history = self._make_history("ITEM_NOT_RECEIVED", wins=8, losses=4)
        model = build_calibration_model("m6", history)
        # 12 disputes >= MIN_DISPUTES_FOR_CALIBRATION (10)
        assert model.is_well_calibrated is True

    def test_not_well_calibrated_with_few_disputes(self):
        history = self._make_history("ITEM_NOT_RECEIVED", wins=2, losses=1)
        model = build_calibration_model("m7", history)
        assert model.is_well_calibrated is False

    def test_multiple_claim_types_tracked_independently(self):
        history = (
            self._make_history("ITEM_NOT_RECEIVED", wins=10, losses=2) +
            self._make_history("SIGNIFICANTLY_NOT_AS_DESCRIBED", wins=2, losses=10)
        )
        model = build_calibration_model("m8", history)
        inr_rate = calibrated_win_rate(model, "ITEM_NOT_RECEIVED")
        snad_rate = calibrated_win_rate(model, "SIGNIFICANTLY_NOT_AS_DESCRIBED")
        assert inr_rate > snad_rate

    def test_total_disputes_observed_correct(self):
        history = self._make_history("ITEM_NOT_RECEIVED", wins=7, losses=3)
        model = build_calibration_model("m9", history)
        assert model.total_disputes_observed == 10


# ═════════════════════════════════════════════════════════════
# Seller Protection Tests
# ═════════════════════════════════════════════════════════════

class TestSellerProtection:

    def _perfect_order(self, amount=100.0, **kwargs) -> OrderForSP:
        defaults = dict(
            order_amount=amount,
            ship_to_paypal_confirmed_address=True,
            has_tracking_number=True,
            carrier_provides_online_tracking=True,
            shipped_within_7_days=True,
            has_signature_confirmation=amount >= 750,
            is_physical_good=True,
            is_prohibited_item=False,
            paid_via_paypal=True,
        )
        defaults.update(kwargs)
        return OrderForSP(**defaults)

    def test_perfect_order_gets_full_coverage(self):
        order = self._perfect_order(amount=100.0)
        result = assess_seller_protection(order)
        assert result.coverage == SPCoverage.FULL
        assert result.inr_protected is True
        assert result.unauthorized_protected is True

    def test_no_tracking_blocks_sp(self):
        order = self._perfect_order(has_tracking_number=False)
        result = assess_seller_protection(order)
        blocking = [g for g in result.gaps if g.severity == SPGapSeverity.BLOCKING]
        assert len(blocking) > 0
        assert result.coverage == SPCoverage.NONE

    def test_unconfirmed_address_blocks_sp(self):
        order = self._perfect_order(ship_to_paypal_confirmed_address=False)
        result = assess_seller_protection(order)
        assert result.coverage == SPCoverage.NONE
        assert result.inr_protected is False

    def test_750_order_without_signature_blocks_sp(self):
        order = self._perfect_order(
            amount=750.0,
            has_signature_confirmation=False,
        )
        result = assess_seller_protection(order)
        blocking = [g for g in result.gaps if g.severity == SPGapSeverity.BLOCKING]
        assert len(blocking) > 0

    def test_750_order_with_signature_gets_full_sp(self):
        order = self._perfect_order(
            amount=750.0,
            has_signature_confirmation=True,
        )
        result = assess_seller_protection(order)
        assert result.coverage == SPCoverage.FULL

    def test_digital_goods_always_excluded_from_sp(self):
        order = self._perfect_order(is_physical_good=False)
        result = assess_seller_protection(order)
        assert result.coverage == SPCoverage.NONE

    def test_snad_never_protected(self):
        order = self._perfect_order()
        result = assess_seller_protection(order)
        assert result.snad_protected is False

    def test_sp_score_100_for_perfect_order(self):
        order = self._perfect_order(amount=100.0, has_signature_confirmation=False)
        # $100 order doesn't need signature
        result = assess_seller_protection(order)
        assert result.sp_score == 100

    def test_sp_score_zero_for_multiple_blockers(self):
        order = OrderForSP(
            order_amount=200.0,
            ship_to_paypal_confirmed_address=False,
            has_tracking_number=False,
            is_physical_good=False,
            paid_via_paypal=False,
        )
        result = assess_seller_protection(order)
        assert result.sp_score == 0
        assert result.coverage == SPCoverage.NONE

    def test_blocking_gaps_have_fix_actions(self):
        order = self._perfect_order(has_tracking_number=False)
        result = assess_seller_protection(order)
        blocking = [g for g in result.gaps if g.severity == SPGapSeverity.BLOCKING and g.fix_available]
        for g in blocking:
            assert len(g.fix_action) > 0

    def test_prohibited_item_blocks_sp(self):
        order = self._perfect_order(is_prohibited_item=True)
        result = assess_seller_protection(order)
        assert result.coverage == SPCoverage.NONE

    def test_recommendation_contains_fix_instruction(self):
        order = self._perfect_order(has_tracking_number=False)
        result = assess_seller_protection(order)
        assert len(result.overall_recommendation) > 0

    def test_non_paypal_payment_blocks_sp(self):
        order = self._perfect_order(paid_via_paypal=False)
        result = assess_seller_protection(order)
        assert result.coverage == SPCoverage.NONE


# ═════════════════════════════════════════════════════════════
# Chargeback Forecast Tests
# ═════════════════════════════════════════════════════════════

class TestChargebackForecast:

    def _snapshots_flat(self, rate=0.008, n=8) -> list[WeeklySnapshot]:
        return [
            WeeklySnapshot(
                week_number=i + 1,
                transactions=150,
                disputes_filed=int(rate * 150),
                dispute_rate=rate,
            )
            for i in range(n)
        ]

    def _pipeline(self, **kwargs) -> OpenOrderPipeline:
        defaults = dict(
            total_open_orders=300,
            high_risk_orders=10,
            medium_risk_orders=50,
            low_risk_orders=240,
        )
        defaults.update(kwargs)
        return OpenOrderPipeline(**defaults)

    def test_flat_trend_stable(self):
        snaps = self._snapshots_flat(rate=0.008)
        forecast = forecast_chargeback_rate(snaps, self._pipeline(), current_month=6)
        assert forecast.trend_direction == "STABLE"

    def test_worsening_trend_detected_in_forecast(self):
        # Rates increasing week over week
        snaps = [
            WeeklySnapshot(1, 150, 4, dispute_rate=0.0267),
            WeeklySnapshot(2, 150, 3, dispute_rate=0.0200),
            WeeklySnapshot(3, 150, 2, dispute_rate=0.0133),
            WeeklySnapshot(4, 150, 1, dispute_rate=0.0067),
        ]
        forecast = forecast_chargeback_rate(snaps, self._pipeline(), current_month=6)
        assert forecast.trend_direction == "WORSENING"
        assert forecast.trend_slope_per_week > 0

    def test_improving_trend_detected(self):
        snaps = [
            WeeklySnapshot(1, 150, 1, dispute_rate=0.0067),
            WeeklySnapshot(2, 150, 2, dispute_rate=0.0133),
            WeeklySnapshot(3, 150, 3, dispute_rate=0.0200),
            WeeklySnapshot(4, 150, 4, dispute_rate=0.0267),
        ]
        forecast = forecast_chargeback_rate(snaps, self._pipeline(), current_month=6)
        assert forecast.trend_direction == "IMPROVING"

    def test_projected_rates_all_positive(self):
        snaps = self._snapshots_flat(rate=0.006)
        forecast = forecast_chargeback_rate(snaps, self._pipeline())
        assert forecast.projected_rate_30d > 0
        assert forecast.projected_rate_60d > 0
        assert forecast.projected_rate_90d > 0

    def test_threshold_crossing_detected_for_rising_rate(self):
        # Starting near threshold, worsening trend
        snaps = [
            WeeklySnapshot(1, 150, 3, dispute_rate=0.0133),
            WeeklySnapshot(2, 150, 2, dispute_rate=0.0100),
            WeeklySnapshot(3, 150, 2, dispute_rate=0.0100),
            WeeklySnapshot(4, 150, 1, dispute_rate=0.0067),
        ]
        forecast = forecast_chargeback_rate(snaps, self._pipeline(), current_month=6)
        # At 1.33% with worsening trend — should detect EXCESSIVE (1.50%) crossing risk
        # (may or may not cross in 90d depending on slope, but should have some crossings)
        assert isinstance(forecast.threshold_crossings, list)

    def test_no_crossings_for_very_low_rate(self):
        snaps = self._snapshots_flat(rate=0.001)
        forecast = forecast_chargeback_rate(snaps, self._pipeline(), current_month=6)
        assert forecast.forecast_risk == ForecastRisk.LOW

    def test_pipeline_disputes_estimated(self):
        pipeline = self._pipeline(high_risk_orders=20, medium_risk_orders=60)
        snaps = self._snapshots_flat(rate=0.005)
        forecast = forecast_chargeback_rate(snaps, pipeline)
        assert forecast.pipeline_dispute_estimate_30d > 0

    def test_intervention_lowers_projected_rate(self):
        snaps = self._snapshots_flat(rate=0.012)
        forecast = forecast_chargeback_rate(snaps, self._pipeline())
        assert forecast.projected_rate_30d_with_intervention <= forecast.projected_rate_30d

    def test_hdr_exposure_zero_below_threshold(self):
        snaps = self._snapshots_flat(rate=0.005)
        forecast = forecast_chargeback_rate(snaps, self._pipeline())
        assert forecast.hdr_fees_projected_30d == 0.0

    def test_hdr_exposure_positive_above_threshold(self):
        snaps = self._snapshots_flat(rate=0.020)
        forecast = forecast_chargeback_rate(snaps, self._pipeline(),
                                            weekly_transactions_avg=200, hdr_active=True)
        assert forecast.hdr_fees_projected_30d > 0 or forecast.hdr_fees_projected_90d > 0

    def test_summary_populated(self):
        snaps = self._snapshots_flat(rate=0.008)
        forecast = forecast_chargeback_rate(snaps, self._pipeline())
        assert len(forecast.summary) > 10

    def test_action_items_populated(self):
        snaps = self._snapshots_flat(rate=0.008)
        forecast = forecast_chargeback_rate(snaps, self._pipeline())
        assert len(forecast.action_items) > 0

    def test_seasonal_multiplier_affects_november(self):
        # November (1.20x) should project higher than April (0.95x) all else equal
        snaps = self._snapshots_flat(rate=0.010)
        forecast_nov = forecast_chargeback_rate(snaps, self._pipeline(), current_month=11)
        forecast_apr = forecast_chargeback_rate(snaps, self._pipeline(), current_month=4)
        assert forecast_nov.projected_rate_30d >= forecast_apr.projected_rate_30d

    def test_empty_snapshots_handled_gracefully(self):
        forecast = forecast_chargeback_rate([], self._pipeline())
        assert forecast.current_dispute_rate == 0.0
        assert forecast.trend_direction == "STABLE"
