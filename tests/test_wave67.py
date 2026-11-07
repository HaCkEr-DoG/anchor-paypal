"""
Wave 67 — Refund ROI Calculator
Tests: 28 total
"""

import pytest
from oracle.refund_roi_calculator import (
    RefundROIInput,
    RefundROIReport,
    DisputeAction,
    ROIConfidence,
    MerchantSegment,
    calculate_refund_roi,
)


def _inp(**kwargs):
    defaults = dict(
        dispute_id="DISP_001",
        merchant_id="MERCH_X",
        dispute_amount=150.0,
        reason_code="INR",
        win_probability=0.70,
        merchant_segment=MerchantSegment.SMALL_BUSINESS,
        buyer_ltv=300.0,
        is_repeat_buyer=True,
        buyer_prior_orders=3,
        refund_goodwill_value=0.0,
        partial_refund_pct=0.0,
    )
    defaults.update(kwargs)
    return RefundROIInput(**defaults)


class TestRefundROICalculator:

    # ─── Basic structure ───────────────────────────────────────────────────────

    def test_returns_report(self):
        r = calculate_refund_roi(_inp())
        assert isinstance(r, RefundROIReport)

    def test_ev_values_are_floats(self):
        r = calculate_refund_roi(_inp())
        assert isinstance(r.ev_fight, float)
        assert isinstance(r.ev_refund, float)

    def test_dispute_id_returned(self):
        r = calculate_refund_roi(_inp(dispute_id="DISP_XYZ"))
        assert r.dispute_id == "DISP_XYZ"

    def test_merchant_id_returned(self):
        r = calculate_refund_roi(_inp(merchant_id="SHOP_99"))
        assert r.merchant_id == "SHOP_99"

    def test_summary_contains_dispute_id(self):
        r = calculate_refund_roi(_inp(dispute_id="DISP_SUMMARY"))
        assert "DISP_SUMMARY" in r.summary

    # ─── High win probability → fight ─────────────────────────────────────────

    def test_high_win_prob_recommends_fight(self):
        r = calculate_refund_roi(_inp(
            win_probability=0.90,
            buyer_ltv=50.0,         # low LTV = less retention bonus
            dispute_amount=200.0,
            merchant_segment=MerchantSegment.ENTERPRISE,  # low response cost
        ))
        assert r.recommended_action == DisputeAction.FIGHT

    def test_ev_fight_positive_at_high_win_prob(self):
        r = calculate_refund_roi(_inp(
            win_probability=0.95,
            dispute_amount=500.0,
            merchant_segment=MerchantSegment.ENTERPRISE,
        ))
        assert r.ev_fight > 0

    def test_fight_confidence_high_on_clear_win(self):
        r = calculate_refund_roi(_inp(
            win_probability=0.95,
            dispute_amount=500.0,
            buyer_ltv=10.0,
            merchant_segment=MerchantSegment.ENTERPRISE,
        ))
        assert r.confidence in (ROIConfidence.HIGH, ROIConfidence.MEDIUM)

    # ─── Low win probability → refund ─────────────────────────────────────────

    def test_low_win_prob_may_recommend_refund(self):
        r = calculate_refund_roi(_inp(
            win_probability=0.10,
            buyer_ltv=500.0,        # high LTV = strong retention bonus
            dispute_amount=50.0,
            merchant_segment=MerchantSegment.MICRO,
        ))
        # Either refund or partial — should not be FIGHT with 10% win
        assert r.recommended_action in (DisputeAction.REFUND, DisputeAction.PARTIAL_REFUND)

    def test_ev_fight_negative_at_low_win_prob_micro(self):
        r = calculate_refund_roi(_inp(
            win_probability=0.05,
            dispute_amount=30.0,
            merchant_segment=MerchantSegment.MICRO,
        ))
        assert r.ev_fight < 0

    def test_low_win_fight_confidence_demoted(self):
        r = calculate_refund_roi(_inp(
            win_probability=0.10,
            dispute_amount=50.0,
            buyer_ltv=5.0,
            merchant_segment=MerchantSegment.MICRO,
        ))
        if r.recommended_action == DisputeAction.FIGHT:
            assert r.confidence == ROIConfidence.LOW

    # ─── Segment cost differentiation ─────────────────────────────────────────

    def test_enterprise_has_lower_response_cost_than_micro(self):
        enterprise = calculate_refund_roi(_inp(merchant_segment=MerchantSegment.ENTERPRISE))
        micro = calculate_refund_roi(_inp(merchant_segment=MerchantSegment.MICRO))
        assert enterprise.response_cost_used < micro.response_cost_used

    def test_unauthorized_has_higher_response_cost_than_inr(self):
        inr = calculate_refund_roi(_inp(reason_code="INR"))
        unauth = calculate_refund_roi(_inp(reason_code="UNAUTHORIZED"))
        assert unauth.response_cost_used > inr.response_cost_used

    def test_response_cost_override(self):
        r = calculate_refund_roi(_inp(response_cost_override=5.0))
        assert r.response_cost_used == 5.0

    def test_chargeback_fee_override(self):
        r = calculate_refund_roi(_inp(chargeback_fee_override=50.0))
        assert r.chargeback_fee_used == 50.0

    # ─── LTV retention modeling ───────────────────────────────────────────────

    def test_high_ltv_buyer_boosts_ev_refund(self):
        low_ltv = calculate_refund_roi(_inp(buyer_ltv=10.0))
        high_ltv = calculate_refund_roi(_inp(buyer_ltv=1000.0))
        assert high_ltv.ev_refund > low_ltv.ev_refund

    def test_repeat_buyer_boosts_refund_ev(self):
        new = calculate_refund_roi(_inp(is_repeat_buyer=False, buyer_prior_orders=0))
        repeat = calculate_refund_roi(_inp(is_repeat_buyer=True, buyer_prior_orders=10))
        assert repeat.ev_refund >= new.ev_refund

    def test_goodwill_value_override_used(self):
        r = calculate_refund_roi(_inp(refund_goodwill_value=100.0, buyer_ltv=0.0))
        assert r.ev_refund == round(-150.0 + 100.0, 2)

    # ─── Delta and flags ──────────────────────────────────────────────────────

    def test_fight_refund_delta_matches_evs(self):
        r = calculate_refund_roi(_inp())
        expected = round(r.ev_fight - r.ev_refund, 2)
        assert r.fight_refund_delta == expected

    def test_is_fight_roi_positive_flag(self):
        r = calculate_refund_roi(_inp(
            win_probability=0.95,
            merchant_segment=MerchantSegment.ENTERPRISE,
            dispute_amount=500.0,
        ))
        assert r.is_fight_roi_positive is (r.ev_fight > 0)

    def test_is_refund_roi_better_flag(self):
        r = calculate_refund_roi(_inp())
        assert r.is_refund_roi_better is (r.ev_refund > r.ev_fight)

    # ─── Loss-leader detection ────────────────────────────────────────────────

    def test_both_negative_ev_possible(self):
        r = calculate_refund_roi(_inp(
            win_probability=0.05,
            dispute_amount=10.0,
            buyer_ltv=0.0,
            merchant_segment=MerchantSegment.MICRO,
            refund_goodwill_value=0.0,
        ))
        # ev_fight almost certainly negative; ev_refund also negative (no LTV)
        assert r.ev_fight < 0
        assert r.ev_refund < 0

    # ─── PayPal signal ────────────────────────────────────────────────────────

    def test_paypal_signal_populated(self):
        r = calculate_refund_roi(_inp())
        assert len(r.paypal_signal) > 20

    def test_paypal_signal_mentions_merchant(self):
        r = calculate_refund_roi(_inp(merchant_id="MERCH_PP_TEST"))
        assert "MERCH_PP_TEST" in r.paypal_signal

    # ─── Key insight and summary ──────────────────────────────────────────────

    def test_key_insight_populated(self):
        r = calculate_refund_roi(_inp())
        assert len(r.key_insight) > 20

    def test_summary_contains_recommended_action(self):
        r = calculate_refund_roi(_inp())
        assert r.recommended_action.value in r.summary

    def test_summary_contains_win_prob(self):
        r = calculate_refund_roi(_inp(win_probability=0.70))
        assert "70%" in r.summary
