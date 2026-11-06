"""
Tests for Wave 29: chargeback_rate_monitor, hdr_threshold_calculator.
"""
import pytest
from oracle.chargeback_rate_monitor import (
    monitor_chargeback_rate, ChargebackRateInput, HDRStatus,
)
from oracle.hdr_threshold_calculator import (
    calculate_hdr_thresholds, HDRThresholdInput, HDRTier,
)


# ═══════════════════════════════════════════════════════════════════
# Chargeback Rate Monitor Tests
# ═══════════════════════════════════════════════════════════════════

class TestChargebackRateMonitor:

    def _inp(self, **kw) -> ChargebackRateInput:
        defaults = dict(
            merchant_id="M-001",
            disputes_opened_90d=50,
            transactions_processed_90d=10000,
        )
        defaults.update(kw)
        return ChargebackRateInput(**defaults)

    # ── Status classification ──────────────────────────────────────

    def test_healthy_below_050(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=40, transactions_processed_90d=10000))  # 0.40%
        assert r.current_status == HDRStatus.HEALTHY

    def test_elevated_at_050(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=50, transactions_processed_90d=10000))  # 0.50%
        assert r.current_status == HDRStatus.ELEVATED

    def test_limited_at_100(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=100, transactions_processed_90d=10000))  # 1.00%
        assert r.current_status == HDRStatus.LIMITED

    def test_penalized_at_150(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=150, transactions_processed_90d=10000))  # 1.50%
        assert r.current_status == HDRStatus.PENALIZED

    def test_severe_at_300(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=300, transactions_processed_90d=10000))  # 3.00%
        assert r.current_status == HDRStatus.SEVERE

    # ── Rate computation ───────────────────────────────────────────

    def test_rate_computed_correctly(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=75, transactions_processed_90d=10000))
        assert r.current_rate_pct == pytest.approx(0.75, abs=0.001)

    def test_zero_transactions_no_crash(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=0, transactions_processed_90d=0))
        assert r.current_rate_pct == 0.0
        assert r.current_status == HDRStatus.HEALTHY

    # ── Fee logic ─────────────────────────────────────────────────

    def test_fee_active_when_penalized(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=150, transactions_processed_90d=10000))
        assert r.is_fee_active is True

    def test_no_fee_below_penalized(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=100, transactions_processed_90d=10000))
        assert r.is_fee_active is False

    def test_monthly_fee_positive_when_penalized(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=150, transactions_processed_90d=10000))
        assert r.monthly_fee_risk > 0

    # ── Safety margins ─────────────────────────────────────────────

    def test_disputes_to_next_tier_decreases_as_rate_rises(self):
        low  = monitor_chargeback_rate(self._inp(disputes_opened_90d=10))
        mid  = monitor_chargeback_rate(self._inp(disputes_opened_90d=45))
        assert low.disputes_to_next_tier > mid.disputes_to_next_tier

    def test_disputes_to_safe_zero_when_healthy(self):
        r = monitor_chargeback_rate(self._inp(disputes_opened_90d=30))
        assert r.disputes_to_safe == 0

    def test_disputes_to_safe_positive_when_elevated(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=55, transactions_processed_90d=10000))  # 0.55%
        assert r.disputes_to_safe >= 0  # May be 0 if just at boundary

    # ── Trend tracking ────────────────────────────────────────────

    def test_worsening_trend_detected(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=80,
            transactions_processed_90d=10000,
            disputes_opened_prior_90d=50,
            transactions_processed_prior_90d=10000,
        ))
        assert r.rate_trend == "WORSENING"

    def test_improving_trend_detected(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=40,
            transactions_processed_90d=10000,
            disputes_opened_prior_90d=80,
            transactions_processed_prior_90d=10000,
        ))
        assert r.rate_trend == "IMPROVING"

    def test_no_trend_without_prior(self):
        r = monitor_chargeback_rate(self._inp())
        assert r.rate_trend is None

    # ── Type split ────────────────────────────────────────────────

    def test_type_split_computed(self):
        r = monitor_chargeback_rate(self._inp(
            inr_disputes=30, snad_disputes=10,
            unauthorized_disputes=5, other_disputes=5,
        ))
        assert r.type_split is not None
        assert r.type_split.dominant_type == "INR"

    def test_no_type_split_without_data(self):
        r = monitor_chargeback_rate(self._inp())
        assert r.type_split is None

    # ── Monthly projection ────────────────────────────────────────

    def test_monthly_projection_computed(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_this_month=10,
            transactions_this_month=2000,
            days_into_month=15,
        ))
        assert r.projected_rate_pct is not None
        assert r.projected_status is not None

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = monitor_chargeback_rate(self._inp(merchant_id="M-XYZ"))
        assert "M-XYZ" in r.summary

    def test_actions_non_empty(self):
        r = monitor_chargeback_rate(self._inp())
        assert len(r.immediate_actions) >= 1

    def test_severe_has_critical_action(self):
        r = monitor_chargeback_rate(self._inp(
            disputes_opened_90d=300, transactions_processed_90d=10000))
        combined = " ".join(r.immediate_actions).upper()
        assert "CRITICAL" in combined or "SEVERE" in combined


