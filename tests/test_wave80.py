"""Wave 80 — Refund Timing Optimizer tests."""
import pytest
from oracle.refund_timing_optimizer import (
    TimingDecision,
    RefundTimingInput,
    calculate_refund_timing,
)


def _inp(
    amount=299.0,
    p_win=0.70,
    p_esc=0.20,
    hours_elapsed=24.0,
    deadline=240.0,
    rc=15.0,
    cb_fee=20.0,
    partial_rate=0.50,
):
    return RefundTimingInput(
        dispute_id="PP-D-TIMING",
        transaction_amount=amount,
        p_win=p_win,
        p_escalation=p_esc,
        hours_elapsed=hours_elapsed,
        deadline_hours_total=deadline,
        response_cost_usd=rc,
        cb_fee_usd=cb_fee,
        partial_refund_rate=partial_rate,
    )


# ─── EV ordering ──────────────────────────────────────────────────────────────

class TestEVOrdering:
    def test_refund_now_ev_negative_amount(self):
        r = calculate_refund_timing(_inp(amount=100.0))
        assert abs(r.ev_refund_now - (-100.0)) < 0.001

    def test_partial_refund_ev_less_negative_than_full(self):
        r = calculate_refund_timing(_inp(amount=100.0, partial_rate=0.50))
        assert r.ev_partial_refund > r.ev_refund_now

    def test_high_win_prob_fight_better_than_refund(self):
        r = calculate_refund_timing(_inp(amount=100.0, p_win=0.90, p_esc=0.05))
        assert r.ev_wait_and_fight > r.ev_refund_now

    def test_low_win_prob_refund_better_than_fight(self):
        r = calculate_refund_timing(_inp(amount=100.0, p_win=0.10, p_esc=0.05))
        assert r.ev_refund_now > r.ev_wait_and_fight

    def test_high_escalation_lowers_fight_ev(self):
        r_low_esc = calculate_refund_timing(_inp(p_esc=0.05))
        r_high_esc = calculate_refund_timing(_inp(p_esc=0.90))
        assert r_high_esc.ev_wait_and_fight < r_low_esc.ev_wait_and_fight


# ─── Decision logic ───────────────────────────────────────────────────────────

class TestDecisionLogic:
    def test_urgent_deadline_recommends_refund_or_partial(self):
        r = calculate_refund_timing(_inp(hours_elapsed=230.0, deadline=240.0))
        assert r.recommended_decision in (TimingDecision.REFUND_NOW, TimingDecision.PARTIAL_REFUND)

    def test_high_win_prob_recommends_fight(self):
        r = calculate_refund_timing(_inp(p_win=0.90, p_esc=0.05, hours_elapsed=12.0))
        assert r.recommended_decision == TimingDecision.WAIT_AND_FIGHT

    def test_low_win_prob_high_escalation_recommends_refund(self):
        r = calculate_refund_timing(_inp(amount=100.0, p_win=0.10, p_esc=0.90, hours_elapsed=10.0))
        assert r.recommended_decision in (
            TimingDecision.REFUND_NOW,
            TimingDecision.CONDITIONAL_REFUND,
            TimingDecision.PARTIAL_REFUND,
        )

    def test_decision_is_valid_enum(self):
        r = calculate_refund_timing(_inp())
        assert r.recommended_decision in list(TimingDecision)


# ─── Deadline urgency ─────────────────────────────────────────────────────────

class TestDeadlineUrgency:
    def test_urgent_when_less_than_24h_remaining(self):
        r = calculate_refund_timing(_inp(hours_elapsed=220.0, deadline=240.0))
        assert r.deadline_urgency == "URGENT"

    def test_approaching_24_to_72h_remaining(self):
        r = calculate_refund_timing(_inp(hours_elapsed=185.0, deadline=240.0))
        assert r.deadline_urgency == "APPROACHING"

    def test_comfortable_when_plenty_of_time(self):
        r = calculate_refund_timing(_inp(hours_elapsed=24.0, deadline=240.0))
        assert r.deadline_urgency == "COMFORTABLE"

    def test_hours_remaining_computed_correctly(self):
        r = calculate_refund_timing(_inp(hours_elapsed=50.0, deadline=240.0))
        assert abs(r.hours_remaining - 190.0) < 0.1


# ─── PayPal savings ───────────────────────────────────────────────────────────

class TestPayPalSavings:
    def test_refund_decision_generates_savings(self):
        r = calculate_refund_timing(_inp(p_win=0.10, p_esc=0.90))
        if r.recommended_decision == TimingDecision.REFUND_NOW:
            assert r.paypal_savings_usd > 0.0

    def test_fight_decision_zero_savings(self):
        r = calculate_refund_timing(_inp(p_win=0.90, p_esc=0.05, hours_elapsed=12.0))
        assert r.paypal_savings_usd == 0.0

    def test_savings_non_negative(self):
        r = calculate_refund_timing(_inp())
        assert r.paypal_savings_usd >= 0.0


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_dispute_id_echoed(self):
        r = calculate_refund_timing(_inp())
        assert r.dispute_id == "PP-D-TIMING"

    def test_recommendation_non_empty(self):
        r = calculate_refund_timing(_inp())
        assert len(r.recommendation) > 20

    def test_paypal_signal_contains_dispute_id(self):
        r = calculate_refund_timing(_inp())
        assert "PP-D-TIMING" in r.paypal_signal

    def test_escalation_adjusted_loss_negative_or_zero(self):
        r = calculate_refund_timing(_inp(p_esc=0.50))
        assert r.escalation_adjusted_loss <= 0.0

    def test_all_ev_values_are_floats(self):
        r = calculate_refund_timing(_inp())
        assert isinstance(r.ev_refund_now, float)
        assert isinstance(r.ev_wait_and_fight, float)
        assert isinstance(r.ev_partial_refund, float)
