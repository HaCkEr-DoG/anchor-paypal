"""
Tests for Wave 10: hdr_war_room, ab_tester.
"""
import pytest
from oracle.hdr_war_room import (
    assess_hdr_war_room, HDRWarRoomInput, HDRZone,
    InterventionType, WarRoomReport,
)
from oracle.ab_tester import (
    create_experiment, record_outcome, analyze_experiment, assign_dispute_to_arm,
    Experiment, ExperimentArm, ExperimentResult, ExperimentStatus,
    DisputeOutcome, ArmLabel,
)


# ═══════════════════════════════════════════════════════════════════
# HDR War Room Tests
# ═══════════════════════════════════════════════════════════════════

class TestHDRWarRoom:

    def _inp(self, **kwargs) -> HDRWarRoomInput:
        defaults = dict(
            current_hdr_rate=0.012,       # LIMITED zone
            disputes_last_90d=120,
            transactions_last_90d=10000,
            disputes_last_30d=40,
            transactions_last_30d=3300,
            monthly_gmv=200000.0,
            avg_dispute_amount=150.0,
            high_risk_product_pct=0.30,
            high_risk_channel_pct=0.20,
            open_at_risk_disputes=15,
            avg_at_risk_dispute_amount=100.0,
        )
        defaults.update(kwargs)
        return HDRWarRoomInput(**defaults)

    def test_safe_zone_classified_correctly(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.003))
        assert r.current_zone == HDRZone.SAFE
        assert r.is_emergency is False

    def test_elevated_zone_classified(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.007))
        assert r.current_zone == HDRZone.ELEVATED

    def test_limited_zone_is_emergency(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.012))
        assert r.current_zone == HDRZone.LIMITED
        assert r.is_emergency is True

    def test_fee_tier_zone(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.020))
        assert r.current_zone == HDRZone.FEE_TIER

    def test_severe_zone(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.035))
        assert r.current_zone == HDRZone.SEVERE

    def test_terminal_zone(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.055))
        assert r.current_zone == HDRZone.TERMINAL

    def test_no_hdr_fee_in_safe_zone(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.003))
        assert r.monthly_hdr_fee == 0.0

    def test_hdr_fee_nonzero_in_fee_tier(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.025))
        assert r.monthly_hdr_fee > 0

    def test_annual_fee_twelve_times_monthly(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.025))
        assert r.fee_tier_annual_cost == pytest.approx(r.monthly_hdr_fee * 12, rel=0.001)

    def test_nine_projections_returned(self):
        r = assess_hdr_war_room(self._inp())
        assert len(r.projections) == 9  # 3 scenarios × 3 time horizons

    def test_aggressive_better_than_do_nothing_at_90d(self):
        r = assess_hdr_war_room(self._inp())
        p_dn = next(p for p in r.projections if p.scenario == "do_nothing" and p.days_out == 90)
        p_agg = next(p for p in r.projections if p.scenario == "aggressive_intervention" and p.days_out == 90)
        assert p_agg.projected_rate <= p_dn.projected_rate

    def test_interventions_non_empty(self):
        r = assess_hdr_war_room(self._inp())
        assert len(r.interventions) >= 2

    def test_preemptive_refund_when_open_disputes(self):
        r = assess_hdr_war_room(self._inp(open_at_risk_disputes=20))
        types = [i.intervention_type for i in r.interventions]
        assert InterventionType.PREEMPTIVE_REFUNDS in types

    def test_no_preemptive_refund_when_no_open_disputes(self):
        r = assess_hdr_war_room(self._inp(open_at_risk_disputes=0))
        types = [i.intervention_type for i in r.interventions]
        assert InterventionType.PREEMPTIVE_REFUNDS not in types

    def test_contact_paypal_in_severe_zone(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.035))
        types = [i.intervention_type for i in r.interventions]
        assert InterventionType.CONTACT_PAYPAL in types

    def test_contact_paypal_not_in_safe_zone(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.003))
        types = [i.intervention_type for i in r.interventions]
        assert InterventionType.CONTACT_PAYPAL not in types

    def test_bps_to_next_threshold_positive_in_elevated(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.007))
        assert r.bps_to_next_threshold > 0

    def test_summary_contains_zone(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.012))
        assert "LIMITED" in r.summary or "1.20" in r.summary

    def test_avoid_actions_populated(self):
        r = assess_hdr_war_room(self._inp())
        assert len(r.avoid_actions) >= 3

    def test_zone_assessment_contains_rate(self):
        r = assess_hdr_war_room(self._inp(current_hdr_rate=0.012))
        assert "1.20%" in r.zone_assessment or "HDR" in r.zone_assessment


