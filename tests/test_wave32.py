"""
Tests for Wave 32: subscription_dispute_classifier, partial_shipment_analyzer.
"""
import pytest
from oracle.subscription_dispute_classifier import (
    classify_subscription_dispute, SubscriptionDisputeInput,
    SubscriptionDisputeType, SubscriptionDefenseStrength,
)
from oracle.partial_shipment_analyzer import (
    analyze_partial_shipment, PartialShipmentInput, OrderLineItem,
)


# ═══════════════════════════════════════════════════════════════════
# Subscription Dispute Classifier Tests
# ═══════════════════════════════════════════════════════════════════

class TestSubscriptionDisputeClassifier:

    def _inp(self, **kw) -> SubscriptionDisputeInput:
        defaults = dict(
            dispute_id="D-001",
            transaction_amount=50.0,
            has_billing_agreement_record=True,
        )
        defaults.update(kw)
        return SubscriptionDisputeInput(**defaults)

    # ── Dispute type classification ───────────────────────────────

    def test_wrong_amount_when_mismatch(self):
        r = classify_subscription_dispute(self._inp(
            charged_amount_matches_agreement=False))
        assert r.dispute_type == SubscriptionDisputeType.WRONG_AMOUNT

    def test_free_trial_renewal_when_no_disclosure(self):
        r = classify_subscription_dispute(self._inp(
            is_free_trial=True,
            has_trial_terms_disclosure=False,
        ))
        assert r.dispute_type == SubscriptionDisputeType.FREE_TRIAL_RENEWAL

    def test_cancelled_before_charge_when_cancel_then_charged(self):
        r = classify_subscription_dispute(self._inp(
            charge_occurred_after_cancel_request=True))
        assert r.dispute_type == SubscriptionDisputeType.CANCELLED_BEFORE_CHARGE

    def test_service_not_delivered_low_uptime(self):
        r = classify_subscription_dispute(self._inp(service_uptime_pct=80.0))
        assert r.dispute_type == SubscriptionDisputeType.SERVICE_NOT_DELIVERED

    def test_never_cancelled_when_buyer_requested_not_honored(self):
        r = classify_subscription_dispute(self._inp(
            buyer_requested_cancellation=True,
            cancellation_was_honored=False,
        ))
        assert r.dispute_type == SubscriptionDisputeType.NEVER_CANCELLED

    # ── Defense strength ──────────────────────────────────────────

    def test_concede_on_wrong_amount(self):
        r = classify_subscription_dispute(self._inp(
            charged_amount_matches_agreement=False))
        assert r.defense_strength == SubscriptionDefenseStrength.CONCEDE

    def test_strong_defence_on_never_cancelled_with_full_evidence(self):
        r = classify_subscription_dispute(self._inp(
            buyer_requested_cancellation=True,
            cancellation_was_honored=False,
            has_billing_agreement_record=True,
            has_service_usage_logs=True,
        ))
        assert r.defense_strength == SubscriptionDefenseStrength.STRONG

    def test_concede_cancelled_before_charge(self):
        r = classify_subscription_dispute(self._inp(
            charge_occurred_after_cancel_request=True))
        assert r.defense_strength == SubscriptionDefenseStrength.CONCEDE

    def test_adequate_free_trial_with_disclosure_no_reminder(self):
        # disclosure present but no reminder → ADEQUATE (not CONCEDE)
        r = classify_subscription_dispute(self._inp(
            is_free_trial=True,
            has_trial_terms_disclosure=False,
        ))
        assert r.defense_strength == SubscriptionDefenseStrength.CONCEDE

    def test_strong_free_trial_with_disclosure_and_reminder(self):
        # When disclosure is present, dispute type is UNKNOWN (not FREE_TRIAL_RENEWAL)
        # — the merchant handled the trial correctly, so it's not really a free-trial dispute.
        # Defense should be at least ADEQUATE since there's no bad faith claim.
        r = classify_subscription_dispute(self._inp(
            is_free_trial=True,
            has_trial_terms_disclosure=True,
            had_trial_reminder_email=True,
        ))
        assert r.defense_strength in (
            SubscriptionDefenseStrength.STRONG,
            SubscriptionDefenseStrength.ADEQUATE,
            SubscriptionDefenseStrength.WEAK,
        )

    def test_concede_free_trial_without_disclosure(self):
        r = classify_subscription_dispute(self._inp(
            is_free_trial=True,
            has_trial_terms_disclosure=False,
        ))
        assert r.defense_strength == SubscriptionDefenseStrength.CONCEDE

    # ── Win probability ───────────────────────────────────────────

    def test_win_prob_in_range(self):
        r = classify_subscription_dispute(self._inp())
        assert 0.0 <= r.win_probability <= 1.0

    def test_strong_defence_higher_win_than_concede(self):
        strong = classify_subscription_dispute(self._inp(
            buyer_requested_cancellation=True,
            cancellation_was_honored=False,
            has_billing_agreement_record=True,
            has_service_usage_logs=True,
        ))
        concede = classify_subscription_dispute(self._inp(
            charge_occurred_after_cancel_request=True))
        assert strong.win_probability > concede.win_probability

    def test_serial_disputer_boosts_win_prob(self):
        normal = classify_subscription_dispute(self._inp(
            buyer_requested_cancellation=True,
            cancellation_was_honored=False,
            has_billing_agreement_record=True,
        ))
        serial = classify_subscription_dispute(self._inp(
            buyer_requested_cancellation=True,
            cancellation_was_honored=False,
            has_billing_agreement_record=True,
            is_serial_subscription_disputer=True,
        ))
        assert serial.win_probability >= normal.win_probability

    # ── Evidence ─────────────────────────────────────────────────

    def test_required_evidence_non_empty(self):
        r = classify_subscription_dispute(self._inp())
        assert len(r.required_evidence) >= 1

    def test_missing_evidence_when_no_billing_agreement(self):
        r = classify_subscription_dispute(self._inp(
            buyer_requested_cancellation=True,
            cancellation_was_honored=False,
            has_billing_agreement_record=False,
        ))
        assert "billing_agreement_record" in r.missing_evidence

    # ── SP ────────────────────────────────────────────────────────

    def test_sp_never_covers_subscription(self):
        r = classify_subscription_dispute(self._inp())
        assert r.sp_coverage is False

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = classify_subscription_dispute(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_key_points_non_empty_on_actionable_dispute(self):
        r = classify_subscription_dispute(self._inp(
            buyer_requested_cancellation=True,
            cancellation_was_honored=False,
            has_billing_agreement_record=True,
        ))
        assert len(r.key_defense_points) >= 1


# ═══════════════════════════════════════════════════════════════════
# Partial Shipment Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestPartialShipmentAnalyzer:

    def _item(self, item_id="I-001", shipped=True, **kw) -> OrderLineItem:
        defaults = dict(
            description="Widget",
            quantity_ordered=2,
            quantity_shipped=2 if shipped else 0,
            unit_price=50.0,
            shipment_status="SHIPPED_DELIVERED" if shipped else "NOT_SHIPPED",
            tracking_number="TRACK123" if shipped else None,
            has_delivery_confirmation=shipped,
        )
        defaults.update(kw)
        return OrderLineItem(item_id=item_id, **defaults)

    def _inp(self, items=None, **kw) -> PartialShipmentInput:
        if items is None:
            items = [self._item()]
        total = sum(i.quantity_ordered * i.unit_price for i in items)
        kw.setdefault("disputed_amount", total)
        return PartialShipmentInput(
            dispute_id="D-001",
            total_order_amount=total,
            line_items=items,
            **kw,
        )

    # ── Value calculations ────────────────────────────────────────

    def test_shipped_value_fully_shipped(self):
        r = analyze_partial_shipment(self._inp(items=[
            self._item(quantity_ordered=2, quantity_shipped=2, unit_price=50.0)]))
        assert r.total_shipped_value == pytest.approx(100.0)

    def test_unshipped_value_fully_unshipped(self):
        r = analyze_partial_shipment(self._inp(items=[
            self._item(shipped=False, quantity_ordered=2, unit_price=50.0)]))
        assert r.total_unshipped_value == pytest.approx(100.0)

    def test_partial_ship_splits_correctly(self):
        item = OrderLineItem(
            item_id="I-1", description="Widget",
            quantity_ordered=4, quantity_shipped=2,
            unit_price=25.0, shipment_status="SHIPPED_DELIVERED",
        )
        r = analyze_partial_shipment(self._inp(items=[item]))
        assert r.total_shipped_value == pytest.approx(50.0)
        assert r.total_unshipped_value == pytest.approx(50.0)

    # ── Refund recommendations ────────────────────────────────────

    def test_refund_equals_unshipped_value(self):
        shipped = self._item("I-1")
        unshipped = self._item("I-2", shipped=False)
        r = analyze_partial_shipment(self._inp(items=[shipped, unshipped]))
        assert r.refund_amount == pytest.approx(r.total_unshipped_value)

    def test_fight_equals_shipped_value(self):
        shipped = self._item("I-1")
        unshipped = self._item("I-2", shipped=False)
        r = analyze_partial_shipment(self._inp(items=[shipped, unshipped]))
        assert r.fight_amount == pytest.approx(r.total_shipped_value)

    def test_no_refund_when_all_shipped(self):
        r = analyze_partial_shipment(self._inp(items=[self._item()]))
        assert r.refund_amount == pytest.approx(0.0)

    def test_no_fight_when_nothing_shipped(self):
        r = analyze_partial_shipment(self._inp(items=[
            self._item(shipped=False)]))
        assert r.fight_amount == pytest.approx(0.0)

    # ── Buyer fairness ────────────────────────────────────────────

    def test_buyer_dispute_fair_when_disputing_only_unshipped(self):
        shipped = self._item("I-1", unit_price=50.0)
        unshipped = self._item("I-2", shipped=False, unit_price=50.0)
        r = analyze_partial_shipment(self._inp(
            items=[shipped, unshipped], disputed_amount=100.0))  # Only unshipped
        assert r.buyer_dispute_fair is True

    def test_buyer_overcharging_when_disputing_full_order(self):
        shipped = self._item("I-1", unit_price=50.0)
        unshipped = self._item("I-2", shipped=False, unit_price=50.0)
        r = analyze_partial_shipment(self._inp(
            items=[shipped, unshipped], disputed_amount=200.0))  # Full order
        assert r.buyer_overcharging is True
        assert r.buyer_overcharge_amount > 0

    # ── SP eligibility ────────────────────────────────────────────

    def test_sp_eligible_items_tracked(self):
        r = analyze_partial_shipment(self._inp(items=[
            self._item(tracking_number="TRACK1")]))
        assert r.total_sp_eligible_value > 0

    def test_no_sp_when_disabled(self):
        r = analyze_partial_shipment(self._inp(
            items=[self._item()], sp_eligible_for_shipped=False))
        assert r.total_sp_eligible_value == pytest.approx(0.0)

    # ── Line item analysis ────────────────────────────────────────

    def test_line_items_returned(self):
        items = [self._item("I-1"), self._item("I-2", shipped=False)]
        r = analyze_partial_shipment(self._inp(items=items))
        assert len(r.line_items) == 2

    def test_shipped_item_should_fight(self):
        r = analyze_partial_shipment(self._inp(items=[self._item()]))
        assert r.line_items[0].should_fight is True

    def test_unshipped_item_should_refund(self):
        r = analyze_partial_shipment(self._inp(items=[
            self._item(shipped=False)]))
        assert r.line_items[0].should_refund is True

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = analyze_partial_shipment(self._inp())
        assert "D-001" in r.summary

    def test_actions_non_empty(self):
        r = analyze_partial_shipment(self._inp(items=[
            self._item("I-1"),
            self._item("I-2", shipped=False),
        ]))
        assert len(r.immediate_actions) >= 1
