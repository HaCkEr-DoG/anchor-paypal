"""
Wave 69 — Seller Protection Eligibility Checker
Tests: 26 total
"""

import pytest
from oracle.seller_protection_checker import (
    check_seller_protection,
    SellerProtectionInput,
    EligibilityStatus,
    ProtectionType,
    RecommendedAction,
    FulfillmentMethod,
)


def _inr(**kwargs):
    defaults = dict(
        dispute_id="D001",
        reason_code="INR",
        transaction_amount=200.0,
        fulfillment_method=FulfillmentMethod.PHYSICAL_SHIPMENT,
        has_tracking_number=True,
        tracking_carrier="USPS",
        delivery_confirmed=True,
        paid_via_paypal_checkout=True,
        buyer_address_confirmed=True,
        intangible_goods=False,
    )
    defaults.update(kwargs)
    return SellerProtectionInput(**defaults)


def _unauth(**kwargs):
    defaults = dict(
        dispute_id="D002",
        reason_code="UNAUTHORIZED",
        transaction_amount=150.0,
        fulfillment_method=FulfillmentMethod.PHYSICAL_SHIPMENT,
        has_tracking_number=True,
        paid_via_paypal_checkout=True,
        buyer_address_confirmed=True,
        intangible_goods=False,
    )
    defaults.update(kwargs)
    return SellerProtectionInput(**defaults)


