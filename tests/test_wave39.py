"""
Tests for Wave 39: refund_policy_advisor, order_fulfillment_tracker.
"""
import pytest
from oracle.refund_policy_advisor import (
    advise_refund_policy, RefundPolicyInput, PolicyDetails,
    PolicyType, PolicyCompliance, PolicyDefensibility,
)
from oracle.order_fulfillment_tracker import (
    assess_fulfillment, FulfillmentInput, FulfillmentRecord,
    TrackingStatus, FulfillmentStrength, DefenseReadiness,
)


BASE_TS = 1_700_000_000
DAY = 86400


# ═══════════════════════════════════════════════════════════════════
# Refund Policy Advisor Tests
# ═══════════════════════════════════════════════════════════════════

class TestRefundPolicyAdvisor:

    def _policy(self, **kw) -> PolicyDetails:
        defaults = dict(
            policy_type=PolicyType.RETURN,
            is_on_checkout_page=True,
            is_on_product_page=True,
            is_in_order_confirmation=True,
            requires_active_acceptance=True,
            return_window_days=30,
            policy_was_in_place_at_order=True,
            days_policy_active=365,
        )
        defaults.update(kw)
        return PolicyDetails(**defaults)

    def _inp(self, policies=None, **kw) -> RefundPolicyInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            order_amount=100.0,
            days_since_purchase=10,
            policy_accepted_at_checkout=True,
        )
        defaults.update(kw)
        if policies is None:
            policies = [self._policy()]
        return RefundPolicyInput(policies=policies, **defaults)

    # ── No policy ─────────────────────────────────────────────────

    def test_no_policy_non_compliant(self):
        r = advise_refund_policy(self._inp(policies=[]))
        assert r.compliance == PolicyCompliance.NON_COMPLIANT

    def test_no_policy_unusable(self):
        r = advise_refund_policy(self._inp(policies=[]))
        assert r.defensibility == PolicyDefensibility.UNUSABLE

    def test_no_policy_evidence_not_usable(self):
        r = advise_refund_policy(self._inp(policies=[]))
        assert r.policy_evidence_usable is False

    # ── Compliant policy ──────────────────────────────────────────

    def test_full_disclosure_compliant(self):
        r = advise_refund_policy(self._inp())
        assert r.compliance in (PolicyCompliance.COMPLIANT, PolicyCompliance.MINOR_GAPS)

    def test_compliant_with_acceptance_strong(self):
        r = advise_refund_policy(self._inp())
        assert r.defensibility in (PolicyDefensibility.STRONG, PolicyDefensibility.ADEQUATE)

    def test_strong_policy_positive_win_impact(self):
        r = advise_refund_policy(self._inp())
        assert r.win_rate_impact >= 0.0

    # ── Checkout disclosure missing ───────────────────────────────

    def test_missing_checkout_disclosure_major_gap(self):
        p = self._policy(is_on_checkout_page=False, requires_active_acceptance=False)
        r = advise_refund_policy(self._inp(policies=[p]))
        assert r.compliance in (PolicyCompliance.MAJOR_GAPS, PolicyCompliance.NON_COMPLIANT)

    def test_missing_checkout_generates_gap(self):
        p = self._policy(is_on_checkout_page=False, requires_active_acceptance=False)
        r = advise_refund_policy(self._inp(policies=[p]))
        assert len(r.gaps) >= 1

    def test_weak_policy_negative_impact(self):
        p = self._policy(is_on_checkout_page=False, requires_active_acceptance=False)
        r = advise_refund_policy(self._inp(policies=[p], policy_accepted_at_checkout=False))
        assert r.win_rate_impact < 0

    # ── Policy not in place at order ──────────────────────────────

    def test_policy_not_in_place_at_order_high_gap(self):
        p = self._policy(policy_was_in_place_at_order=False)
        r = advise_refund_policy(self._inp(policies=[p]))
        high_gaps = [g for g in r.gaps if g.severity == "HIGH"]
        assert len(high_gaps) >= 1

    # ── Subscription policy ───────────────────────────────────────

    def test_phone_only_cancel_generates_gap(self):
        p = self._policy(
            policy_type=PolicyType.SUBSCRIPTION_CANCEL,
            cancel_requires_phone=True,
            cancel_online_available=False,
        )
        r = advise_refund_policy(self._inp(
            policies=[p], is_subscription=True, claim_type="SUBSCRIPTION"))
        assert any("phone" in g.gap.lower() or "cancel" in g.gap.lower()
                   for g in r.gaps)

    def test_online_cancel_no_subscription_gap(self):
        p = self._policy(
            policy_type=PolicyType.SUBSCRIPTION_CANCEL,
            cancel_online_available=True,
            cancel_requires_phone=False,
        )
        r = advise_refund_policy(self._inp(
            policies=[p], is_subscription=True))
        cancel_gaps = [g for g in r.gaps if "cancel" in g.gap.lower()]
        assert len(cancel_gaps) == 0

    # ── SNAD policy ───────────────────────────────────────────────

    def test_snad_expired_window_no_negative_gap(self):
        p = self._policy(return_window_days=14)
        r = advise_refund_policy(self._inp(
            policies=[p], claim_type="SNAD", days_since_purchase=20))
        # Expired window is LOW severity (strengthens our defense)
        high_gaps = [g for g in r.gaps if g.severity == "HIGH"]
        assert len(high_gaps) == 0

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = advise_refund_policy(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_win_rate_impact_bounded(self):
        r = advise_refund_policy(self._inp())
        assert -0.25 <= r.win_rate_impact <= 0.15

    def test_recommendations_non_empty(self):
        r = advise_refund_policy(self._inp())
        assert len(r.recommendations) >= 1


# ═══════════════════════════════════════════════════════════════════
# Order Fulfillment Tracker Tests
# ═══════════════════════════════════════════════════════════════════

class TestOrderFulfillmentTracker:

    def _ship(self, ship_id="S-001", carrier="FEDEX",
              tracking="123456789",
              status=TrackingStatus.DELIVERED,
              has_del=True, has_sig=False,
              shipped_offset=1, **kw) -> FulfillmentRecord:
        return FulfillmentRecord(
            shipment_id=ship_id,
            carrier=carrier,
            tracking_number=tracking,
            shipped_date_epoch=BASE_TS + shipped_offset * DAY,
            promised_delivery_epoch=BASE_TS + 7 * DAY,
            tracking_status=status,
            has_delivery_confirmation=has_del,
            has_signature_confirmation=has_sig,
            **kw,
        )

    def _inp(self, shipments=None, amount=100.0, **kw) -> FulfillmentInput:
        defaults = dict(
            dispute_id="D-001",
            order_amount=amount,
            order_date_epoch=BASE_TS,
            dispute_date_epoch=BASE_TS + 10 * DAY,
            stated_handling_days=2,
            stated_transit_days=7,
            network="PAYPAL",
        )
        defaults.update(kw)
        if shipments is None:
            shipments = [self._ship()]
        return FulfillmentInput(shipments=shipments, **defaults)

    # ── No shipments ──────────────────────────────────────────────

    def test_no_shipments_missing_strength(self):
        r = assess_fulfillment(self._inp(shipments=[]))
        assert r.overall_strength == FulfillmentStrength.MISSING

    def test_no_shipments_not_ready(self):
        r = assess_fulfillment(self._inp(shipments=[]))
        assert r.defense_readiness == DefenseReadiness.NOT_READY

    def test_no_shipments_missing_evidence_critical(self):
        r = assess_fulfillment(self._inp(shipments=[]))
        assert len(r.missing_evidence) >= 1

    # ── Delivered with confirmation ───────────────────────────────

    def test_delivered_with_confirmation_strong(self):
        r = assess_fulfillment(self._inp(
            shipments=[self._ship(status=TrackingStatus.DELIVERED, has_del=True)]))
        assert r.overall_strength in (
            FulfillmentStrength.DEFINITIVE, FulfillmentStrength.STRONG)

    def test_delivered_with_confirmation_ready(self):
        r = assess_fulfillment(self._inp(
            shipments=[self._ship(status=TrackingStatus.DELIVERED, has_del=True)]))
        assert r.defense_readiness in (DefenseReadiness.READY, DefenseReadiness.READY_PARTIAL)

    def test_delivered_has_del_flag(self):
        r = assess_fulfillment(self._inp(
            shipments=[self._ship(has_del=True)]))
        assert r.has_delivery_confirmation is True

    # ── Signature required ────────────────────────────────────────

    def test_signature_required_for_750_plus(self):
        r = assess_fulfillment(self._inp(
            amount=800.0,
            shipments=[self._ship(has_del=True, has_sig=False)]))
        assert r.signature_required is True

    def test_no_signature_required_below_750(self):
        r = assess_fulfillment(self._inp(
            amount=100.0,
            shipments=[self._ship(has_del=True)]))
        assert r.signature_required is False

    def test_missing_signature_generates_gap_on_high_value(self):
        r = assess_fulfillment(self._inp(
            amount=800.0,
            shipments=[self._ship(has_del=True, has_sig=False)]))
        sig_gaps = [g for g in r.gaps if "signature" in g.lower()]
        assert len(sig_gaps) >= 1

    def test_definitive_when_signature_present_on_high_value(self):
        r = assess_fulfillment(self._inp(
            amount=800.0,
            shipments=[self._ship(
                status=TrackingStatus.DELIVERED,
                has_del=True, has_sig=True)]))
        assert r.overall_strength == FulfillmentStrength.DEFINITIVE

    # ── In-transit ────────────────────────────────────────────────

    def test_in_transit_moderate_strength(self):
        r = assess_fulfillment(self._inp(
            shipments=[self._ship(
                status=TrackingStatus.IN_TRANSIT, has_del=False)]))
        assert r.overall_strength == FulfillmentStrength.MODERATE

    def test_in_transit_partial_ready(self):
        r = assess_fulfillment(self._inp(
            shipments=[self._ship(
                status=TrackingStatus.IN_TRANSIT, has_del=False)]))
        assert r.defense_readiness in (
            DefenseReadiness.READY_PARTIAL, DefenseReadiness.NEEDS_EVIDENCE)

    # ── Label created only ────────────────────────────────────────

    def test_label_created_weak_strength(self):
        r = assess_fulfillment(self._inp(
            shipments=[self._ship(
                status=TrackingStatus.LABEL_CREATED, has_del=False)]))
        assert r.overall_strength == FulfillmentStrength.WEAK

    # ── Returned to sender ────────────────────────────────────────

    def test_returned_shipment_weak(self):
        r = assess_fulfillment(self._inp(
            shipments=[self._ship(
                status=TrackingStatus.RETURNED, has_del=False)]))
        assert r.overall_strength == FulfillmentStrength.WEAK

    def test_returned_generates_gap(self):
        r = assess_fulfillment(self._inp(
            shipments=[self._ship(
                status=TrackingStatus.RETURNED, has_del=False)]))
        assert len(r.gaps) >= 1

    # ── Positive win rate boost ───────────────────────────────────

    def test_delivered_boosts_win_rate(self):
        strong = assess_fulfillment(self._inp(
            shipments=[self._ship(
                status=TrackingStatus.DELIVERED, has_del=True)]))
        weak = assess_fulfillment(self._inp(
            shipments=[self._ship(
                status=TrackingStatus.LABEL_CREATED, has_del=False)]))
        assert strong.win_rate_adjustment > weak.win_rate_adjustment

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = assess_fulfillment(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_shipment_count_correct(self):
        ships = [self._ship(f"S-{i}") for i in range(3)]
        r = assess_fulfillment(self._inp(shipments=ships))
        assert r.shipment_count == 3
