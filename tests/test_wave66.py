"""
Wave 66 — Buyer Satisfaction Risk Scorer
Tests: 30 total
"""

import pytest
from oracle.buyer_satisfaction_risk import (
    ResolvedDisputeContext,
    DisputeOutcome,
    BuyerLTVTier,
    SatisfactionRiskTier,
    RetentionActionType,
    score_buyer_satisfaction_risk,
)


def _ctx(**kwargs):
    defaults = dict(
        dispute_id="DISP_001",
        merchant_id="MERCH_X",
        buyer_id="BUYER_1",
        reason_code="INR",
        dispute_outcome=DisputeOutcome.FULL_REFUND,
        dispute_amount=150.0,
        days_to_resolution=5,
        buyer_ltv_tier=BuyerLTVTier.MEDIUM,
        buyer_total_orders_at_merchant=3,
        buyer_prior_disputes_at_merchant=0,
        buyer_lifetime_spend=300.0,
        merchant_responded_before_escalation=True,
        days_until_refund_issued=1,
        was_tone_empathetic=True,
        buyer_contacted_merchant_first=True,
        is_repeat_issue=False,
    )
    defaults.update(kwargs)
    return ResolvedDisputeContext(**defaults)


class TestBuyerSatisfactionRisk:

    # ─── Low risk baseline ─────────────────────────────────────────────────────

    def test_full_refund_fast_empathetic_is_low_risk(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.FULL_REFUND,
            days_to_resolution=3,
            days_until_refund_issued=1,
            was_tone_empathetic=True,
        )
        r = score_buyer_satisfaction_risk(ctx)
        assert r.risk_tier in (SatisfactionRiskTier.LOW, SatisfactionRiskTier.MEDIUM)

    def test_probabilities_between_0_and_1(self):
        r = score_buyer_satisfaction_risk(_ctx())
        assert 0 <= r.churn_probability <= 1
        assert 0 <= r.negative_review_probability <= 1
        assert 0 <= r.bank_chargeback_probability <= 1

    def test_overall_risk_is_max_of_three(self):
        r = score_buyer_satisfaction_risk(_ctx())
        assert r.overall_satisfaction_risk == max(
            r.churn_probability, r.negative_review_probability, r.bank_chargeback_probability
        )

    def test_no_action_needed_on_low_risk(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.FULL_REFUND,
            days_to_resolution=2,
            was_tone_empathetic=True,
            buyer_prior_disputes_at_merchant=0,
        )
        r = score_buyer_satisfaction_risk(ctx)
        if r.risk_tier == SatisfactionRiskTier.LOW:
            types = [a.action_type for a in r.retention_actions]
            assert RetentionActionType.NO_ACTION_NEEDED in types

    # ─── Outcome signal ───────────────────────────────────────────────────────

    def test_won_by_merchant_highest_outcome_signal(self):
        win = _ctx(dispute_outcome=DisputeOutcome.WON_BY_MERCHANT)
        refund = _ctx(dispute_outcome=DisputeOutcome.FULL_REFUND)
        assert score_buyer_satisfaction_risk(win).outcome_signal > score_buyer_satisfaction_risk(refund).outcome_signal

    def test_full_refund_has_zero_outcome_signal(self):
        ctx = _ctx(dispute_outcome=DisputeOutcome.FULL_REFUND)
        r = score_buyer_satisfaction_risk(ctx)
        assert r.outcome_signal == 0.0

    def test_partial_refund_has_nonzero_outcome_signal(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.PARTIAL_REFUND,
            partial_refund_amount=50.0,
            dispute_amount=150.0,
        )
        r = score_buyer_satisfaction_risk(ctx)
        assert r.outcome_signal > 0.0

    # ─── Churn probability ────────────────────────────────────────────────────

    def test_won_outcome_raises_churn(self):
        won = _ctx(dispute_outcome=DisputeOutcome.WON_BY_MERCHANT)
        refund = _ctx(dispute_outcome=DisputeOutcome.FULL_REFUND)
        assert score_buyer_satisfaction_risk(won).churn_probability > score_buyer_satisfaction_risk(refund).churn_probability

    def test_slow_resolution_raises_churn(self):
        fast = _ctx(days_to_resolution=2)
        slow = _ctx(days_to_resolution=25)
        assert score_buyer_satisfaction_risk(slow).churn_probability > score_buyer_satisfaction_risk(fast).churn_probability

    def test_non_empathetic_response_raises_churn(self):
        good = _ctx(was_tone_empathetic=True)
        bad = _ctx(was_tone_empathetic=False)
        assert score_buyer_satisfaction_risk(bad).churn_probability > score_buyer_satisfaction_risk(good).churn_probability

    def test_prior_disputes_raise_churn(self):
        no_prior = _ctx(buyer_prior_disputes_at_merchant=0)
        with_prior = _ctx(buyer_prior_disputes_at_merchant=2)
        assert score_buyer_satisfaction_risk(with_prior).churn_probability > score_buyer_satisfaction_risk(no_prior).churn_probability

    # ─── Bank chargeback probability ─────────────────────────────────────────

    def test_won_outcome_raises_chargeback(self):
        won = _ctx(dispute_outcome=DisputeOutcome.WON_BY_MERCHANT)
        r = score_buyer_satisfaction_risk(won)
        assert r.bank_chargeback_probability > 0.20

    def test_unauthorized_reason_boosts_chargeback(self):
        inr = _ctx(reason_code="INR", dispute_outcome=DisputeOutcome.WON_BY_MERCHANT)
        unauth = _ctx(reason_code="UNAUTHORIZED", dispute_outcome=DisputeOutcome.WON_BY_MERCHANT)
        r_inr = score_buyer_satisfaction_risk(inr)
        r_unauth = score_buyer_satisfaction_risk(unauth)
        assert r_unauth.bank_chargeback_probability > r_inr.bank_chargeback_probability

    def test_full_refund_has_low_chargeback_risk(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.FULL_REFUND,
            was_tone_empathetic=True,
            buyer_prior_disputes_at_merchant=0,
        )
        r = score_buyer_satisfaction_risk(ctx)
        assert r.bank_chargeback_probability < 0.30

    # ─── Risk tier ─────────────────────────────────────────────────────────────

    def test_critical_tier_on_won_plus_adversarial(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.WON_BY_MERCHANT,
            was_tone_empathetic=False,
            merchant_responded_before_escalation=False,
            days_to_resolution=25,
            buyer_prior_disputes_at_merchant=2,
            is_repeat_issue=True,
        )
        r = score_buyer_satisfaction_risk(ctx)
        assert r.risk_tier in (SatisfactionRiskTier.HIGH, SatisfactionRiskTier.CRITICAL)

    def test_low_risk_tier_on_smooth_refund(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.FULL_REFUND,
            days_to_resolution=3,
            was_tone_empathetic=True,
            days_until_refund_issued=0,
            buyer_prior_disputes_at_merchant=0,
        )
        r = score_buyer_satisfaction_risk(ctx)
        assert r.risk_tier in (SatisfactionRiskTier.LOW, SatisfactionRiskTier.MEDIUM)

    # ─── Retention actions ───────────────────────────────────────────────────

    def test_chargeback_monitoring_on_high_cb_risk(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.WON_BY_MERCHANT,
            was_tone_empathetic=False,
            merchant_responded_before_escalation=False,
            reason_code="UNAUTHORIZED",
        )
        r = score_buyer_satisfaction_risk(ctx)
        types = [a.action_type for a in r.retention_actions]
        assert RetentionActionType.MONITOR_FOR_BANK_CHARGEBACK in types

    def test_vip_buyer_gets_phone_call_action(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.WON_BY_MERCHANT,
            buyer_ltv_tier=BuyerLTVTier.VIP,
            buyer_total_orders_at_merchant=10,
        )
        r = score_buyer_satisfaction_risk(ctx)
        types = [a.action_type for a in r.retention_actions]
        assert RetentionActionType.PERSONAL_PHONE_CALL in types

    def test_won_dispute_suggests_discount(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.WON_BY_MERCHANT,
            was_tone_empathetic=True,
        )
        r = score_buyer_satisfaction_risk(ctx)
        types = [a.action_type for a in r.retention_actions]
        assert RetentionActionType.OFFER_DISCOUNT_NEXT_ORDER in types

    def test_max_three_retention_actions(self):
        r = score_buyer_satisfaction_risk(_ctx())
        assert len(r.retention_actions) <= 3

    # ─── Summary / insight ───────────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        ctx = _ctx(dispute_id="DISP_SUMMARY_XYZ")
        r = score_buyer_satisfaction_risk(ctx)
        assert "DISP_SUMMARY_XYZ" in r.summary

    def test_key_insight_populated(self):
        r = score_buyer_satisfaction_risk(_ctx())
        assert len(r.key_insight) > 20

    def test_critical_insight_mentions_urgency(self):
        ctx = _ctx(
            dispute_outcome=DisputeOutcome.WON_BY_MERCHANT,
            was_tone_empathetic=False,
            merchant_responded_before_escalation=False,
            buyer_prior_disputes_at_merchant=3,
            is_repeat_issue=True,
        )
        r = score_buyer_satisfaction_risk(ctx)
        if r.risk_tier == SatisfactionRiskTier.CRITICAL:
            assert "immediate" in r.key_insight.lower() or "critical" in r.key_insight.lower()

    def test_summary_contains_outcome(self):
        ctx = _ctx(dispute_outcome=DisputeOutcome.FULL_REFUND)
        r = score_buyer_satisfaction_risk(ctx)
        assert "FULL_REFUND" in r.summary