# ═══════════════════════════════════════════════════════════════════
# A/B Tester Tests
# ═══════════════════════════════════════════════════════════════════

class TestABTester:

    def _exp(self, **kwargs) -> Experiment:
        return create_experiment(
            experiment_id="EXP-001",
            name="Signature confirmation lift",
            description="Does adding signature confirmation improve INR win rate?",
            claim_type="ITEM_NOT_RECEIVED",
            control_name="standard",
            control_description="Standard tracking only",
            treatment_name="signature_required",
            treatment_description="Tracking + signature confirmation",
            min_sample_per_arm=kwargs.pop("min_sample_per_arm", 20),
            **kwargs,
        )

    def test_deterministic_assignment(self):
        arm1 = assign_dispute_to_arm("D-001", "EXP-001")
        arm2 = assign_dispute_to_arm("D-001", "EXP-001")
        assert arm1 == arm2

    def test_different_disputes_distributed(self):
        arms = [assign_dispute_to_arm(f"D-{i}", "EXP-001") for i in range(100)]
        control_count = arms.count(ArmLabel.CONTROL)
        treatment_count = arms.count(ArmLabel.TREATMENT)
        # Should be roughly 50/50 (allow wide tolerance)
        assert 30 <= control_count <= 70
        assert 30 <= treatment_count <= 70

    def test_record_win_increments_correct_arm(self):
        exp = self._exp()
        # Force a known assignment by using a dispute we know goes to treatment
        # Find a dispute ID that goes to treatment
        for i in range(200):
            did = f"D-trt-{i}"
            if assign_dispute_to_arm(did, "EXP-001") == ArmLabel.TREATMENT:
                exp2 = record_outcome(exp, did, DisputeOutcome.WIN)
                assert exp2.treatment_arm.wins == 1
                assert exp2.control_arm.wins == 0
                break

    def test_record_loss_increments_losses(self):
        exp = self._exp()
        for i in range(200):
            did = f"D-ctrl-{i}"
            if assign_dispute_to_arm(did, "EXP-001") == ArmLabel.CONTROL:
                exp2 = record_outcome(exp, did, DisputeOutcome.LOSS)
                assert exp2.control_arm.losses == 1
                break

    def test_pending_outcome_no_change(self):
        exp = self._exp()
        exp2 = record_outcome(exp, "D-pending", DisputeOutcome.PENDING)
        assert exp2.control_arm.total == 0
        assert exp2.treatment_arm.total == 0

    def test_inconclusive_with_no_data(self):
        exp = self._exp()
        result = analyze_experiment(exp)
        assert result.status == ExperimentStatus.INCONCLUSIVE
        assert result.winner is None

    def test_significant_result_when_treatment_clearly_wins(self):
        exp = self._exp(min_sample_per_arm=5)
        # Manually build arms with clear difference
        from oracle.ab_tester import ExperimentArm, ArmLabel
        exp.control_arm = ExperimentArm("standard", ArmLabel.CONTROL, "", wins=10, losses=20)
        exp.treatment_arm = ExperimentArm("sig", ArmLabel.TREATMENT, "", wins=30, losses=10)
        result = analyze_experiment(exp)
        assert result.is_significant
        assert result.winner == "sig"

    def test_control_wins_when_treatment_worse(self):
        exp = self._exp(min_sample_per_arm=5)
        from oracle.ab_tester import ExperimentArm, ArmLabel
        exp.control_arm = ExperimentArm("standard", ArmLabel.CONTROL, "", wins=30, losses=10)
        exp.treatment_arm = ExperimentArm("sig", ArmLabel.TREATMENT, "", wins=10, losses=30)
        result = analyze_experiment(exp)
        assert result.is_significant
        assert result.winner == "standard"

    def test_p_value_in_range(self):
        exp = self._exp(min_sample_per_arm=5)
        from oracle.ab_tester import ExperimentArm, ArmLabel
        exp.control_arm = ExperimentArm("A", ArmLabel.CONTROL, "", wins=15, losses=15)
        exp.treatment_arm = ExperimentArm("B", ArmLabel.TREATMENT, "", wins=20, losses=10)
        result = analyze_experiment(exp)
        assert 0 <= result.p_value <= 1.0

    def test_absolute_lift_computed(self):
        exp = self._exp(min_sample_per_arm=5)
        from oracle.ab_tester import ExperimentArm, ArmLabel
        exp.control_arm = ExperimentArm("A", ArmLabel.CONTROL, "", wins=10, losses=10)
        exp.treatment_arm = ExperimentArm("B", ArmLabel.TREATMENT, "", wins=15, losses=5)
        result = analyze_experiment(exp)
        # Control WR = 0.50, Treatment WR = 0.75, lift = 0.25
        assert result.absolute_lift == pytest.approx(0.25, abs=0.01)

    def test_relative_lift_computed(self):
        exp = self._exp(min_sample_per_arm=5)
        from oracle.ab_tester import ExperimentArm, ArmLabel
        exp.control_arm = ExperimentArm("A", ArmLabel.CONTROL, "", wins=10, losses=10)
        exp.treatment_arm = ExperimentArm("B", ArmLabel.TREATMENT, "", wins=15, losses=5)
        result = analyze_experiment(exp)
        # Control WR = 0.50, relative lift = 0.25/0.50 = 0.50
        assert result.relative_lift == pytest.approx(0.50, abs=0.02)

    def test_mde_decreases_with_sample_size(self):
        exp_small = self._exp(min_sample_per_arm=5)
        exp_large = self._exp(min_sample_per_arm=5)
        from oracle.ab_tester import ExperimentArm, ArmLabel
        exp_small.control_arm = ExperimentArm("A", ArmLabel.CONTROL, "", wins=10, losses=10)
        exp_small.treatment_arm = ExperimentArm("B", ArmLabel.TREATMENT, "", wins=10, losses=10)
        exp_large.control_arm = ExperimentArm("A", ArmLabel.CONTROL, "", wins=100, losses=100)
        exp_large.treatment_arm = ExperimentArm("B", ArmLabel.TREATMENT, "", wins=100, losses=100)
        r_small = analyze_experiment(exp_small)
        r_large = analyze_experiment(exp_large)
        assert r_large.minimum_detectable_effect < r_small.minimum_detectable_effect

    def test_confidence_level_in_range(self):
        exp = self._exp(min_sample_per_arm=5)
        from oracle.ab_tester import ExperimentArm, ArmLabel
        exp.control_arm = ExperimentArm("A", ArmLabel.CONTROL, "", wins=15, losses=15)
        exp.treatment_arm = ExperimentArm("B", ArmLabel.TREATMENT, "", wins=15, losses=15)
        result = analyze_experiment(exp)
        assert 0 <= result.confidence_level <= 1.0

    def test_summary_contains_experiment_id(self):
        exp = self._exp()
        result = analyze_experiment(exp)
        assert result.experiment_id == "EXP-001"
        assert len(result.summary) > 20

    def test_recommendation_populated(self):
        exp = self._exp()
        result = analyze_experiment(exp)
        assert len(result.recommendation) > 20

    def test_experiment_arm_win_rate_zero_when_no_data(self):
        arm = ExperimentArm("test", ArmLabel.CONTROL, "desc")
        assert arm.win_rate == 0.0
        assert arm.total == 0

    def test_experiment_arm_has_enough_data_threshold(self):
        arm = ExperimentArm("test", ArmLabel.CONTROL, "desc", wins=10, losses=10)
        assert arm.has_enough_data is True
        arm2 = ExperimentArm("test", ArmLabel.CONTROL, "desc", wins=5, losses=5)
        assert arm2.has_enough_data is False