class TestSellerProtectionChecker:

    # ─── INR — eligible ───────────────────────────────────────────────────────

    def test_inr_fully_eligible(self):
        r = check_seller_protection(_inr())
        assert r.eligibility_status == EligibilityStatus.ELIGIBLE

    def test_inr_eligible_protection_type(self):
        r = check_seller_protection(_inr())
        assert r.protection_type == ProtectionType.ITEM_NOT_RECEIVED

    def test_inr_eligible_recommends_fight(self):
        r = check_seller_protection(_inr())
        assert r.recommended_action == RecommendedAction.FIGHT

    def test_inr_eligible_confidence_high(self):
        r = check_seller_protection(_inr())
        assert r.fight_confidence >= 0.80

    # ─── INR — missing tracking ───────────────────────────────────────────────

    def test_inr_no_tracking_ineligible(self):
        r = check_seller_protection(_inr(has_tracking_number=False))
        assert r.eligibility_status in (
            EligibilityStatus.INELIGIBLE,
            EligibilityStatus.PARTIALLY_ELIGIBLE,
        )

    def test_inr_no_tracking_refund_or_conditional(self):
        r = check_seller_protection(_inr(has_tracking_number=False))
        assert r.recommended_action in (
            RecommendedAction.REFUND_NOW,
            RecommendedAction.CONDITIONAL_FIGHT,
        )

    # ─── INR — high value missing signature ───────────────────────────────────

    def test_inr_750_plus_needs_signature(self):
        r = check_seller_protection(_inr(transaction_amount=800.0, signature_obtained=False))
        assert r.eligibility_status in (
            EligibilityStatus.INELIGIBLE,
            EligibilityStatus.PARTIALLY_ELIGIBLE,
        )

    def test_inr_750_plus_with_signature_eligible(self):
        r = check_seller_protection(_inr(
            transaction_amount=800.0,
            signature_obtained=True,
        ))
        assert r.eligibility_status == EligibilityStatus.ELIGIBLE

    # ─── Unauthorized — eligible ──────────────────────────────────────────────

    def test_unauthorized_eligible(self):
        r = check_seller_protection(_unauth())
        assert r.eligibility_status == EligibilityStatus.ELIGIBLE

    def test_unauthorized_protection_type(self):
        r = check_seller_protection(_unauth())
        assert r.protection_type == ProtectionType.UNAUTHORIZED

    def test_unauthorized_recommends_fight(self):
        r = check_seller_protection(_unauth())
        assert r.recommended_action == RecommendedAction.FIGHT

    # ─── Unauthorized — unconfirmed address ───────────────────────────────────

    def test_unauthorized_unconfirmed_address_ineligible(self):
        r = check_seller_protection(_unauth(buyer_address_confirmed=False))
        assert r.eligibility_status in (
            EligibilityStatus.INELIGIBLE,
            EligibilityStatus.PARTIALLY_ELIGIBLE,
        )

    # ─── SNAD — always ineligible ─────────────────────────────────────────────

    def test_snad_ineligible(self):
        inp = SellerProtectionInput(
            dispute_id="D003",
            reason_code="SNAD",
            transaction_amount=99.0,
            paid_via_paypal_checkout=True,
        )
        r = check_seller_protection(inp)
        assert r.eligibility_status == EligibilityStatus.INELIGIBLE

    def test_snad_recommends_refund(self):
        inp = SellerProtectionInput(
            dispute_id="D003",
            reason_code="SNAD",
            transaction_amount=99.0,
            paid_via_paypal_checkout=True,
        )
        r = check_seller_protection(inp)
        assert r.recommended_action == RecommendedAction.REFUND_NOW

    # ─── Categorical exclusions ───────────────────────────────────────────────

    def test_donation_ineligible(self):
        inp = SellerProtectionInput(
            dispute_id="D004",
            reason_code="UNAUTHORIZED",
            transaction_amount=50.0,
            paid_via_paypal_checkout=True,
            is_donation=True,
        )
        r = check_seller_protection(inp)
        assert r.eligibility_status == EligibilityStatus.INELIGIBLE

    def test_money_transfer_ineligible(self):
        inp = SellerProtectionInput(
            dispute_id="D005",
            reason_code="UNAUTHORIZED",
            transaction_amount=200.0,
            paid_via_paypal_checkout=True,
            is_money_transfer=True,
        )
        r = check_seller_protection(inp)
        assert r.eligibility_status == EligibilityStatus.INELIGIBLE

    def test_non_paypal_payment_ineligible(self):
        r = check_seller_protection(_inr(paid_via_paypal_checkout=False))
        assert r.eligibility_status == EligibilityStatus.INELIGIBLE

    # ─── Digital goods ────────────────────────────────────────────────────────

    def test_digital_goods_inr_ineligible(self):
        r = check_seller_protection(_inr(
            intangible_goods=True,
            fulfillment_method=FulfillmentMethod.DIGITAL_DOWNLOAD,
        ))
        assert r.eligibility_status in (
            EligibilityStatus.INELIGIBLE,
            EligibilityStatus.PARTIALLY_ELIGIBLE,
        )

    def test_digital_goods_unauthorized_can_be_eligible(self):
        r = check_seller_protection(_unauth(intangible_goods=True))
        # Unauthorized protection applies even to intangible goods
        assert r.eligibility_status in (
            EligibilityStatus.ELIGIBLE,
            EligibilityStatus.PARTIALLY_ELIGIBLE,
        )

    # ─── Findings structure ───────────────────────────────────────────────────

    def test_findings_list_not_empty(self):
        r = check_seller_protection(_inr())
        assert len(r.findings) > 0

    def test_ineligible_reasons_populated_on_block(self):
        r = check_seller_protection(_inr(has_tracking_number=False))
        assert len(r.ineligible_reasons) >= 1

    def test_no_ineligible_reasons_when_eligible(self):
        r = check_seller_protection(_inr())
        assert len(r.ineligible_reasons) == 0

    # ─── PayPal signal ────────────────────────────────────────────────────────

    def test_paypal_signal_populated(self):
        r = check_seller_protection(_inr())
        assert len(r.paypal_signal) > 20

    def test_ineligible_paypal_signal_mentions_dispute_id(self):
        r = check_seller_protection(_inr(has_tracking_number=False))
        assert "D001" in r.paypal_signal

    # ─── Fight confidence ─────────────────────────────────────────────────────

    def test_ineligible_fight_confidence_low(self):
        r = check_seller_protection(_inr(has_tracking_number=False, paid_via_paypal_checkout=False))
        assert r.fight_confidence <= 0.20

    def test_action_detail_populated(self):
        r = check_seller_protection(_inr())
        assert len(r.action_detail) > 20
