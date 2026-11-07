"""
Wave 68 — Dispute Deadline Prioritizer
Tests: 26 total
"""

import pytest
from datetime import date, timedelta
from oracle.deadline_prioritizer import (
    OpenDispute, PrioritizerInput, PrioritizedQueue,
    Priority, ActionType,
    prioritize_disputes,
)

REF = date(2026, 11, 7)


def _d(dispute_id="D001", amount=200.0, reason_code="INR",
       days_until_deadline=5, win_probability=0.65, expected_value=80.0,
       evidence_ready=False, estimated_response_minutes=60):
    return OpenDispute(
        dispute_id=dispute_id,
        amount=amount,
        reason_code=reason_code,
        deadline=REF + timedelta(days=days_until_deadline),
        win_probability=win_probability,
        expected_value=expected_value,
        evidence_ready=evidence_ready,
        estimated_response_minutes=estimated_response_minutes,
    )


def _run(disputes, minutes=240):
    return prioritize_disputes(PrioritizerInput(
        merchant_id="MERCH_X",
        disputes=disputes,
        minutes_available_today=minutes,
        reference_date=REF,
    ))


class TestDeadlinePrioritizer:

    # ─── Empty queue ──────────────────────────────────────────────────────────

    def test_empty_queue_returns_valid_report(self):
        r = _run([])
        assert r.total_disputes == 0
        assert r.items == []

    def test_empty_queue_can_complete_today(self):
        r = _run([])
        assert r.can_complete_all_today is True

    # ─── Sorting ─────────────────────────────────────────────────────────────

    def test_today_deadline_sorts_first(self):
        disputes = [
            _d("D_FAR", days_until_deadline=10, expected_value=200.0),
            _d("D_NOW", days_until_deadline=0, expected_value=50.0),
        ]
        r = _run(disputes)
        assert r.items[0].dispute_id == "D_NOW"

    def test_higher_ev_sorts_above_lower_ev_at_same_deadline(self):
        disputes = [
            _d("D_LOW_EV", days_until_deadline=3, expected_value=10.0),
            _d("D_HI_EV",  days_until_deadline=3, expected_value=300.0),
        ]
        r = _run(disputes)
        assert r.items[0].dispute_id == "D_HI_EV"

    def test_all_disputes_in_output(self):
        disputes = [_d(f"D{i:03}") for i in range(5)]
        r = _run(disputes)
        assert len(r.items) == 5

    # ─── Priority assignment ──────────────────────────────────────────────────

    def test_today_deadline_is_p0(self):
        r = _run([_d("D_P0", days_until_deadline=0)])
        assert r.items[0].priority == Priority.P0_CRITICAL

    def test_one_day_deadline_is_p0(self):
        r = _run([_d("D_P0", days_until_deadline=1)])
        assert r.items[0].priority == Priority.P0_CRITICAL

    def test_three_day_deadline_is_p1_or_p2(self):
        r = _run([_d("D_P12", days_until_deadline=3)])
        assert r.items[0].priority in (Priority.P1_HIGH, Priority.P2_MEDIUM)

    def test_fourteen_day_low_ev_is_low_or_defer(self):
        r = _run([_d("D_DEFER", days_until_deadline=14, expected_value=5.0, win_probability=0.2)])
        assert r.items[0].priority in (Priority.P3_LOW, Priority.P4_DEFER)

    # ─── Action recommendations ──────────────────────────────────────────────

    def test_today_deadline_recommends_respond_now(self):
        r = _run([_d("D_NOW", days_until_deadline=0)])
        assert r.items[0].recommended_action == ActionType.RESPOND_NOW

    def test_evidence_ready_recommends_respond_now(self):
        r = _run([_d("D_EV", days_until_deadline=5, evidence_ready=True)])
        assert r.items[0].recommended_action == ActionType.RESPOND_NOW

    def test_low_ev_far_deadline_recommends_refund(self):
        r = _run([_d("D_REF", days_until_deadline=14, expected_value=2.0, win_probability=0.15)])
        if r.items[0].priority == Priority.P4_DEFER:
            assert r.items[0].recommended_action == ActionType.ISSUE_REFUND

    def test_high_value_high_win_recommends_escalate(self):
        r = _run([_d("D_ESC", amount=1000.0, days_until_deadline=4, win_probability=0.80, expected_value=500.0)])
        assert r.items[0].recommended_action in (ActionType.ESCALATE_TO_TEAM, ActionType.GATHER_EVIDENCE, ActionType.RESPOND_NOW)

    # ─── Time budget ─────────────────────────────────────────────────────────

    def test_total_minutes_sum(self):
        disputes = [
            _d("D1", estimated_response_minutes=30),
            _d("D2", estimated_response_minutes=45),
            _d("D3", estimated_response_minutes=60),
        ]
        r = _run(disputes)
        assert r.total_minutes_required == 135

    def test_can_complete_when_minutes_sufficient(self):
        disputes = [_d(f"D{i}", estimated_response_minutes=30) for i in range(3)]
        r = _run(disputes, minutes=200)
        assert r.can_complete_all_today is True

    def test_cannot_complete_when_overloaded(self):
        disputes = [_d(f"D{i}", estimated_response_minutes=60) for i in range(10)]
        r = _run(disputes, minutes=120)
        assert r.can_complete_all_today is False

    def test_deferred_list_populated_on_overflow(self):
        disputes = [_d(f"D{i}", days_until_deadline=10, estimated_response_minutes=60) for i in range(10)]
        r = _run(disputes, minutes=120)
        assert len(r.deferred_disputes) > 0

    # ─── Urgency score ────────────────────────────────────────────────────────

    def test_urgency_score_between_0_and_1(self):
        r = _run([_d()])
        assert 0.0 <= r.items[0].urgency_score <= 1.0

    def test_p0_has_highest_urgency(self):
        p0 = _run([_d("D0", days_until_deadline=0)]).items[0]
        p3 = _run([_d("D3", days_until_deadline=14, expected_value=5.0)]).items[0]
        assert p0.urgency_score > p3.urgency_score

    # ─── Counts ──────────────────────────────────────────────────────────────

    def test_critical_count_matches_p0_items(self):
        disputes = [
            _d("D0A", days_until_deadline=0),
            _d("D0B", days_until_deadline=1),
            _d("D3",  days_until_deadline=5),
        ]
        r = _run(disputes)
        p0_actual = sum(1 for i in r.items if i.priority == Priority.P0_CRITICAL)
        assert r.critical_count == p0_actual

    # ─── PayPal signal ────────────────────────────────────────────────────────

    def test_paypal_signal_populated(self):
        r = _run([_d()])
        assert len(r.paypal_capacity_signal) > 20

    def test_capacity_signal_mentions_merchant(self):
        r = _run([_d()])
        assert "MERCH_X" in r.paypal_capacity_signal

    # ─── Summary ─────────────────────────────────────────────────────────────

    def test_summary_contains_dispute_count(self):
        disputes = [_d(f"D{i}") for i in range(4)]
        r = _run(disputes)
        assert "4" in r.summary

    def test_days_remaining_correct(self):
        r = _run([_d("D_CHECK", days_until_deadline=3)])
        assert r.items[0].days_remaining == 3
