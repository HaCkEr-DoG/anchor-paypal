"""
Wave 63 — Merchant Health Advisor
Tests: 28 total
"""

import pytest
from oracle.merchant_health_advisor import (
    HistoricalDispute,
    MerchantHealthInput,
    HealthTier,
    DisputeRootCause,
    RecommendationPriority,
    analyze_merchant_health,
)


def _d(
    dispute_id="D1",
    reason_code="INR",
    amount=150.0,
    outcome="LOST",
    days_to_respond=3,
    had_tracking=True,
    had_invoice=True,
    had_buyer_communication=False,
    carrier="USPS",
    sku="SKU_A",
) -> HistoricalDispute:
    return HistoricalDispute(
        dispute_id=dispute_id,
        reason_code=reason_code,
        amount=amount,
        outcome=outcome,
        days_to_respond=days_to_respond,
        had_tracking=had_tracking,
        had_invoice=had_invoice,
        had_buyer_communication=had_buyer_communication,
        carrier=carrier,
        sku=sku,
    )


def _run(disputes, total_orders=1000):
    return analyze_merchant_health(MerchantHealthInput(
        merchant_id="TEST_M",
        disputes=disputes,
        total_orders_in_period=total_orders,
    ))


class TestMerchantHealthAdvisor:

    # ─── Empty / healthy baseline ─────────────────────────────────────────────

    def test_no_disputes_is_excellent_or_good(self):
        r = _run([])
        assert r.health_tier in (HealthTier.EXCELLENT, HealthTier.GOOD)
        assert r.current_dispute_rate == 0.0

    def test_low_dispute_rate_good_tier(self):
        # 3 disputes / 1000 orders = 0.3% < 0.75% threshold
        disputes = [_d(f"D{i}", outcome="WON") for i in range(3)]
        r = _run(disputes)
        assert r.health_tier in (HealthTier.EXCELLENT, HealthTier.GOOD)

    def test_dispute_rate_calculation(self):
        disputes = [_d(f"D{i}") for i in range(10)]
        r = _run(disputes, total_orders=1000)
        assert abs(r.current_dispute_rate - 0.01) < 0.0001

    def test_win_rate_calculation(self):
        disputes = (
            [_d(f"W{i}", outcome="WON") for i in range(6)] +
            [_d(f"L{i}", outcome="LOST") for i in range(4)]
        )
        r = _run(disputes)
        assert abs(r.win_rate - 0.6) < 0.01

    # ─── Health tiers ─────────────────────────────────────────────────────────

    def test_above_threshold_is_poor_or_critical(self):
        # 1% dispute rate vs 0.75% threshold
        disputes = [_d(f"D{i}", outcome="LOST", days_to_respond=8) for i in range(10)]
        r = _run(disputes, total_orders=1000)
        assert r.health_tier in (HealthTier.POOR, HealthTier.CRITICAL)

    def test_critical_tier_very_high_rate(self):
        disputes = [_d(f"D{i}", outcome="LOST", days_to_respond=10) for i in range(50)]
        r = _run(disputes, total_orders=500)
        assert r.health_tier == HealthTier.CRITICAL
        assert r.health_score < 25

    def test_health_score_range(self):
        disputes = [_d(f"D{i}", outcome="WON", days_to_respond=1) for i in range(2)]
        r = _run(disputes, total_orders=1000)
        assert 0 <= r.health_score <= 100

    def test_dispute_rate_vs_threshold_above_1_when_over(self):
        disputes = [_d(f"D{i}", outcome="LOST") for i in range(10)]
        r = _run(disputes, total_orders=1000)
        # 1% rate / 0.75% threshold > 1.0
        assert r.dispute_rate_vs_threshold > 1.0

    # ─── Root cause detection ────────────────────────────────────────────────

    def test_fulfillment_delays_root_cause(self):
        disputes = [_d(f"D{i}", reason_code="INR") for i in range(8)]
        disputes += [_d(f"S{i}", reason_code="SNAD") for i in range(2)]
        r = _run(disputes, total_orders=200)
        assert DisputeRootCause.FULFILLMENT_DELAYS in r.root_causes

    def test_product_quality_root_cause(self):
        disputes = [_d(f"D{i}", reason_code="SNAD") for i in range(8)]
        disputes += [_d(f"I{i}", reason_code="INR") for i in range(2)]
        r = _run(disputes, total_orders=200)
        assert DisputeRootCause.PRODUCT_QUALITY in r.root_causes

    def test_fraud_exposure_root_cause(self):
        disputes = [_d(f"D{i}", reason_code="UNAUTHORIZED") for i in range(5)]
        disputes += [_d(f"I{i}", reason_code="INR") for i in range(5)]
        r = _run(disputes, total_orders=200)
        assert DisputeRootCause.FRAUD_EXPOSURE in r.root_causes

    def test_subscription_confusion_root_cause(self):
        disputes = [_d(f"D{i}", reason_code="SUBSCRIPTION") for i in range(5)]
        disputes += [_d(f"I{i}", reason_code="INR") for i in range(3)]
        r = _run(disputes, total_orders=100)
        assert DisputeRootCause.SUBSCRIPTION_CONFUSION in r.root_causes

    def test_response_latency_root_cause(self):
        # Very slow response = response latency cause
        disputes = [_d(f"D{i}", days_to_respond=9) for i in range(5)]
        r = _run(disputes, total_orders=100)
        assert DisputeRootCause.RESPONSE_LATENCY in r.root_causes

    def test_evidence_weakness_root_cause(self):
        # Very low win rate → evidence weakness
        disputes = [_d(f"D{i}", outcome="LOST", days_to_respond=2) for i in range(10)]
        r = _run(disputes, total_orders=200)
        assert DisputeRootCause.EVIDENCE_WEAKNESS in r.root_causes

    def test_max_three_root_causes(self):
        r = _run([_d(f"D{i}") for i in range(10)], total_orders=200)
        assert len(r.root_causes) <= 3

    def test_at_least_one_root_cause(self):
        r = _run([_d(f"D{i}", outcome="LOST") for i in range(5)], total_orders=100)
        assert len(r.root_causes) >= 1

    # ─── Recommendations ─────────────────────────────────────────────────────

    def test_recommendations_not_empty_when_disputes_exist(self):
        disputes = [_d(f"D{i}", outcome="LOST") for i in range(5)]
        r = _run(disputes, total_orders=100)
        assert len(r.recommendations) >= 1

    def test_recommendation_has_expected_impact(self):
        disputes = [_d(f"D{i}", reason_code="INR") for i in range(8)]
        r = _run(disputes, total_orders=200)
        for rec in r.recommendations:
            assert len(rec.expected_impact) > 10

    def test_recommendation_has_valid_effort(self):
        disputes = [_d(f"D{i}", outcome="LOST") for i in range(5)]
        r = _run(disputes, total_orders=100)
        for rec in r.recommendations:
            assert rec.effort in ("LOW", "MEDIUM", "HIGH")

    def test_urgent_priority_for_poor_tier(self):
        disputes = [_d(f"D{i}", outcome="LOST", days_to_respond=8) for i in range(50)]
        r = _run(disputes, total_orders=1000)
        priorities = [rec.priority for rec in r.recommendations]
        assert RecommendationPriority.URGENT in priorities

    # ─── Projections ─────────────────────────────────────────────────────────

    def test_projected_rate_lower_than_current(self):
        disputes = [_d(f"D{i}", reason_code="INR") for i in range(10)]
        r = _run(disputes, total_orders=500)
        if r.current_dispute_rate > 0:
            assert r.projected_dispute_rate_if_actions_taken <= r.current_dispute_rate

    def test_projected_score_higher_than_current(self):
        disputes = [_d(f"D{i}", outcome="LOST", days_to_respond=8) for i in range(20)]
        r = _run(disputes, total_orders=500)
        assert r.projected_health_score_if_actions_taken >= r.health_score

    # ─── PayPal signals ───────────────────────────────────────────────────────

    def test_paypal_risk_signal_populated(self):
        r = _run([_d(f"D{i}") for i in range(5)], total_orders=100)
        assert len(r.paypal_risk_signal) > 30
        assert "TEST_M" in r.paypal_risk_signal

    def test_paypal_opportunity_signal_populated(self):
        r = _run([_d(f"D{i}", outcome="WON") for i in range(2)], total_orders=1000)
        assert len(r.paypal_opportunity_signal) > 10

    def test_critical_tier_risk_signal_mentions_action(self):
        disputes = [_d(f"D{i}", outcome="LOST", days_to_respond=10) for i in range(50)]
        r = _run(disputes, total_orders=500)
        assert "ACTION" in r.paypal_risk_signal or "threshold" in r.paypal_risk_signal.lower()

    # ─── Summary / insight quality ────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = _run([_d("D1")], total_orders=100)
        assert "TEST_M" in r.summary

    def test_key_insight_populated(self):
        r = _run([_d(f"D{i}") for i in range(5)], total_orders=100)
        assert len(r.key_insight) > 20

    def test_reason_code_percentages_sum_to_1(self):
        disputes = (
            [_d(f"I{i}", reason_code="INR") for i in range(4)] +
            [_d(f"S{i}", reason_code="SNAD") for i in range(3)] +
            [_d(f"U{i}", reason_code="UNAUTHORIZED") for i in range(2)] +
            [_d(f"B{i}", reason_code="SUBSCRIPTION") for i in range(1)]
        )
        r = _run(disputes, total_orders=500)
        total = r.inr_pct + r.snad_pct + r.unauthorized_pct + r.subscription_pct
        assert abs(total - 1.0) < 0.01
