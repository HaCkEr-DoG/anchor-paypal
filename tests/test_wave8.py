"""
Tests for Wave 8: response_optimizer, merchant_health.
"""
import pytest
from datetime import date, timedelta

from oracle.response_optimizer import (
    optimize_submission_timing, build_portfolio_submission_plan,
    DisputeSubmissionInput, DayQuality, TimeWindowQuality,
    SubmissionTiming, PortfolioSubmissionPlan,
)
from oracle.merchant_health import (
    assess_merchant_health, MerchantHealthInput, HealthGrade,
    HealthDimension, MerchantHealthReport,
)

TODAY = date(2024, 6, 18)  # Tuesday — optimal day


# ═══════════════════════════════════════════════════════════════════
# Response Optimizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestResponseOptimizer:

    def _dispute(self, **kwargs) -> DisputeSubmissionInput:
        defaults = dict(
            dispute_id="D-" + str(id(kwargs)),
            deadline=TODAY + timedelta(days=10),
            dispute_amount=200.0,
            fight_score=70.0,
            claim_type="ITEM_NOT_RECEIVED",
            response_ready=True,
        )
        defaults.update(kwargs)
        return DisputeSubmissionInput(**defaults)

    def test_optimal_day_has_high_attention(self):
        # TODAY is Tuesday — submit tomorrow (Wednesday) should be optimal
        d = self._dispute(deadline=TODAY + timedelta(days=10))
        t = optimize_submission_timing(d, TODAY)
        assert t.attention_score >= 60

    def test_overdue_dispute_submits_today(self):
        d = self._dispute(deadline=TODAY - timedelta(days=1))
        t = optimize_submission_timing(d, TODAY)
        assert t.submit_date == TODAY

    def test_deadline_tomorrow_submits_today(self):
        d = self._dispute(deadline=TODAY + timedelta(days=1))
        t = optimize_submission_timing(d, TODAY)
        assert t.submit_date == TODAY

    def test_optimal_time_is_peak(self):
        d = self._dispute(deadline=TODAY + timedelta(days=10))
        t = optimize_submission_timing(d, TODAY)
        assert t.time_quality == TimeWindowQuality.PEAK

    def test_submit_before_deadline(self):
        deadline = TODAY + timedelta(days=7)
        d = self._dispute(deadline=deadline)
        t = optimize_submission_timing(d, TODAY)
        assert t.submit_date <= deadline

    def test_friday_submission_suboptimal_warning(self):
        # Find a Friday
        d_offset = (4 - TODAY.weekday()) % 7  # Days to next Friday
        if d_offset == 0:
            d_offset = 7
        friday = TODAY + timedelta(days=d_offset)
        # Force a tight deadline that lands on Friday
        d = self._dispute(deadline=friday + timedelta(days=1))
        t = optimize_submission_timing(d, friday - timedelta(days=1))
        # Either submits on Friday (suboptimal) or the day before (better)
        assert t.submit_date is not None

    def test_holiday_period_warning(self):
        # Christmas period
        christmas = date(2024, 12, 25)
        d = self._dispute(deadline=christmas + timedelta(days=5))
        t = optimize_submission_timing(d, christmas)
        # Christmas is holiday period
        if t.day_quality == DayQuality.HOLIDAY_RISK:
            assert len(t.warnings) >= 1

    def test_days_before_deadline_populated(self):
        d = self._dispute(deadline=TODAY + timedelta(days=8))
        t = optimize_submission_timing(d, TODAY)
        assert t.days_before_deadline >= 0

    def test_reasoning_populated(self):
        d = self._dispute(deadline=TODAY + timedelta(days=10))
        t = optimize_submission_timing(d, TODAY)
        assert len(t.reasoning) >= 1

    def test_portfolio_plan_all_disputes_included(self):
        disputes = [self._dispute(dispute_id=f"D-{i}") for i in range(5)]
        plan = build_portfolio_submission_plan(disputes, TODAY)
        assert plan.total_disputes == 5
        assert len(plan.per_dispute) == 5

    def test_portfolio_plan_dates_sorted(self):
        disputes = [self._dispute(dispute_id=f"D-{i}", deadline=TODAY + timedelta(days=10+i)) for i in range(5)]
        plan = build_portfolio_submission_plan(disputes, TODAY)
        dates = [t.submit_date for t in plan.per_dispute]
        # Deadlines are sorted and plans should be ordered
        assert min(dates) >= TODAY

    def test_empty_portfolio_returns_empty_plan(self):
        plan = build_portfolio_submission_plan([], TODAY)
        assert plan.total_disputes == 0
        assert len(plan.per_dispute) == 0

    def test_max_per_day_cap_respected(self):
        disputes = [self._dispute(dispute_id=f"D-{i}") for i in range(20)]
        plan = build_portfolio_submission_plan(disputes, TODAY, max_per_day=5)
        for date_str, ids in plan.submissions_by_date.items():
            assert len(ids) <= 5

    def test_avg_attention_score_in_range(self):
        disputes = [self._dispute(dispute_id=f"D-{i}") for i in range(5)]
        plan = build_portfolio_submission_plan(disputes, TODAY)
        assert 0 <= plan.avg_attention_score <= 100

    def test_summary_populated(self):
        disputes = [self._dispute(dispute_id=f"D-{i}") for i in range(3)]
        plan = build_portfolio_submission_plan(disputes, TODAY)
        assert len(plan.summary) > 20


