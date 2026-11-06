"""
Tests for Wave 3: network_intelligence, carrier_intel, timeline_optimizer, legal_escalation.
"""
import pytest
from datetime import date, timedelta

from oracle.network_intelligence import (
    BuyerNetworkProfile, NetworkRiskLevel, NetworkSignalType,
    analyze_network_signals, network_summary,
)
from oracle.carrier_intel import (
    analyze_carrier, CarrierTrustLevel, CarrierVulnerability,
    best_carrier_for_order,
)
from oracle.timeline_optimizer import (
    DisputeStage, ActionUrgency, ActionType,
    optimize_response_timing, build_portfolio_schedule,
)
from oracle.legal_escalation import (
    assess_escalation, EscalationPath, EscalationUrgency,
)


TODAY = date(2024, 6, 15)


# ═══════════════════════════════════════════════════════════════════
# Network Intelligence Tests
# ═══════════════════════════════════════════════════════════════════

class TestNetworkIntelligence:

    def _profile(self, **kwargs) -> BuyerNetworkProfile:
        defaults = dict(
            email_hash="abc123",
            total_transactions_on_network=0,
            total_disputes_on_network=0,
            disputes_last_30d=0,
            disputes_last_90d=0,
            merchants_disputed_last_90d=0,
            total_merchants_transacted=0,
            total_merchants_disputed=0,
            merchant_wins_vs_buyer=0,
            buyer_wins_vs_merchant=0,
            shared_device_fraud_count=0,
        )
        defaults.update(kwargs)
        return BuyerNetworkProfile(**defaults)

    def test_new_buyer_gets_insufficient_data_signal(self):
        profile = self._profile()
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 100.0)
        signal_types = [s.signal_type for s in report.signals]
        assert NetworkSignalType.INSUFFICIENT_DATA in signal_types

    def test_serial_fraudster_50_disputes_critical(self):
        # 60 disputes / 80 tx = 75% rate → 40pts; 6 recent disputes → 30pts; breadth ≥3 → 10pts = 80 → CRITICAL
        profile = self._profile(
            total_disputes_on_network=60,
            total_merchants_disputed=20,
            total_transactions_on_network=80,
            disputes_last_30d=6,
            merchants_disputed_last_90d=5,
        )
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 150.0)
        assert report.network_risk_level == NetworkRiskLevel.CRITICAL
        signal_types = [s.signal_type for s in report.signals]
        assert NetworkSignalType.SERIAL_FRAUDSTER in signal_types

    def test_velocity_spike_5_disputes_30d_critical(self):
        profile = self._profile(
            disputes_last_30d=7,
            merchants_disputed_last_90d=5,
            total_transactions_on_network=20,
            total_disputes_on_network=7,
        )
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 200.0)
        signal_types = [s.signal_type for s in report.signals]
        assert NetworkSignalType.VELOCITY_SPIKE in signal_types
        assert report.network_risk_score > 30

    def test_trusted_buyer_long_clean_history(self):
        profile = self._profile(
            total_transactions_on_network=50,
            total_disputes_on_network=0,
            disputes_last_30d=0,
            total_merchants_transacted=10,
        )
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 100.0)
        assert report.network_risk_level == NetworkRiskLevel.TRUSTED
        signal_types = [s.signal_type for s in report.signals]
        assert NetworkSignalType.CLEAN_BUYER in signal_types

    def test_device_fraud_ring_raises_risk(self):
        profile = self._profile(
            shared_device_fraud_count=5,
            total_transactions_on_network=10,
            total_disputes_on_network=2,
        )
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 300.0)
        signal_types = [s.signal_type for s in report.signals]
        assert NetworkSignalType.DEVICE_FINGERPRINT in signal_types
        assert report.network_risk_score > 20

    def test_fight_recommendation_for_critical_buyer(self):
        profile = self._profile(
            total_disputes_on_network=55,
            total_merchants_disputed=25,
            total_transactions_on_network=70,
        )
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 200.0)
        assert report.fight_recommendation == "FIGHT"
        assert report.alert_merchant is True

    def test_win_probability_adjustment_positive_for_fraud(self):
        profile = self._profile(
            total_disputes_on_network=50,
            total_merchants_disputed=20,
            total_transactions_on_network=65,
            disputes_last_30d=6,
            merchants_disputed_last_90d=4,
        )
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 200.0)
        assert report.win_probability_adjustment > 0

    def test_win_probability_adjustment_negative_for_trusted(self):
        profile = self._profile(
            total_transactions_on_network=30,
            total_disputes_on_network=0,
            total_merchants_transacted=8,
        )
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 100.0)
        assert report.win_probability_adjustment <= 0

    def test_network_win_rate_computed_from_history(self):
        profile = self._profile(
            merchant_wins_vs_buyer=8,
            buyer_wins_vs_merchant=2,
            total_transactions_on_network=20,
            total_disputes_on_network=10,
        )
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 100.0)
        assert report.network_win_rate == pytest.approx(0.8, abs=0.05)

    def test_network_summary_string_populated(self):
        profile = self._profile(
            total_disputes_on_network=15,
            total_merchants_disputed=5,
            total_transactions_on_network=20,
        )
        report = analyze_network_signals(profile, "ITEM_NOT_RECEIVED", 100.0)
        summary = network_summary(report)
        assert len(summary) > 20
        assert "Network:" in summary


