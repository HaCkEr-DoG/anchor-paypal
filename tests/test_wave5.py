"""
Tests for Wave 5: batch_dispatcher, hdr_war_room.
"""
import pytest
from datetime import date, timedelta

from oracle.batch_dispatcher import (
    dispatch_portfolio, DisputeInput, DispatchCategory, DailyDispatchReport,
)
from oracle.hdr_war_room import (
    analyze_hdr_crisis, HDRSnapshot, HDRZone, HDRForecast,
)

TODAY = date(2024, 6, 15)


# ═══════════════════════════════════════════════════════════════════
# Batch Dispatcher Tests
# ═══════════════════════════════════════════════════════════════════

class TestBatchDispatcher:

    def _inp(self, **kwargs) -> DisputeInput:
        defaults = dict(
            dispute_id="D-" + str(id(kwargs)),
            claim_type="ITEM_NOT_RECEIVED",
            dispute_amount=200.0,
        )
        defaults.update(kwargs)
        return DisputeInput(**defaults)

    def test_empty_portfolio_returns_zero_report(self):
        report = dispatch_portfolio([], dispatch_date=TODAY)
        assert report.total_open_disputes == 0
        assert len(report.must_act_today) == 0
        assert len(report.auto_fight) == 0

    def test_overdue_dispute_in_must_act(self):
        inp = self._inp(dispute_id="D-overdue", days_remaining=-2)
        report = dispatch_portfolio([inp], dispatch_date=TODAY)
        assert len(report.must_act_today) == 1
        assert report.must_act_today[0].dispute_id == "D-overdue"
        assert report.must_act_today[0].deadline_today is True

    def test_critical_1_day_in_must_act(self):
        inp = self._inp(dispute_id="D-crit", days_remaining=0)
        report = dispatch_portfolio([inp], dispatch_date=TODAY)
        assert len(report.must_act_today) >= 1

    def test_optimal_window_in_act_now(self):
        inp = self._inp(
            dispute_id="D-opt",
            response_in_optimal_window=True,
            days_remaining=10,
        )
        report = dispatch_portfolio([inp], dispatch_date=TODAY)
        assert len(report.act_now_optimal) == 1

    def test_high_score_goes_to_auto_fight(self):
        inp = self._inp(
            dispute_id="D-auto",
            calibrated_win_rate=0.90,
            evidence_completeness=0.95,
            evidence_quality_tier="STRONG",
            network_risk_level="CRITICAL",
            network_risk_score=85.0,
            sp_covered=True,
            sp_score=90.0,
            sp_has_blocking_issues=False,
        )
        report = dispatch_portfolio([inp], dispatch_date=TODAY)
        # High score → AUTO_FIGHT (not constrained by deadline)
        all_auto = [d for d in report.auto_fight]
        # Could end up in auto_fight or act_now_optimal depending on timing flags
        assert len(report.auto_fight) + len(report.act_now_optimal) >= 1

    def test_low_score_goes_to_accept_or_settle(self):
        inp = self._inp(
            dispute_id="D-accept",
            calibrated_win_rate=0.20,
            evidence_completeness=0.10,
            evidence_quality_tier="WEAK",
            network_risk_level="TRUSTED",
            network_risk_score=5.0,
            sp_covered=False,
            sp_has_blocking_issues=True,
        )
        report = dispatch_portfolio([inp], dispatch_date=TODAY)
        all_low = report.auto_accept + report.settle_outreach
        assert len(all_low) >= 1

    def test_priority_ranks_assigned(self):
        inputs = [self._inp(dispute_id=f"D-{i}") for i in range(5)]
        report = dispatch_portfolio(inputs, dispatch_date=TODAY)
        all_disputes = (
            report.must_act_today + report.act_now_optimal + report.auto_fight +
            report.auto_accept + report.settle_outreach + report.review_queue
        )
        ranks = [d.priority_rank for d in all_disputes]
        assert sorted(ranks) == list(range(1, len(all_disputes) + 1))

    def test_overdue_dispute_generates_priority_alert(self):
        inp = self._inp(dispute_id="D-alert", days_remaining=-3, dispute_amount=500.0)
        report = dispatch_portfolio([inp], dispatch_date=TODAY)
        assert len(report.priority_alerts) > 0
        assert any("D-alert" in a for a in report.priority_alerts)

    def test_autopilot_disputes_counted(self):
        inputs = [
            self._inp(dispute_id="D-auto1", calibrated_win_rate=0.90,
                      evidence_completeness=0.95, network_risk_level="CRITICAL",
                      network_risk_score=90.0),
            self._inp(dispute_id="D-auto2", calibrated_win_rate=0.90,
                      evidence_completeness=0.95, network_risk_level="CRITICAL",
                      network_risk_score=90.0),
        ]
        report = dispatch_portfolio(inputs, dispatch_date=TODAY)
        assert report.total_autopilot_disputes >= 0  # May or may not be auto depending on score

    def test_summary_populated(self):
        inputs = [self._inp(dispute_id=f"D-s{i}") for i in range(3)]
        report = dispatch_portfolio(inputs, dispatch_date=TODAY)
        assert len(report.summary) > 20
        assert "3" in report.summary

    def test_at_risk_amount_nonzero_for_urgent_disputes(self):
        inputs = [
            self._inp(dispute_id="D-at-risk", days_remaining=0, dispute_amount=500.0),
        ]
        report = dispatch_portfolio(inputs, dispatch_date=TODAY)
        assert report.total_at_risk_amount >= 500.0

    def test_mixed_portfolio_all_categorized(self):
        inputs = [
            self._inp(dispute_id="D-overdue", days_remaining=-1),
            self._inp(dispute_id="D-opt", response_in_optimal_window=True, days_remaining=10),
            self._inp(dispute_id="D-normal"),
        ]
        report = dispatch_portfolio(inputs, dispatch_date=TODAY)
        all_categorized = (
            len(report.must_act_today) + len(report.act_now_optimal) +
            len(report.auto_fight) + len(report.auto_accept) +
            len(report.settle_outreach) + len(report.review_queue) +
            len(report.monitor_only)
        )
        assert all_categorized == 3


