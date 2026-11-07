"""
Wave 62 — Dispute Velocity Monitor
Tests: 24 total
"""

import pytest
from oracle.dispute_velocity_monitor import (
    DisputeVelocityRecord,
    VelocityInput,
    VelocitySpikeType,
    SpikeUrgency,
    monitor_dispute_velocity,
)


def _rec(reason_code="INR", amount=150.0, hour=10, sku="SKU_A", carrier="USPS") -> DisputeVelocityRecord:
    return DisputeVelocityRecord(
        dispute_id=f"D_{reason_code}_{hour}",
        reason_code=reason_code,
        amount=amount,
        filed_hour=hour,
        sku=sku,
        carrier=carrier,
    )


def _run(records, baseline=5.0, window=24) -> "VelocityReport":
    return monitor_dispute_velocity(VelocityInput(
        merchant_id="TEST_M",
        window_hours=window,
        current_window_disputes=records,
        baseline_daily_disputes=baseline,
    ))


class TestDisputeVelocityMonitor:

    # ─── Normal state ─────────────────────────────────────────────────────────

    def test_empty_window_is_normal(self):
        r = _run([])
        assert r.spike_type == VelocitySpikeType.NORMAL
        assert r.is_spike is False
        assert r.urgency == SpikeUrgency.NORMAL

    def test_below_2x_baseline_is_normal(self):
        records = [_rec() for _ in range(8)]  # baseline=5, so 1.6× = normal
        r = _run(records, baseline=5.0)
        assert r.spike_type == VelocitySpikeType.NORMAL
        assert r.velocity_multiplier < 2.0

    def test_velocity_multiplier_calculation(self):
        records = [_rec() for _ in range(20)]  # 20 disputes, baseline=5/day
        r = _run(records, baseline=5.0, window=24)
        assert abs(r.velocity_multiplier - 4.0) < 0.1

    # ─── Fraud wave detection ─────────────────────────────────────────────────

    def test_fraud_wave_spike_detected(self):
        # 50%+ UNAUTHORIZED clustered in one hour
        records = [_rec("UNAUTHORIZED", hour=3) for _ in range(12)]
        records += [_rec("INR", hour=10) for _ in range(3)]
        r = _run(records, baseline=3.0)
        assert r.spike_type == VelocitySpikeType.FRAUD_WAVE_SPIKE

    def test_fraud_wave_is_critical_urgency(self):
        records = [_rec("UNAUTHORIZED", hour=3) for _ in range(30)]
        r = _run(records, baseline=3.0)
        assert r.urgency == SpikeUrgency.CRITICAL

    def test_fraud_wave_actions_mention_paypal(self):
        records = [_rec("UNAUTHORIZED", hour=3) for _ in range(12)]
        records += [_rec("INR", hour=10) for _ in range(2)]
        r = _run(records, baseline=3.0)
        owners = [a.owner for a in r.immediate_actions]
        assert "PAYPAL" in owners

    # ─── Product defect detection ─────────────────────────────────────────────

    def test_product_defect_spike_detected(self):
        records = [_rec("SNAD", sku="SKU_BAD") for _ in range(15)]
        records += [_rec("INR") for _ in range(3)]
        r = _run(records, baseline=3.0)
        assert r.spike_type == VelocitySpikeType.PRODUCT_DEFECT_SPIKE

    def test_product_defect_top_sku_identified(self):
        records = [_rec("SNAD", sku="DEFECT_SKU") for _ in range(10)]
        records += [_rec("SNAD", sku="ANOTHER_SKU") for _ in range(2)]
        records += [_rec("INR") for _ in range(2)]
        r = _run(records, baseline=3.0)
        assert r.top_sku == "DEFECT_SKU"
        assert r.top_sku_dispute_count == 10

    def test_product_defect_insight_mentions_sku(self):
        records = [_rec("SNAD", sku="SKU_RECALL") for _ in range(12)]
        records += [_rec("INR") for _ in range(2)]
        r = _run(records, baseline=3.0)
        assert "SNAD" in r.key_insight or "defect" in r.key_insight.lower()

    # ─── Fulfillment failure detection ───────────────────────────────────────

    def test_fulfillment_failure_spike_detected(self):
        records = [_rec("INR", carrier="USPS") for _ in range(18)]
        records += [_rec("SNAD") for _ in range(3)]
        r = _run(records, baseline=3.0)
        assert r.spike_type == VelocitySpikeType.FULFILLMENT_FAILURE_SPIKE

    def test_fulfillment_failure_top_carrier_identified(self):
        records = [_rec("INR", carrier="FEDEX") for _ in range(15)]
        r = _run(records, baseline=3.0)
        assert r.top_carrier == "FEDEX"

    def test_fulfillment_failure_actions_mention_carrier(self):
        records = [_rec("INR", carrier="UPS") for _ in range(15)]
        r = _run(records, baseline=3.0)
        action_texts = " ".join(a.action for a in r.immediate_actions)
        assert "carrier" in action_texts.lower() or "UPS" in action_texts

    # ─── Subscription churn detection ────────────────────────────────────────

    def test_subscription_churn_spike_detected(self):
        records = [_rec("SUBSCRIPTION", carrier="") for _ in range(14)]
        records += [_rec("INR") for _ in range(2)]
        r = _run(records, baseline=3.0)
        assert r.spike_type == VelocitySpikeType.SUBSCRIPTION_CHURN_SPIKE

    # ─── General spike ────────────────────────────────────────────────────────

    def test_general_spike_on_mixed_codes(self):
        # 3× baseline but no dominant code at threshold
        records = (
            [_rec("INR") for _ in range(5)] +
            [_rec("SNAD") for _ in range(5)] +
            [_rec("UNAUTHORIZED") for _ in range(5)]
        )
        r = _run(records, baseline=3.0)
        assert r.is_spike is True

    # ─── Financial metrics ───────────────────────────────────────────────────

    def test_total_exposure_calculated(self):
        records = [_rec(amount=100.0) for _ in range(10)]
        r = _run(records, baseline=3.0)
        assert r.total_exposure == 1000.0

    def test_high_value_dispute_count(self):
        records = [_rec(amount=300.0) for _ in range(5)]
        records += [_rec(amount=50.0) for _ in range(5)]
        r = _run(records, baseline=3.0)
        assert r.high_value_dispute_count == 5

    def test_avg_dispute_amount(self):
        records = [_rec(amount=200.0) for _ in range(4)]
        r = _run(records, baseline=3.0)
        assert r.avg_dispute_amount == 200.0

    # ─── Counts ──────────────────────────────────────────────────────────────

    def test_reason_code_counts_correct(self):
        records = (
            [_rec("INR") for _ in range(3)] +
            [_rec("SNAD") for _ in range(5)] +
            [_rec("UNAUTHORIZED") for _ in range(2)]
        )
        r = _run(records, baseline=2.0)
        assert r.inr_count == 3
        assert r.snad_count == 5
        assert r.unauthorized_count == 2
        assert r.subscription_count == 0

    # ─── Summary / insight quality ────────────────────────────────────────────

    def test_summary_is_populated(self):
        r = _run([_rec() for _ in range(15)], baseline=3.0)
        assert len(r.summary) > 20
        assert "TEST_M" in r.summary

    def test_paypal_notification_on_spike(self):
        records = [_rec("UNAUTHORIZED", hour=5) for _ in range(15)]
        r = _run(records, baseline=3.0)
        assert len(r.paypal_notification) > 20
        assert "TEST_M" in r.paypal_notification

    def test_no_paypal_notification_when_normal(self):
        records = [_rec() for _ in range(4)]
        r = _run(records, baseline=5.0)
        assert r.paypal_notification == ""

    def test_immediate_actions_not_empty_on_spike(self):
        records = [_rec("UNAUTHORIZED", hour=3) for _ in range(12)]
        records += [_rec("INR", hour=10) for _ in range(2)]
        r = _run(records, baseline=3.0)
        assert len(r.immediate_actions) >= 2
        for action in r.immediate_actions:
            assert len(action.action) > 10

    def test_actions_have_valid_owners(self):
        records = [_rec("SNAD") for _ in range(12)]
        r = _run(records, baseline=3.0)
        for action in r.immediate_actions:
            assert action.owner in ("MERCHANT", "PAYPAL", "BOTH")
