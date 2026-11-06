"""
Tests for Wave 18: appeal_strategy_engine, dispute_lifecycle_tracker.
"""
import pytest
from oracle.appeal_strategy_engine import (
    evaluate_appeal, AppealInput, AppealDecision, AppealGround,
)
from oracle.dispute_lifecycle_tracker import (
    analyze_lifecycle, validate_transition, LifecycleInput,
    DisputeState, DisputeAction, AnomalyType,
)


# ═══════════════════════════════════════════════════════════════════
# Appeal Strategy Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestAppealStrategyEngine:

    def _inp(self, **kw) -> AppealInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            dispute_amount=200.0,
            original_outcome="LOST",
            days_since_ruling=3,
            is_chargeback=False,
        )
        defaults.update(kw)
        return AppealInput(**defaults)

    def test_deadline_passed_do_not_appeal(self):
        # PayPal appeal deadline = 10 days; 11 days elapsed
        r = evaluate_appeal(self._inp(days_since_ruling=11))
        assert r.decision == AppealDecision.DO_NOT_APPEAL
        assert r.deadline_days_remaining == 0

    def test_deadline_passed_chargeback_45d(self):
        # Chargeback deadline = 45 days
        r = evaluate_appeal(self._inp(is_chargeback=True, days_since_ruling=46))
        assert r.decision == AppealDecision.DO_NOT_APPEAL

    def test_chargeback_still_valid_at_30_days(self):
        r = evaluate_appeal(self._inp(is_chargeback=True, days_since_ruling=30))
        assert r.deadline_days_remaining > 0

    def test_factual_error_strong_appeal(self):
        r = evaluate_appeal(self._inp(has_factual_error_proof=True))
        assert r.decision in (AppealDecision.APPEAL_STRONG, AppealDecision.APPEAL_MODERATE)
        assert AppealGround.FACTUAL_ERROR in r.appeal_grounds

    def test_buyer_acknowledgment_strong(self):
        r = evaluate_appeal(self._inp(has_buyer_acknowledgment=True))
        assert r.decision in (AppealDecision.APPEAL_STRONG, AppealDecision.APPEAL_MODERATE)
        assert AppealGround.BUYER_ACKNOWLEDGMENT in r.appeal_grounds

    def test_ce30_eligible_unauthorized(self):
        r = evaluate_appeal(self._inp(claim_type="UNAUTHORIZED", ce30_eligible=True))
        assert AppealGround.CE30_ELIGIBLE in r.appeal_grounds

    def test_ce30_does_not_add_ground_for_inr(self):
        r = evaluate_appeal(self._inp(claim_type="INR", ce30_eligible=True))
        assert AppealGround.CE30_ELIGIBLE not in r.appeal_grounds

    def test_no_new_evidence_do_not_appeal(self):
        r = evaluate_appeal(self._inp())
        # No new evidence → weak appeal → EV negative → do not appeal
        assert r.decision == AppealDecision.DO_NOT_APPEAL

    def test_ev_appeal_positive_for_strong(self):
        r = evaluate_appeal(self._inp(has_factual_error_proof=True, dispute_amount=500.0))
        assert r.ev_appeal > 0

    def test_ev_appeal_negative_for_no_grounds(self):
        r = evaluate_appeal(self._inp(dispute_amount=50.0, appeal_cost=15.0))
        # p_win ~ 0.15, EV = 0.15*50 - 15 = -7.5
        assert r.ev_appeal < 0

    def test_p_appeal_win_range(self):
        r = evaluate_appeal(self._inp(has_factual_error_proof=True))
        assert 0.0 <= r.p_appeal_win <= 1.0

    def test_appeal_text_non_empty_for_strong(self):
        r = evaluate_appeal(self._inp(has_factual_error_proof=True))
        assert len(r.appeal_text) > 50

    def test_appeal_text_empty_for_do_not_appeal(self):
        r = evaluate_appeal(self._inp())
        assert r.appeal_text == ""

    def test_required_evidence_includes_factual_error_docs(self):
        r = evaluate_appeal(self._inp(has_factual_error_proof=True))
        evidence_text = " ".join(r.required_evidence).lower()
        assert "error" in evidence_text or "tracking" in evidence_text

    def test_summary_contains_dispute_id(self):
        r = evaluate_appeal(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_days_remaining_correct(self):
        # 10d deadline, 7d elapsed → 3d remaining
        r = evaluate_appeal(self._inp(days_since_ruling=7, is_chargeback=False))
        assert r.deadline_days_remaining == 3

    def test_new_tracking_adds_new_evidence_ground(self):
        r = evaluate_appeal(self._inp(has_new_tracking_evidence=True))
        assert AppealGround.NEW_EVIDENCE in r.appeal_grounds

    def test_multiple_grounds_accumulate(self):
        r = evaluate_appeal(self._inp(
            has_factual_error_proof=True,
            has_buyer_acknowledgment=True,
        ))
        assert len(r.appeal_grounds) >= 2


# ═══════════════════════════════════════════════════════════════════
# Dispute Lifecycle Tracker Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeLifecycleTracker:

    def _inp(self, state=DisputeState.WAITING_MERCHANT, days=3, **kw) -> LifecycleInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            dispute_amount=150.0,
            is_formal_dispute=True,
            merchant_responded=False,
            total_days_open=days,
        )
        defaults.update(kw)
        return LifecycleInput(
            current_state=state,
            days_in_current_state=days,
            **defaults,
        )

    # Transition validation
    def test_open_to_waiting_merchant_valid(self):
        v = validate_transition(DisputeState.OPEN, DisputeState.WAITING_MERCHANT)
        assert v.is_valid is True

    def test_open_to_resolved_won_invalid(self):
        v = validate_transition(DisputeState.OPEN, DisputeState.RESOLVED_WON)
        assert v.is_valid is False

    def test_waiting_merchant_to_under_review_valid(self):
        v = validate_transition(DisputeState.WAITING_MERCHANT, DisputeState.UNDER_REVIEW)
        assert v.is_valid is True

    def test_waiting_merchant_to_refunded_valid(self):
        v = validate_transition(DisputeState.WAITING_MERCHANT, DisputeState.REFUNDED)
        assert v.is_valid is True

    def test_resolved_won_has_no_transitions(self):
        v = validate_transition(DisputeState.RESOLVED_WON, DisputeState.APPEALING)
        assert v.is_valid is False

    def test_resolved_lost_to_appealing_valid(self):
        v = validate_transition(DisputeState.RESOLVED_LOST, DisputeState.APPEALING)
        assert v.is_valid is True

    def test_terminal_states_no_transitions(self):
        for s in (DisputeState.AUTO_CLOSED, DisputeState.REFUNDED, DisputeState.APPEAL_WON):
            v = validate_transition(s, DisputeState.OPEN)
            assert v.is_valid is False

    # Lifecycle analysis
    def test_waiting_merchant_recommend_submit(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.WAITING_MERCHANT))
        assert DisputeAction.SUBMIT_RESPONSE in r.available_actions

    def test_resolved_lost_recommend_appeal(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.RESOLVED_LOST))
        assert DisputeAction.FILE_APPEAL in r.available_actions

    def test_appeal_lost_escalate_available(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.APPEAL_LOST))
        assert DisputeAction.ESCALATE in r.available_actions

    def test_resolved_won_is_terminal(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.RESOLVED_WON))
        assert r.is_terminal is True
        assert r.is_won is True

    def test_resolved_lost_is_lost(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.RESOLVED_LOST))
        assert r.is_lost is True

    def test_missed_deadline_anomaly(self):
        # WAITING_MERCHANT deadline = 10d; in state for 15d
        r = analyze_lifecycle(self._inp(state=DisputeState.WAITING_MERCHANT, days=15))
        types = [a.anomaly_type for a in r.anomalies]
        assert AnomalyType.MISSED_DEADLINE in types

    def test_approaching_deadline_anomaly(self):
        # WAITING_MERCHANT deadline = 10d; in state for 9d
        r = analyze_lifecycle(self._inp(state=DisputeState.WAITING_MERCHANT, days=9))
        types = [a.anomaly_type for a in r.anomalies]
        assert AnomalyType.APPROACHING_AUTO_CLOSE in types or AnomalyType.MISSED_DEADLINE in types

    def test_no_anomaly_for_new_dispute(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.WAITING_MERCHANT, days=2))
        missed = [a for a in r.anomalies if a.anomaly_type == AnomalyType.MISSED_DEADLINE]
        assert len(missed) == 0

    def test_deadline_days_waiting_merchant(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.WAITING_MERCHANT, days=4))
        assert r.deadline_days == 10
        assert r.days_until_deadline == 6

    def test_no_deadline_for_under_review(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.UNDER_REVIEW, days=5))
        assert r.deadline_days is None

    def test_summary_contains_state(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.WAITING_MERCHANT))
        assert "WAITING_MERCHANT" in r.summary

    def test_summary_contains_dispute_id(self):
        r = analyze_lifecycle(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_possible_next_states_non_empty_for_active(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.WAITING_MERCHANT))
        assert len(r.possible_next_states) > 0

    def test_possible_next_states_empty_for_terminal(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.RESOLVED_WON))
        assert len(r.possible_next_states) == 0

    def test_auto_close_risk_for_waiting_buyer_long(self):
        r = analyze_lifecycle(self._inp(state=DisputeState.WAITING_BUYER, days=16))
        assert r.days_until_auto_close is not None
        assert r.days_until_auto_close > 0