# ═══════════════════════════════════════════════════════════════════
# Carrier Intel Tests
# ═══════════════════════════════════════════════════════════════════

class TestCarrierIntel:

    def test_usps_priority_platinum_trust(self):
        result = analyze_carrier("USPS", "Priority Mail", order_amount=200.0)
        assert result.trust_level == CarrierTrustLevel.PLATINUM

    def test_usps_first_class_silver_trust(self):
        result = analyze_carrier("USPS First Class", order_amount=100.0)
        assert result.trust_level == CarrierTrustLevel.SILVER

    def test_fedex_express_platinum_trust(self):
        result = analyze_carrier("FedEx Express", order_amount=1000.0)
        assert result.trust_level == CarrierTrustLevel.PLATINUM

    def test_ontrac_bronze_trust(self):
        result = analyze_carrier("OnTrac", order_amount=100.0)
        assert result.trust_level == CarrierTrustLevel.BRONZE

    def test_lasership_risky_trust(self):
        result = analyze_carrier("LaserShip", order_amount=50.0)
        assert result.trust_level == CarrierTrustLevel.RISKY

    def test_usps_priority_high_inr_win_rate(self):
        result = analyze_carrier("USPS", order_amount=200.0, claim_type="ITEM_NOT_RECEIVED")
        assert result.inr_win_rate >= 0.80

    def test_upgrade_recommended_for_750_without_signature(self):
        # OnTrac doesn't have reliable signature — should trigger upgrade for $750+ order
        result = analyze_carrier("OnTrac", order_amount=800.0, claim_type="ITEM_NOT_RECEIVED")
        assert result.carrier_upgrade_recommended is True

    def test_upgrade_recommended_for_risky_carrier(self):
        result = analyze_carrier("LaserShip", order_amount=100.0)
        assert result.carrier_upgrade_recommended is True
        assert result.upgrade_to is not None

    def test_no_upgrade_for_fedex_express_high_value(self):
        result = analyze_carrier("FedEx Express", order_amount=5000.0)
        assert result.carrier_upgrade_recommended is False

    def test_lasership_has_vulnerabilities(self):
        result = analyze_carrier("LaserShip", order_amount=50.0)
        assert len(result.vulnerabilities) > 0

    def test_evidence_requirements_populated_for_weak_carrier(self):
        result = analyze_carrier("OnTrac", order_amount=200.0)
        assert len(result.evidence_requirements) > 0

    def test_unknown_carrier_returns_bronze_conservative(self):
        result = analyze_carrier("SomeFakeCarrier", order_amount=200.0)
        assert result.trust_level == CarrierTrustLevel.BRONZE
        assert result.carrier_upgrade_recommended is True

    def test_best_carrier_high_value_recommends_fedex(self):
        rec = best_carrier_for_order(order_amount=1000.0)
        assert "fedex" in rec.lower() or "express" in rec.lower()

    def test_best_carrier_low_value_recommends_usps_priority(self):
        rec = best_carrier_for_order(order_amount=50.0)
        assert "usps" in rec.lower() or "priority" in rec.lower()

    def test_dhl_has_international_vulnerability(self):
        result = analyze_carrier("DHL", order_amount=200.0)
        assert CarrierVulnerability.INTERNATIONAL_GAP.value in result.vulnerabilities


