"""
Wave 64 — Dispute Probability Forecaster
Tests: 30 total
"""

import pytest
from oracle.dispute_probability_forecaster import (
    OrderProfile,
    DisputeProbabilityTier,
    LikelyDisputeType,
    PreventionActionType,
    forecast_dispute_probability,
    batch_forecast,
)


def _order(**kwargs) -> OrderProfile:
    defaults = dict(
        order_id="O1",
        merchant_id="MERCH_X",
        buyer_id="BUYER_1",
        amount=100.0,
        is_digital=False,
        is_subscription=False,
        has_tracking=True,
        carrier="USPS",
        days_since_ship=3,
        delivery_status="IN_TRANSIT",
        has_delivery_confirmation=False,
        has_signature_confirmation=False,
        is_international=False,
        is_new_buyer=False,
        buyer_prior_disputes_at_merchant=0,
        is_peak_season=False,
        express_on_time=True,
    )
    defaults.update(kwargs)
    return OrderProfile(**defaults)


class TestDisputeProbabilityForecaster:

    # ─── Low risk baseline ─────────────────────────────────────────────────────

    def test_delivered_order_with_tracking_is_low_risk(self):
        o = _order(has_tracking=True, delivery_status="DELIVERED",
                   has_delivery_confirmation=True)
        r = forecast_dispute_probability(o)
        assert r.probability_tier == DisputeProbabilityTier.LOW

    def test_probability_between_0_and_1(self):
        o = _order()
        r = forecast_dispute_probability(o)
        assert 0.0 <= r.dispute_probability <= 1.0

    def test_low_risk_no_prevention_or_low_tier(self):
        o = _order(has_tracking=True, delivery_status="DELIVERED",
                   has_delivery_confirmation=True, amount=50.0)
        r = forecast_dispute_probability(o)
        assert r.probability_tier == DisputeProbabilityTier.LOW

    # ─── No tracking signal ───────────────────────────────────────────────────

    def test_no_tracking_raises_risk(self):
        o_with = _order(has_tracking=True)
        o_without = _order(has_tracking=False)
        r_with = forecast_dispute_probability(o_with)
        r_without = forecast_dispute_probability(o_without)
        assert r_without.dispute_probability > r_with.dispute_probability

    def test_no_tracking_physical_is_high_signal(self):
        o = _order(has_tracking=False, is_digital=False)
        r = forecast_dispute_probability(o)
        assert r.no_tracking_signal >= 0.25

    def test_no_tracking_digital_has_no_signal(self):
        o = _order(has_tracking=False, is_digital=True)
        r = forecast_dispute_probability(o)
        assert r.no_tracking_signal == 0.0

    def test_no_tracking_adds_add_tracking_action(self):
        o = _order(has_tracking=False, is_digital=False, amount=50.0)
        r = forecast_dispute_probability(o)
        types = [a.action_type for a in r.prevention_actions]
        assert PreventionActionType.ADD_TRACKING_TO_ORDER in types

    # ─── Delivery exception ────────────────────────────────────────────────────

    def test_delivery_exception_raises_risk(self):
        o_exc = _order(delivery_status="EXCEPTION")
        o_ok = _order(delivery_status="IN_TRANSIT")
        assert forecast_dispute_probability(o_exc).dispute_probability > forecast_dispute_probability(o_ok).dispute_probability

    def test_exception_adds_carrier_escalation_action(self):
        o = _order(delivery_status="EXCEPTION", carrier="FEDEX")
        r = forecast_dispute_probability(o)
        types = [a.action_type for a in r.prevention_actions]
        assert PreventionActionType.ESCALATE_WITH_CARRIER in types

    def test_exception_carrier_in_action_description(self):
        o = _order(delivery_status="EXCEPTION", carrier="UPS")
        r = forecast_dispute_probability(o)
        exc_actions = [a for a in r.prevention_actions
                       if a.action_type == PreventionActionType.ESCALATE_WITH_CARRIER]
        if exc_actions:
            assert "UPS" in exc_actions[0].description

    # ─── High value order ─────────────────────────────────────────────────────

    def test_high_value_without_confirmation_raises_risk(self):
        o_high = _order(amount=600.0, has_signature_confirmation=False)
        o_low = _order(amount=50.0)
        assert forecast_dispute_probability(o_high).high_value_no_sig_signal > 0

    def test_high_value_with_signature_no_signal(self):
        o = _order(amount=600.0, has_signature_confirmation=True)
        r = forecast_dispute_probability(o)
        assert r.high_value_no_sig_signal == 0.0

    # ─── Prior dispute history ────────────────────────────────────────────────

    def test_buyer_with_3_prior_disputes_is_high_signal(self):
        o = _order(buyer_prior_disputes_at_merchant=3)
        r = forecast_dispute_probability(o)
        assert r.prior_dispute_signal >= 0.20

    def test_buyer_no_disputes_is_zero_signal(self):
        o = _order(buyer_prior_disputes_at_merchant=0)
        r = forecast_dispute_probability(o)
        assert r.prior_dispute_signal == 0.0

    def test_one_prior_dispute_is_medium_signal(self):
        o = _order(buyer_prior_disputes_at_merchant=1)
        r = forecast_dispute_probability(o)
        assert 0.05 <= r.prior_dispute_signal <= 0.15

    # ─── International ─────────────────────────────────────────────────────────

    def test_international_without_confirmation_raises_risk(self):
        o = _order(is_international=True, has_delivery_confirmation=False)
        r = forecast_dispute_probability(o)
        assert r.international_signal > 0

    def test_international_with_confirmation_lower_signal(self):
        o_no = _order(is_international=True, has_delivery_confirmation=False)
        o_yes = _order(is_international=True, has_delivery_confirmation=True)
        assert forecast_dispute_probability(o_no).international_signal > forecast_dispute_probability(o_yes).international_signal

    # ─── Subscription ─────────────────────────────────────────────────────────

    def test_subscription_at_renewal_is_high_signal(self):
        o = _order(is_subscription=True, subscription_days_until_renewal=1)
        r = forecast_dispute_probability(o)
        assert r.subscription_signal >= 0.10

    def test_subscription_adds_notice_action(self):
        o = _order(is_subscription=True, subscription_days_until_renewal=2)
        r = forecast_dispute_probability(o)
        types = [a.action_type for a in r.prevention_actions]
        assert PreventionActionType.SEND_SUBSCRIPTION_NOTICE in types

    def test_subscription_likely_type_is_subscription(self):
        o = _order(is_subscription=True, subscription_days_until_renewal=0,
                   has_tracking=True, delivery_status="DELIVERED")
        r = forecast_dispute_probability(o)
        assert r.likely_dispute_type == LikelyDisputeType.SUBSCRIPTION

    # ─── Likely dispute type ──────────────────────────────────────────────────

    def test_no_tracking_leads_to_inr_type(self):
        o = _order(has_tracking=False, is_digital=False)
        r = forecast_dispute_probability(o)
        assert r.likely_dispute_type == LikelyDisputeType.INR

    def test_low_risk_order_no_primary_type(self):
        o = _order(has_tracking=True, delivery_status="DELIVERED",
                   has_delivery_confirmation=True, amount=30.0)
        r = forecast_dispute_probability(o)
        assert r.likely_dispute_type == LikelyDisputeType.NONE

    # ─── Batch forecast ───────────────────────────────────────────────────────

    def test_batch_sorted_highest_first(self):
        orders = [
            _order(order_id="HIGH", has_tracking=False, buyer_prior_disputes_at_merchant=3),
            _order(order_id="LOW", has_tracking=True, delivery_status="DELIVERED",
                   has_delivery_confirmation=True),
            _order(order_id="MED", is_international=True),
        ]
        results = batch_forecast(orders)
        probs = [r.dispute_probability for r in results]
        assert probs == sorted(probs, reverse=True)

    def test_batch_returns_all_orders(self):
        orders = [_order(order_id=f"O{i}") for i in range(5)]
        results = batch_forecast(orders)
        assert len(results) == 5

    def test_critical_tier_no_tracking_high_value_prior_disputes(self):
        o = _order(
            has_tracking=False, amount=500.0,
            buyer_prior_disputes_at_merchant=3,
            is_peak_season=True, is_international=True,
        )
        r = forecast_dispute_probability(o)
        assert r.probability_tier == DisputeProbabilityTier.CRITICAL

    # ─── Summary / key risk ───────────────────────────────────────────────────

    def test_summary_contains_order_id(self):
        o = _order(order_id="TEST_ORDER_XYZ")
        r = forecast_dispute_probability(o)
        assert "TEST_ORDER_XYZ" in r.summary

    def test_key_risk_factor_populated(self):
        o = _order(has_tracking=False)
        r = forecast_dispute_probability(o)
        assert len(r.key_risk_factor) > 5

    def test_prevention_actions_at_most_3(self):
        o = _order(
            has_tracking=False, delivery_status="EXCEPTION",
            is_subscription=True, subscription_days_until_renewal=1,
            amount=600.0,
        )
        r = forecast_dispute_probability(o)
        assert len(r.prevention_actions) <= 3

    def test_prevention_action_has_description(self):
        o = _order(has_tracking=False)
        r = forecast_dispute_probability(o)
        for action in r.prevention_actions:
            assert len(action.description) > 20