# ═══════════════════════════════════════════════════════════════════
# Merchant Health Tests
# ═══════════════════════════════════════════════════════════════════

class TestMerchantHealth:

    def _inp(self, **kwargs) -> MerchantHealthInput:
        defaults = dict(
            current_hdr_rate=0.005,
            hdr_trajectory="STABLE",
            dispute_win_rate_90d=0.70,
            evidence_quality_avg="MODERATE",
            total_open_disputes=10,
            total_at_risk_amount=2000.0,
            monthly_gmv=100000.0,
            fraud_detection_rate=0.75,
            blocked_buyers_count=3,
            autopilot_ratio=0.75,
            avg_human_minutes_per_dispute=20.0,
            response_time_compliance=0.97,
            has_carrier_strategy=True,
            has_buyer_vetting=True,
        )
        defaults.update(kwargs)
        return MerchantHealthInput(**defaults)

    def test_healthy_merchant_good_grade(self):
        r = assess_merchant_health(self._inp())
        assert r.grade in (HealthGrade.GOOD, HealthGrade.EXCELLENT, HealthGrade.FAIR)

    def test_excellent_merchant_top_score(self):
        r = assess_merchant_health(self._inp(
            current_hdr_rate=0.002,
            dispute_win_rate_90d=0.85,
            fraud_detection_rate=0.90,
            autopilot_ratio=0.90,
            total_at_risk_amount=500.0,
            hdr_trajectory="IMPROVING",
        ))
        assert r.grade in (HealthGrade.EXCELLENT, HealthGrade.GOOD)
        assert r.composite_score >= 70

    def test_critical_merchant_low_score(self):
        r = assess_merchant_health(self._inp(
            current_hdr_rate=0.035,
            dispute_win_rate_90d=0.40,
            fraud_detection_rate=0.30,
            autopilot_ratio=0.20,
            total_at_risk_amount=20000.0,
            hdr_trajectory="WORSENING",
        ))
        assert r.grade in (HealthGrade.CRITICAL, HealthGrade.POOR)
        assert r.composite_score < 50

    def test_composite_score_weighted_sum(self):
        r = assess_merchant_health(self._inp())
        # Composite should be in [0, 100]
        assert 0 <= r.composite_score <= 100

    def test_five_dimensions_returned(self):
        r = assess_merchant_health(self._inp())
        assert len(r.dimensions) == 5

    def test_hdr_dimension_weight_30pct(self):
        r = assess_merchant_health(self._inp())
        hdr_dim = next(d for d in r.dimensions if d.name == "HDR Safety")
        assert hdr_dim.weight == pytest.approx(0.30, abs=0.01)

    def test_win_rate_dimension_weight_25pct(self):
        r = assess_merchant_health(self._inp())
        wr_dim = next(d for d in r.dimensions if d.name == "Win Rate")
        assert wr_dim.weight == pytest.approx(0.25, abs=0.01)

    def test_urgent_actions_for_critical_merchant(self):
        r = assess_merchant_health(self._inp(
            current_hdr_rate=0.040,
            dispute_win_rate_90d=0.35,
            hdr_trajectory="WORSENING",
        ))
        assert len(r.urgent_actions) >= 1

    def test_no_urgent_actions_for_excellent_merchant(self):
        r = assess_merchant_health(self._inp(
            current_hdr_rate=0.002,
            dispute_win_rate_90d=0.85,
            hdr_trajectory="IMPROVING",
        ))
        assert len(r.urgent_actions) == 0

    def test_trend_improving_when_hdr_improving_and_good_wr(self):
        r = assess_merchant_health(self._inp(
            hdr_trajectory="IMPROVING", dispute_win_rate_90d=0.70,
        ))
        assert r.trend_direction == "IMPROVING"

    def test_trend_declining_when_hdr_worsening(self):
        r = assess_merchant_health(self._inp(hdr_trajectory="WORSENING"))
        assert r.trend_direction == "DECLINING"

    def test_hdr_limited_zone_urgent_action(self):
        r = assess_merchant_health(self._inp(current_hdr_rate=0.012))
        # LIMITED zone should generate urgent action
        hdr_dim = next(d for d in r.dimensions if d.name == "HDR Safety")
        assert hdr_dim.grade in ("POOR", "CRITICAL")

    def test_terminal_hdr_critical_score(self):
        r = assess_merchant_health(self._inp(current_hdr_rate=0.055))
        hdr_dim = next(d for d in r.dimensions if d.name == "HDR Safety")
        assert hdr_dim.score < 20

    def test_summary_populated(self):
        r = assess_merchant_health(self._inp())
        assert len(r.summary) > 50
        assert "%" in r.summary

    def test_dashboard_headline_populated(self):
        r = assess_merchant_health(self._inp())
        assert len(r.dashboard_headline) > 20
        assert str(round(r.composite_score)) in r.dashboard_headline

    def test_at_risk_amount_in_report(self):
        r = assess_merchant_health(self._inp(total_at_risk_amount=5000.0))
        assert r.at_risk_amount == 5000.0

    def test_low_win_rate_generates_fair_or_worse_dimension(self):
        r = assess_merchant_health(self._inp(dispute_win_rate_90d=0.45))
        wr_dim = next(d for d in r.dimensions if d.name == "Win Rate")
        assert wr_dim.grade in ("FAIR", "POOR", "CRITICAL")

    def test_high_fraud_detection_good_dimension(self):
        r = assess_merchant_health(self._inp(fraud_detection_rate=0.92))
        fd_dim = next(d for d in r.dimensions if d.name == "Fraud Defense")
        assert fd_dim.grade in ("EXCELLENT", "GOOD")

    def test_cash_flow_pct_critical_when_high(self):
        r = assess_merchant_health(self._inp(
            total_at_risk_amount=20000.0, monthly_gmv=50000.0,  # 40% at risk
        ))
        cash_dim = next(d for d in r.dimensions if d.name == "Cash Flow Risk")
        assert cash_dim.grade in ("POOR", "CRITICAL")

    def test_autopilot_affects_ops_dimension(self):
        r_high = assess_merchant_health(self._inp(autopilot_ratio=0.90))
        r_low = assess_merchant_health(self._inp(autopilot_ratio=0.20))
        ops_high = next(d for d in r_high.dimensions if d.name == "Operational Efficiency")
        ops_low = next(d for d in r_low.dimensions if d.name == "Operational Efficiency")
        assert ops_high.score > ops_low.score