# ═══════════════════════════════════════════════════════════════════
# Timeline Optimizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestTimelineOptimizer:

    def test_optimal_timing_day_4_for_inr(self):
        filed = TODAY - timedelta(days=4)
        rec = optimize_response_timing(
            "D001", "ITEM_NOT_RECEIVED", filed,
            DisputeStage.OPEN, today=TODAY,
        )
        assert rec.urgency == ActionUrgency.OPTIMAL
        assert rec.act_today is True
        assert rec.today_is_optimal is True

    def test_too_early_day_1_says_wait(self):
        filed = TODAY - timedelta(days=1)
        rec = optimize_response_timing(
            "D002", "ITEM_NOT_RECEIVED", filed,
            DisputeStage.OPEN, today=TODAY,
        )
        assert rec.recommended_action == ActionType.WAIT
        assert rec.urgency == ActionUrgency.LOW

    def test_critical_1_day_remaining(self):
        filed = TODAY - timedelta(days=19)
        rec = optimize_response_timing(
            "D003", "ITEM_NOT_RECEIVED", filed,
            DisputeStage.OPEN, today=TODAY,
        )
        assert rec.urgency in (ActionUrgency.CRITICAL, ActionUrgency.OVERDUE)
        assert rec.act_today is True

    def test_overdue_past_deadline(self):
        filed = TODAY - timedelta(days=25)  # 20d window, 5 days overdue
        rec = optimize_response_timing(
            "D004", "ITEM_NOT_RECEIVED", filed,
            DisputeStage.OPEN, today=TODAY,
        )
        assert rec.urgency == ActionUrgency.OVERDUE
        assert rec.days_remaining < 0

    def test_appeal_window_detected_after_loss(self):
        filed = TODAY - timedelta(days=25)
        decision = TODAY - timedelta(days=3)
        rec = optimize_response_timing(
            "D005", "ITEM_NOT_RECEIVED", filed,
            DisputeStage.DECIDED_AGAINST, decision_date=decision, today=TODAY,
        )
        assert rec.appeal_window_open is True
        assert rec.appeal_deadline is not None
        assert rec.recommended_action == ActionType.SUBMIT_APPEAL

    def test_appeal_critical_when_1_day_left(self):
        filed = TODAY - timedelta(days=25)
        decision = TODAY - timedelta(days=9)  # 10d window, 1 day left
        rec = optimize_response_timing(
            "D006", "ITEM_NOT_RECEIVED", filed,
            DisputeStage.DECIDED_AGAINST, decision_date=decision, today=TODAY,
        )
        assert rec.urgency in (ActionUrgency.CRITICAL, ActionUrgency.OVERDUE)

    def test_unauthorized_shorter_window_optimal_earlier(self):
        # UNAUTHORIZED has 10-day window, optimal days 1-2
        filed = TODAY - timedelta(days=1)
        rec = optimize_response_timing(
            "D007", "UNAUTHORIZED", filed,
            DisputeStage.OPEN, today=TODAY,
        )
        # Day 1 for UNAUTHORIZED is in optimal window (opt_min=1)
        assert rec.urgency == ActionUrgency.OPTIMAL

    def test_days_remaining_computed_correctly(self):
        filed = TODAY - timedelta(days=10)
        rec = optimize_response_timing(
            "D008", "ITEM_NOT_RECEIVED", filed,
            DisputeStage.OPEN, today=TODAY,
        )
        assert rec.days_remaining == 10  # 20 - 10

    def test_portfolio_schedule_sorts_overdue_first(self):
        disputes = [
            {
                "dispute_id": "D-urgent",
                "claim_type": "ITEM_NOT_RECEIVED",
                "filed_date": TODAY - timedelta(days=19),  # 1 day left
                "stage": DisputeStage.OPEN,
            },
            {
                "dispute_id": "D-fine",
                "claim_type": "ITEM_NOT_RECEIVED",
                "filed_date": TODAY - timedelta(days=2),   # 18 days left
                "stage": DisputeStage.OPEN,
            },
        ]
        schedule = build_portfolio_schedule(disputes, today=TODAY)
        assert schedule.total_active_disputes == 2
        # D-urgent should appear in critical or overdue, not D-fine
        urgent_ids = [d.dispute_id for d in schedule.critical_today + schedule.overdue_disputes]
        assert "D-urgent" in urgent_ids

    def test_portfolio_summary_populated(self):
        disputes = [
            {
                "dispute_id": "D-test",
                "claim_type": "ITEM_NOT_RECEIVED",
                "filed_date": TODAY - timedelta(days=4),
                "stage": DisputeStage.OPEN,
            }
        ]
        schedule = build_portfolio_schedule(disputes, today=TODAY)
        assert len(schedule.summary) > 10

    def test_optimal_response_date_set_correctly(self):
        filed = TODAY - timedelta(days=1)
        rec = optimize_response_timing(
            "D-opt", "ITEM_NOT_RECEIVED", filed,
            DisputeStage.OPEN, today=TODAY,
        )
        # Optimal day = (3+5)//2 = 4, so optimal date = filed + 4 days
        expected_optimal = filed + timedelta(days=4)
        assert rec.optimal_response_date == expected_optimal


# ═══════════════════════════════════════════════════════════════════
# Legal Escalation Tests
# ═══════════════════════════════════════════════════════════════════