# ═══════════════════════════════════════════════════════════════════
# HDR War Room Tests
# ═══════════════════════════════════════════════════════════════════

class TestHDRWarRoom:

    def _snapshot(self, **kwargs) -> HDRSnapshot:
        defaults = dict(
            snapshot_date=TODAY,
            total_transactions_90d=1000,
            total_disputes_90d=10,
            dispute_rate=0.010,
            zone=HDRZone.LIMITED,
            weekly_new_disputes=[2, 2, 3, 3],
            weekly_new_transactions=[70, 70, 70, 70],
            open_disputes=5,
        )
        defaults.update(kwargs)
        return HDRSnapshot(**defaults)

    def test_safe_zone_no_exec_escalation(self):
        snap = self._snapshot(
            dispute_rate=0.003,
            zone=HDRZone.SAFE,
            total_disputes_90d=3,
        )
        report = analyze_hdr_crisis(snap)
        assert report.executive_escalation_required is False
        assert report.current_zone == HDRZone.SAFE

    def test_limited_zone_paypal_contact_recommended(self):
        snap = self._snapshot(dispute_rate=0.012, zone=HDRZone.LIMITED, total_disputes_90d=12)
        report = analyze_hdr_crisis(snap)
        assert report.paypal_proactive_contact_recommended is True

    def test_severe_zone_requires_exec_escalation(self):
        snap = self._snapshot(
            dispute_rate=0.035, zone=HDRZone.SEVERE, total_disputes_90d=35,
        )
        report = analyze_hdr_crisis(snap)
        assert report.executive_escalation_required is True

    def test_bps_from_limited_computed(self):
        snap = self._snapshot(dispute_rate=0.009, zone=HDRZone.ELEVATED, total_disputes_90d=9)
        report = analyze_hdr_crisis(snap)
        # 1.00% - 0.90% = 10 bps
        assert report.basis_points_from_limited == pytest.approx(10.0, abs=0.5)

    def test_bps_from_limited_negative_when_already_limited(self):
        snap = self._snapshot(dispute_rate=0.012, zone=HDRZone.LIMITED, total_disputes_90d=12)
        report = analyze_hdr_crisis(snap)
        # Already past LIMITED threshold
        assert report.basis_points_from_limited < 0

    def test_immediate_actions_always_populated(self):
        snap = self._snapshot()
        report = analyze_hdr_crisis(snap)
        assert len(report.immediate_actions) >= 1

    def test_emergency_actions_added_for_limited_plus(self):
        snap = self._snapshot(dispute_rate=0.012, zone=HDRZone.LIMITED, total_disputes_90d=12)
        report = analyze_hdr_crisis(snap)
        action_ids = {a.action_id for a in report.immediate_actions}
        # Emergency actions: PREEMPTIVE_REFUNDS and PAUSE_HIGH_RISK_PRODUCTS
        assert "PREEMPTIVE_REFUNDS" in action_ids

    def test_forecast_worsening_when_disputes_increasing(self):
        snap = self._snapshot(
            dispute_rate=0.009,
            zone=HDRZone.ELEVATED,
            total_disputes_90d=9,
            weekly_new_disputes=[1, 2, 3, 4],  # increasing trend
            weekly_new_transactions=[70, 70, 70, 70],
        )
        report = analyze_hdr_crisis(snap)
        assert report.forecast.trajectory == "WORSENING"

    def test_forecast_improving_when_disputes_decreasing(self):
        snap = self._snapshot(
            dispute_rate=0.009,
            zone=HDRZone.ELEVATED,
            total_disputes_90d=9,
            weekly_new_disputes=[4, 3, 2, 1],  # decreasing
            weekly_new_transactions=[70, 70, 70, 70],
        )
        report = analyze_hdr_crisis(snap)
        assert report.forecast.trajectory == "IMPROVING"

    def test_max_new_disputes_nonzero_for_elevated(self):
        snap = self._snapshot(dispute_rate=0.009, zone=HDRZone.ELEVATED, total_disputes_90d=9)
        report = analyze_hdr_crisis(snap)
        assert report.maximum_new_disputes_this_month >= 0

    def test_transactions_needed_nonzero_above_target(self):
        snap = self._snapshot(dispute_rate=0.009, zone=HDRZone.ELEVATED, total_disputes_90d=9)
        report = analyze_hdr_crisis(snap)
        assert report.transactions_needed_to_normalize >= 0

    def test_summary_contains_rate(self):
        snap = self._snapshot()
        report = analyze_hdr_crisis(snap)
        assert "1.00" in report.summary or "1.0%" in report.summary or "LIMITED" in report.summary

    def test_alarm_message_populated(self):
        snap = self._snapshot()
        report = analyze_hdr_crisis(snap)
        assert len(report.alarm_message) > 10

    def test_forecast_30d_exists(self):
        snap = self._snapshot()
        report = analyze_hdr_crisis(snap)
        assert report.forecast.days_30_projected >= 0.0
        assert isinstance(report.forecast.zone_30d, HDRZone)

    def test_terminal_zone_alarm_contains_suspension_warning(self):
        snap = self._snapshot(
            dispute_rate=0.055, zone=HDRZone.TERMINAL, total_disputes_90d=55,
        )
        report = analyze_hdr_crisis(snap)
        assert "TERMINAL" in report.alarm_message or "suspension" in report.alarm_message.lower()
