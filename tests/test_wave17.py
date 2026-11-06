"""
Tests for Wave 17: partial_refund_calculator, win_rate_calibrator.
"""
import pytest
from oracle.partial_refund_calculator import (
    compute_partial_refund, PartialRefundInput, PartialRefundBasis,
    PartialRefundOutcome, OrderItem,
)
from oracle.win_rate_calibrator import (
    calibrate_win_rate, WinRateInput, ClaimType, CarrierTier, EvidenceTier,
)


# ═══════════════════════════════════════════════════════════════════
# Partial Refund Calculator Tests
# ═══════════════════════════════════════════════════════════════════

class TestPartialRefundCalculator:

    def _inp(self, basis=PartialRefundBasis.DELIVERY_VALUE_PCT, **kw) -> PartialRefundInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            order_total=200.0,
            dispute_amount=200.0,
            basis=basis,
            delivery_pct=0.5,
            p_win_no_refund=0.5,
            fight_cost=25.0,
        )
        defaults.update(kw)
        return PartialRefundInput(**defaults)

    def test_50pct_delivery_gives_50pct_refund(self):
        r = compute_partial_refund(self._inp(delivery_pct=0.5))
        assert r.recommended_refund_pct == pytest.approx(0.5, abs=0.01)

    def test_full_delivery_no_refund(self):
        r = compute_partial_refund(self._inp(delivery_pct=1.0))
        assert r.outcome == PartialRefundOutcome.NO_REFUND
        assert r.recommended_refund_pct <= 0.05

    def test_zero_delivery_full_refund(self):
        r = compute_partial_refund(self._inp(delivery_pct=0.0))
        assert r.outcome == PartialRefundOutcome.FULL_REFUND

    def test_item_count_basis_3_of_5_received(self):
        items = [
            OrderItem("I1", "Widget", quantity_ordered=5, quantity_received=3, unit_price=40.0),
        ]
        r = compute_partial_refund(self._inp(
            basis=PartialRefundBasis.ITEM_COUNT,
            items=items,
        ))
        # 2 of 5 not received = 40% refund
        assert r.recommended_refund_amount == pytest.approx(80.0, abs=0.01)
        assert r.outcome == PartialRefundOutcome.PARTIAL_REFUND

    def test_all_items_received_no_refund(self):
        items = [
            OrderItem("I1", "Widget", quantity_ordered=3, quantity_received=3, unit_price=50.0),
        ]
        r = compute_partial_refund(self._inp(
            basis=PartialRefundBasis.ITEM_COUNT,
            items=items,
        ))
        assert r.outcome == PartialRefundOutcome.NO_REFUND

    def test_damaged_item_reduces_refund(self):
        items = [
            OrderItem("I1", "Vase", quantity_ordered=1, quantity_received=1,
                      unit_price=100.0, is_damaged=True, damage_reduction_pct=0.4),
        ]
        r = compute_partial_refund(self._inp(
            basis=PartialRefundBasis.ITEM_COUNT,
            items=items,
            dispute_amount=100.0,
        ))
        # Received but 40% damaged → refund = 100 * 0.4 = $40
        assert r.recommended_refund_amount == pytest.approx(40.0, abs=0.01)

    def test_service_proportion_half_used(self):
        r = compute_partial_refund(self._inp(
            basis=PartialRefundBasis.SERVICE_PROPORTION,
            service_days_delivered=15,
            service_days_total=30,
            dispute_amount=100.0,
        ))
        assert r.recommended_refund_amount == pytest.approx(50.0, abs=0.01)

    def test_service_full_delivered_no_refund(self):
        r = compute_partial_refund(self._inp(
            basis=PartialRefundBasis.SERVICE_PROPORTION,
            service_days_delivered=30,
            service_days_total=30,
            dispute_amount=100.0,
        ))
        assert r.outcome == PartialRefundOutcome.NO_REFUND

    def test_opening_offer_lt_fair_amount(self):
        r = compute_partial_refund(self._inp(delivery_pct=0.5))
        assert r.opening_offer_amount <= r.recommended_refund_amount

    def test_loyal_buyer_gets_full_fair_upfront(self):
        r = compute_partial_refund(self._inp(delivery_pct=0.5, buyer_is_loyal=True))
        assert r.opening_offer_amount == pytest.approx(r.recommended_refund_amount, abs=0.01)

    def test_ev_partial_negative(self):
        r = compute_partial_refund(self._inp(delivery_pct=0.5, dispute_amount=200.0))
        assert r.ev_partial_refund < 0

    def test_ev_full_refund_negative_full_amount(self):
        r = compute_partial_refund(self._inp(delivery_pct=0.5, dispute_amount=200.0))
        assert r.ev_full_refund == pytest.approx(-200.0, abs=0.01)

    def test_ev_no_refund_positive_when_high_win(self):
        r = compute_partial_refund(self._inp(p_win_no_refund=0.9, dispute_amount=200.0, fight_cost=25.0))
        assert r.ev_no_refund > 0

    def test_min_refund_pct_respected(self):
        r = compute_partial_refund(self._inp(delivery_pct=0.0, min_refund_pct=0.3))
        assert r.recommended_refund_pct >= 0.3

    def test_max_refund_pct_respected(self):
        r = compute_partial_refund(self._inp(delivery_pct=1.0, max_refund_pct=0.3, min_refund_pct=0.0))
        assert r.recommended_refund_pct <= 0.30 + 0.01

    def test_item_lines_non_empty_for_item_basis(self):
        items = [OrderItem("I1", "Widget", 3, 1, 50.0)]
        r = compute_partial_refund(self._inp(
            basis=PartialRefundBasis.ITEM_COUNT, items=items,
        ))
        assert len(r.item_lines) > 0

    def test_buyer_message_non_empty_for_partial(self):
        r = compute_partial_refund(self._inp(delivery_pct=0.5))
        assert len(r.buyer_message) > 20

    def test_buyer_message_empty_for_no_refund(self):
        r = compute_partial_refund(self._inp(delivery_pct=1.0))
        assert r.buyer_message == ""

    def test_summary_contains_dispute_id(self):
        r = compute_partial_refund(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_refund_pct_between_0_and_1(self):
        r = compute_partial_refund(self._inp(delivery_pct=0.7))
        assert 0.0 <= r.recommended_refund_pct <= 1.0

    def test_damage_plus_undelivered_combined(self):
        items = [
            OrderItem("I1", "Lamp", quantity_ordered=4, quantity_received=2,
                      unit_price=50.0, is_damaged=True, damage_reduction_pct=0.5),
        ]
        r = compute_partial_refund(self._inp(
            basis=PartialRefundBasis.ITEM_COUNT,
            items=items,
            dispute_amount=200.0,
        ))
        # 2 not received ($100) + 2 damaged at 50% ($50) = $150
        assert r.recommended_refund_amount == pytest.approx(150.0, abs=0.01)


# ═══════════════════════════════════════════════════════════════════
# Win Rate Calibrator Tests
# ═══════════════════════════════════════════════════════════════════

class TestWinRateCalibrator:

    def _inp(self, claim_type=ClaimType.INR, **kw) -> WinRateInput:
        defaults = dict(
            dispute_id="D-001",
            dispute_amount=100.0,
            is_formal_dispute=True,
        )
        defaults.update(kw)
        return WinRateInput(claim_type=claim_type, **defaults)

    def test_base_inr_rate_in_range(self):
        r = calibrate_win_rate(self._inp())
        assert 0.02 <= r.calibrated_win_rate <= 0.97

    def test_signature_increases_win_rate(self):
        base = calibrate_win_rate(self._inp())
        with_sig = calibrate_win_rate(self._inp(has_signature_confirmation=True))
        assert with_sig.calibrated_win_rate > base.calibrated_win_rate

    def test_no_tracking_lowers_inr_win_rate(self):
        with_tracking = calibrate_win_rate(self._inp(has_tracking_number=True))
        no_tracking = calibrate_win_rate(self._inp(has_tracking_number=False))
        assert no_tracking.calibrated_win_rate < with_tracking.calibrated_win_rate

    def test_ce30_boosts_unauthorized(self):
        base = calibrate_win_rate(self._inp(claim_type=ClaimType.UNAUTHORIZED))
        with_ce30 = calibrate_win_rate(self._inp(
            claim_type=ClaimType.UNAUTHORIZED, ce30_eligible=True,
        ))
        assert with_ce30.calibrated_win_rate > base.calibrated_win_rate

    def test_ce30_does_not_boost_inr(self):
        base = calibrate_win_rate(self._inp(claim_type=ClaimType.INR))
        with_ce30 = calibrate_win_rate(self._inp(
            claim_type=ClaimType.INR, ce30_eligible=True,
        ))
        # CE3.0 is for UNAUTHORIZED only, should not boost INR
        assert with_ce30.calibrated_win_rate == base.calibrated_win_rate

    def test_serial_filer_increases_win_rate(self):
        base = calibrate_win_rate(self._inp())
        serial = calibrate_win_rate(self._inp(buyer_dispute_count_90d=4))
        assert serial.calibrated_win_rate > base.calibrated_win_rate

    def test_fraud_network_buyer_boosts_rate(self):
        base = calibrate_win_rate(self._inp())
        fraud = calibrate_win_rate(self._inp(buyer_is_in_fraud_network=True))
        assert fraud.calibrated_win_rate > base.calibrated_win_rate

    def test_tier3_carrier_lowers_rate(self):
        t1 = calibrate_win_rate(self._inp(carrier_tier=CarrierTier.TIER_1))
        t3 = calibrate_win_rate(self._inp(carrier_tier=CarrierTier.TIER_3))
        assert t3.calibrated_win_rate < t1.calibrated_win_rate

    def test_sp_covered_increases_rate(self):
        base = calibrate_win_rate(self._inp())
        sp = calibrate_win_rate(self._inp(sp_covered=True))
        assert sp.calibrated_win_rate > base.calibrated_win_rate

    def test_sp_not_applied_to_snad(self):
        # SNAD is NOT covered by Seller Protection
        base_snad = calibrate_win_rate(self._inp(claim_type=ClaimType.SNAD, sp_covered=False))
        sp_snad = calibrate_win_rate(self._inp(claim_type=ClaimType.SNAD, sp_covered=True))
        # SP flag set to True but claim is SNAD so sp_covered=False in logic
        assert base_snad.calibrated_win_rate == sp_snad.calibrated_win_rate

    def test_excellent_evidence_max_boost(self):
        base = calibrate_win_rate(self._inp())
        exc = calibrate_win_rate(self._inp(evidence_tier=EvidenceTier.EXCELLENT))
        assert exc.calibrated_win_rate > base.calibrated_win_rate

    def test_insufficient_evidence_hurts_rate(self):
        good = calibrate_win_rate(self._inp(evidence_tier=EvidenceTier.GOOD))
        insuf = calibrate_win_rate(self._inp(evidence_tier=EvidenceTier.INSUFFICIENT))
        assert insuf.calibrated_win_rate < good.calibrated_win_rate

    def test_high_value_lowers_rate(self):
        low = calibrate_win_rate(self._inp(dispute_amount=100.0))
        high = calibrate_win_rate(self._inp(dispute_amount=800.0))
        assert high.calibrated_win_rate < low.calibrated_win_rate

    def test_win_rate_clamped_above_minimum(self):
        # Worst case — all penalties
        r = calibrate_win_rate(self._inp(
            claim_type=ClaimType.SNAD,
            has_tracking_number=False,
            carrier_tier=CarrierTier.TIER_3,
            dispute_amount=800.0,
            evidence_tier=EvidenceTier.INSUFFICIENT,
        ))
        assert r.calibrated_win_rate >= 0.02

    def test_win_rate_clamped_below_maximum(self):
        r = calibrate_win_rate(self._inp(
            has_signature_confirmation=True,
            has_delivery_photo=True,
            has_carrier_affidavit=True,
            carrier_tier=CarrierTier.TIER_1,
            buyer_dispute_count_90d=5,
            sp_covered=True,
            evidence_tier=EvidenceTier.EXCELLENT,
        ))
        assert r.calibrated_win_rate <= 0.97

    def test_adjustments_list_non_empty(self):
        r = calibrate_win_rate(self._inp())
        assert len(r.adjustments) > 0

    def test_summary_contains_dispute_id(self):
        r = calibrate_win_rate(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_confidence_high_with_many_factors(self):
        r = calibrate_win_rate(self._inp(
            has_signature_confirmation=True,
            has_delivery_photo=True,
            has_carrier_affidavit=True,
            carrier_tier=CarrierTier.TIER_1,
            sp_covered=True,
        ))
        assert r.confidence in ("HIGH", "MEDIUM")

    def test_snad_base_rate_lower_than_inr(self):
        inr = calibrate_win_rate(self._inp(claim_type=ClaimType.INR))
        snad = calibrate_win_rate(self._inp(claim_type=ClaimType.SNAD))
        assert snad.base_win_rate < inr.base_win_rate

    def test_win_rate_pct_matches_rate(self):
        r = calibrate_win_rate(self._inp())
        assert r.calibrated_win_pct == pytest.approx(r.calibrated_win_rate * 100, abs=0.01)