class TestLegalEscalation:

    def test_small_claims_applicable_for_500_with_tracking(self):
        analysis = assess_escalation(
            dispute_amount=500.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
            has_buyer_address=True,
        )
        sc = next(o for o in analysis.options if o.path == EscalationPath.SMALL_CLAIMS_COURT)
        assert sc.applicable is True
        assert sc.ev > 0

    def test_small_claims_not_applicable_below_200(self):
        analysis = assess_escalation(
            dispute_amount=50.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
            has_buyer_address=True,
        )
        sc = next(o for o in analysis.options if o.path == EscalationPath.SMALL_CLAIMS_COURT)
        assert sc.applicable is False

    def test_cfpb_applicable_for_300_plus(self):
        analysis = assess_escalation(
            dispute_amount=400.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
        )
        cfpb = next(o for o in analysis.options if o.path == EscalationPath.CFPB_COMPLAINT)
        assert cfpb.applicable is True

    def test_cfpb_has_no_filing_cost(self):
        analysis = assess_escalation(
            dispute_amount=400.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
        )
        cfpb = next(o for o in analysis.options if o.path == EscalationPath.CFPB_COMPLAINT)
        assert cfpb.filing_cost == 0.0

    def test_escalation_warranted_for_evidence_loss(self):
        analysis = assess_escalation(
            dispute_amount=600.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
            has_delivery_confirmation=True,
            has_buyer_address=True,
        )
        assert analysis.escalation_warranted is True

    def test_no_escalation_warranted_tiny_amount_no_evidence(self):
        analysis = assess_escalation(
            dispute_amount=25.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=False,
            has_buyer_address=False,
        )
        # No applicable options with positive EV for $25 with no evidence
        assert analysis.recommended_ev == 0.0 or not analysis.escalation_warranted

    def test_paypal_legal_applicable_for_5000_plus(self):
        analysis = assess_escalation(
            dispute_amount=6000.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
        )
        pml = next(o for o in analysis.options if o.path == EscalationPath.PAYPAL_MERCHANT_LEGAL)
        assert pml.applicable is True
        assert pml.urgency == EscalationUrgency.IMMEDIATE

    def test_frozen_account_triggers_cfpb_and_legal(self):
        analysis = assess_escalation(
            dispute_amount=1000.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="IN_REVIEW",
            paypal_account_frozen=True,
        )
        cfpb = next(o for o in analysis.options if o.path == EscalationPath.CFPB_COMPLAINT)
        assert cfpb.applicable is True
        assert analysis.escalation_warranted is True

    def test_state_ag_applicable_with_repeat_buyer(self):
        analysis = assess_escalation(
            dispute_amount=500.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_buyer_address=True,
            buyer_state="CA",
            prior_disputes_this_buyer=3,
        )
        ag = next(o for o in analysis.options if o.path == EscalationPath.STATE_AG_COMPLAINT)
        assert ag.applicable is True

    def test_ic3_applicable_for_fraud_pattern(self):
        analysis = assess_escalation(
            dispute_amount=600.0,
            claim_type="UNAUTHORIZED",
            paypal_decision="AGAINST_MERCHANT",
            is_fraud_pattern=True,
        )
        ic3 = next(o for o in analysis.options if o.path == EscalationPath.IC3_FBI_COMPLAINT)
        assert ic3.applicable is True

    def test_recommended_path_has_highest_ev(self):
        analysis = assess_escalation(
            dispute_amount=800.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
            has_delivery_confirmation=True,
            has_buyer_address=True,
        )
        if analysis.recommended_path and analysis.options:
            recommended = next(
                (o for o in analysis.options if o.path == analysis.recommended_path), None
            )
            if recommended:
                for other in analysis.options:
                    if other.applicable:
                        assert recommended.ev >= other.ev

    def test_priority_actions_populated_when_warranted(self):
        analysis = assess_escalation(
            dispute_amount=750.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
            has_buyer_address=True,
        )
        assert len(analysis.priority_actions) > 0

    def test_all_options_have_how_to_file(self):
        analysis = assess_escalation(
            dispute_amount=500.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
        )
        for option in analysis.options:
            assert len(option.how_to_file) > 0

    def test_delivery_confirmation_raises_small_claims_win_prob(self):
        with_delivery = assess_escalation(
            dispute_amount=500.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
            has_delivery_confirmation=True,
            has_buyer_address=True,
        )
        without_delivery = assess_escalation(
            dispute_amount=500.0,
            claim_type="ITEM_NOT_RECEIVED",
            paypal_decision="AGAINST_MERCHANT",
            has_tracking=True,
            has_delivery_confirmation=False,
            has_buyer_address=True,
        )
        sc_with = next(o for o in with_delivery.options if o.path == EscalationPath.SMALL_CLAIMS_COURT)
        sc_without = next(o for o in without_delivery.options if o.path == EscalationPath.SMALL_CLAIMS_COURT)
        assert sc_with.win_probability >= sc_without.win_probability
