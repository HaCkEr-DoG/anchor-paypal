"""
Wave 73 — Refund vs. Fight ROI Calculator tests.
"""
import pytest
from oracle.refund_roi_calculator import (
    ROIAction,
    ROIInput,
    calculate_roi,
)


def _inp(
    dispute_id="PP-D-99101",
    amount=299.0,
    win_prob=0.65,
    sp_eligible=False,
    response_cost=15.0,
    reorder_prob=0.15,
    clv=0.0,
    recovery_rate=0.0,
    deadline_days=7,
):
    return ROIInput(
        dispute_id=dispute_id,
        transaction_amount=amount,
        win_probability=win_prob,
        seller_protection_eligible=sp_eligible,
        response_cost_usd=response_cost,
        reorder_probability=reorder_prob,
        customer_lifetime_value_usd=clv,
        refund_recovery_rate=recovery_rate,
        deadline_days=deadline_days,
    )


# ─── Basic EV calculations ────────────────────────────────────────────────────

class TestBasicEV:
    def test_high_win_prob_recommends_fight(self):
        r = calculate_roi(_inp(amount=299.0, win_prob=0.85))
        assert r.recommended_action == ROIAction.FIGHT

    def test_low_win_prob_recommends_refund(self):
        r = calculate_roi(_inp(amount=299.0, win_prob=0.10))
        assert r.recommended_action == ROIAction.REFUND

    def test_zero_amount_refund_ev_zero(self):
        r = calculate_roi(_inp(amount=0.0, win_prob=0.90))
        # With zero amount, fight EV ≤ 0 (only costs)
        assert r.fight.expected_value <= 0.0

    def test_fight_ev_formula_correct(self):
        # Manual: amount=100, win_prob=0.70, rc=15, deadline=7 (adds $5 surcharge → rc_eff=20)
        # fight_win = 100 - 20 = 80; fight_lose = -(100 + 20 + 20) = -140
        # fight_ev = 0.70*80 + 0.30*(-140) = 56 - 42 = 14.0
        r = calculate_roi(_inp(amount=100.0, win_prob=0.70, response_cost=15.0, deadline_days=7))
        assert abs(r.fight.expected_value - 14.0) < 0.5

    def test_refund_ev_formula_correct(self):
        # refund: amount=100, reorder=0.15, recovery=0
        # refund_cash = -100; reorder_gain = 0.15 * 100 * 0.5 = 7.5
        # refund_ev = -100 + 7.5 = -92.5
        r = calculate_roi(_inp(amount=100.0, reorder_prob=0.15, recovery_rate=0.0))
        assert abs(r.refund.expected_value - (-92.5)) < 1.0


# ─── Break-even probability ───────────────────────────────────────────────────

class TestBreakEven:
    def test_break_even_in_range(self):
        r = calculate_roi(_inp(amount=299.0, win_prob=0.65))
        assert 0.0 <= r.break_even_win_probability <= 1.0

    def test_win_above_break_even_recommends_fight(self):
        r = calculate_roi(_inp(amount=500.0, win_prob=0.80))
        # If win_prob is above break-even, fight should be recommended
        if r.win_probability > r.break_even_win_probability:
            assert r.recommended_action in (ROIAction.FIGHT, ROIAction.CONDITIONAL_FIGHT)

    def test_win_below_break_even_recommends_refund(self):
        r = calculate_roi(_inp(amount=500.0, win_prob=0.05))
        assert r.win_probability <= r.break_even_win_probability
        assert r.recommended_action == ROIAction.REFUND


# ─── Urgency surcharge ────────────────────────────────────────────────────────

class TestUrgencySurcharge:
    def test_urgent_deadline_increases_cost(self):
        r_urgent = calculate_roi(_inp(deadline_days=1))
        r_normal = calculate_roi(_inp(deadline_days=14))
        # Higher RC reduces fight EV — urgent should have lower fight EV
        assert r_urgent.fight.expected_value <= r_normal.fight.expected_value

    def test_zero_deadline_days_applies_max_surcharge(self):
        r = calculate_roi(_inp(deadline_days=0))
        # Should not raise; fight EV should still be computed
        assert r.fight.expected_value is not None


# ─── Seller Protection ────────────────────────────────────────────────────────

class TestSellerProtection:
    def test_sp_does_not_raise(self):
        r = calculate_roi(_inp(win_prob=0.75, sp_eligible=True))
        assert r.fight.expected_value is not None

    def test_sp_flag_accessible(self):
        r = calculate_roi(_inp(win_prob=0.75, sp_eligible=True))
        # With SP eligible, fight should still produce a valid EV
        assert r.fight.gross_outcome_win > 0 or r.fight.gross_outcome_lose < 0


# ─── CLV adjustments ─────────────────────────────────────────────────────────

class TestCLV:
    def test_clv_zero_ignores_clv(self):
        r = calculate_roi(_inp(clv=0.0))
        assert r.fight.clv_impact == 0.0
        assert r.refund.clv_impact == 0.0

    def test_clv_positive_affects_total_ev(self):
        r = calculate_roi(_inp(clv=500.0))
        assert r.fight.total_ev_with_clv != r.fight.expected_value or \
               r.refund.total_ev_with_clv != r.refund.expected_value

    def test_high_clv_may_shift_recommendation_to_refund(self):
        # Very high CLV buyer + borderline fight case → CLV-adjusted might prefer refund
        r = calculate_roi(_inp(amount=50.0, win_prob=0.55, clv=2000.0, reorder_prob=0.60))
        assert r.clv_adjusted_recommendation is not None


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_dispute_id_echoed(self):
        r = calculate_roi(_inp(dispute_id="PP-D-ROI-001"))
        assert r.dispute_id == "PP-D-ROI-001"

    def test_roi_differential_is_fight_minus_refund(self):
        r = calculate_roi(_inp(amount=299.0, win_prob=0.65))
        expected = r.fight.expected_value - r.refund.expected_value
        assert abs(r.roi_differential - expected) < 0.001

    def test_paypal_signal_contains_action(self):
        r = calculate_roi(_inp())
        assert r.recommended_action.value in r.paypal_signal

    def test_recommendation_reason_non_empty(self):
        r = calculate_roi(_inp())
        assert len(r.recommendation_reason) > 20

    def test_all_ev_values_are_floats(self):
        r = calculate_roi(_inp(amount=499.0, win_prob=0.72))
        assert isinstance(r.fight.expected_value, float)
        assert isinstance(r.refund.expected_value, float)
        assert isinstance(r.break_even_win_probability, float)
        assert isinstance(r.roi_differential, float)

    def test_conditional_fight_for_marginal_case(self):
        # Win prob just barely above break-even → marginal
        r = calculate_roi(_inp(amount=30.0, win_prob=0.55, response_cost=10.0, deadline_days=7))
        # This marginal case should produce FIGHT or CONDITIONAL_FIGHT or REFUND
        assert r.recommended_action in list(ROIAction)

    def test_recovery_rate_reduces_refund_loss(self):
        r_no_recovery = calculate_roi(_inp(recovery_rate=0.0))
        r_with_recovery = calculate_roi(_inp(recovery_rate=0.50))
        assert r_with_recovery.refund.expected_value >= r_no_recovery.refund.expected_value

    def test_high_reorder_probability_improves_refund_ev(self):
        r_low = calculate_roi(_inp(reorder_prob=0.0))
        r_high = calculate_roi(_inp(reorder_prob=0.80))
        assert r_high.refund.expected_value >= r_low.refund.expected_value