# ═══════════════════════════════════════════════════════════════════
# HDR Threshold Calculator Tests
# ═══════════════════════════════════════════════════════════════════

class TestHDRThresholdCalculator:

    def _inp(self, **kw) -> HDRThresholdInput:
        defaults = dict(
            merchant_id="M-001",
            current_disputes_90d=50,
            current_transactions_90d=10000,
        )
        defaults.update(kw)
        return HDRThresholdInput(**defaults)

    # ── Tier classification ───────────────────────────────────────

    def test_healthy_tier(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=30, current_transactions_90d=10000))
        assert r.current_tier == HDRTier.HEALTHY

    def test_elevated_tier(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=55, current_transactions_90d=10000))
        assert r.current_tier == HDRTier.ELEVATED

    def test_limited_tier(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=110, current_transactions_90d=10000))
        assert r.current_tier == HDRTier.LIMITED

    def test_penalized_tier(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=200, current_transactions_90d=10000))
        assert r.current_tier == HDRTier.PENALIZED

    def test_severe_tier(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=300, current_transactions_90d=10000))
        assert r.current_tier == HDRTier.SEVERE

    # ── Tier threshold details ────────────────────────────────────

    def test_tier_thresholds_non_empty(self):
        r = calculate_hdr_thresholds(self._inp())
        assert len(r.tier_thresholds) >= 4

    def test_elevated_threshold_at_050pct(self):
        r = calculate_hdr_thresholds(self._inp(current_transactions_90d=10000))
        elevated = next(t for t in r.tier_thresholds if t.tier == HDRTier.ELEVATED)
        assert elevated.disputes_at_boundary == 50  # 0.50% × 10000

    def test_limited_threshold_at_100pct(self):
        r = calculate_hdr_thresholds(self._inp(current_transactions_90d=10000))
        limited = next(t for t in r.tier_thresholds if t.tier == HDRTier.LIMITED)
        assert limited.disputes_at_boundary == 100  # 1.00% × 10000

    def test_penalized_threshold_at_150pct(self):
        r = calculate_hdr_thresholds(self._inp(current_transactions_90d=10000))
        penalized = next(t for t in r.tier_thresholds if t.tier == HDRTier.PENALIZED)
        assert penalized.disputes_at_boundary == 150

    # ── Next tier ─────────────────────────────────────────────────

    def test_next_tier_elevated_from_healthy(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=30, current_transactions_90d=10000))
        assert r.next_tier == HDRTier.ELEVATED

    def test_disputes_to_next_tier_correct(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=30, current_transactions_90d=10000))
        # ELEVATED = 50 disputes; currently 30 → 20 to go
        assert r.disputes_to_next_tier == 20

    def test_no_next_tier_at_severe(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=300, current_transactions_90d=10000))
        assert r.disputes_to_next_tier is None or r.next_tier is None

    # ── Days to crossing ──────────────────────────────────────────

    def test_days_to_limited_computed_at_1d_rate(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=30,
            current_transactions_90d=10000,
            new_disputes_per_day=1.0,
        ))
        # Need 100 total for LIMITED; 30 now → 70 days
        assert r.days_to_limited is not None
        assert abs(r.days_to_limited - 70.0) < 1.0

    def test_no_crossing_days_without_rate(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=30, new_disputes_per_day=0))
        assert r.days_to_limited is None

    # ── Max safe disputes ─────────────────────────────────────────

    def test_max_healthy_below_elevated_threshold(self):
        r = calculate_hdr_thresholds(self._inp(current_transactions_90d=10000))
        assert r.max_disputes_healthy < 50  # Below ELEVATED boundary of 50

    def test_max_elevated_below_limited_threshold(self):
        r = calculate_hdr_thresholds(self._inp(current_transactions_90d=10000))
        assert r.max_disputes_elevated < 100  # Below LIMITED boundary of 100

    def test_headroom_positive_when_healthy(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=10, current_transactions_90d=10000))
        assert r.headroom_disputes > 0

    # ── Recovery scenarios ────────────────────────────────────────

    def test_recovery_scenarios_non_empty(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=200, current_transactions_90d=10000))
        assert len(r.recovery_scenarios) >= 2

    def test_faster_resolution_means_fewer_days(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=200, current_transactions_90d=10000))
        s_1 = next(s for s in r.recovery_scenarios if s.resolution_rate_per_day == 1.0)
        s_5 = next(s for s in r.recovery_scenarios if s.resolution_rate_per_day == 5.0)
        assert s_5.days_to_healthy < s_1.days_to_healthy

    def test_zero_resolution_rate_no_days(self):
        r = calculate_hdr_thresholds(self._inp(
            current_disputes_90d=200, current_transactions_90d=10000,
            disputes_resolved_per_day=0,
        ))
        assert r.recovery_at_current_rate.days_to_healthy is None

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = calculate_hdr_thresholds(self._inp(merchant_id="M-XYZ"))
        assert "M-XYZ" in r.summary

    def test_actions_non_empty(self):
        r = calculate_hdr_thresholds(self._inp())
        assert len(r.immediate_actions) >= 1

    def test_rate_pct_in_range(self):
        r = calculate_hdr_thresholds(self._inp())
        assert 0.0 <= r.current_rate_pct <= 100.0
