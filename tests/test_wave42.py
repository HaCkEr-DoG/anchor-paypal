"""
Tests for Wave 42: evidence_package_builder, seller_protection_checker.
"""
import pytest
from oracle.evidence_package_builder import (
    build_evidence_package, EvidencePackageInput,
    EvidenceStrength,
)
from oracle.seller_protection_checker import (
    check_seller_protection, SellerProtectionInput,
    SPEligibility, DisputeTypeForSP, SPRequirement,
)


# ═══════════════════════════════════════════════════════════════════
# Evidence Package Builder Tests
# ═══════════════════════════════════════════════════════════════════

class TestEvidencePackageBuilder:

    def _inp(self, reason_code="INR", **kw) -> EvidencePackageInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code=reason_code,
            network="PAYPAL",
            transaction_amount=100.0,
        )
        defaults.update(kw)
        return EvidencePackageInput(**defaults)

    # ── Basic structure ───────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = build_evidence_package(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_completeness_bounded(self):
        r = build_evidence_package(self._inp())
        assert 0.0 <= r.completeness_score <= 100.0

    # ── INR — no evidence ────────────────────────────────────────

    def test_inr_no_evidence_cannot_file(self):
        r = build_evidence_package(self._inp(reason_code="INR"))
        assert r.can_file is False

    def test_inr_no_evidence_insufficient(self):
        r = build_evidence_package(self._inp(reason_code="INR"))
        assert r.evidence_strength == EvidenceStrength.INSUFFICIENT

    def test_inr_missing_required_listed(self):
        r = build_evidence_package(self._inp(reason_code="INR"))
        assert "has_tracking_number" in r.missing_required

    # ── INR — full evidence ───────────────────────────────────────

    def test_inr_full_evidence_can_file(self):
        r = build_evidence_package(self._inp(
            reason_code="INR",
            has_tracking_number=True,
            has_shipment_date_proof=True,
            has_delivery_confirmation=True,
            tracking_shows_delivered=True,
        ))
        assert r.can_file is True

    def test_inr_full_evidence_strong(self):
        r = build_evidence_package(self._inp(
            reason_code="INR",
            has_tracking_number=True,
            has_shipment_date_proof=True,
            has_delivery_confirmation=True,
        ))
        assert r.evidence_strength in (
            EvidenceStrength.STRONG, EvidenceStrength.COMPELLING, EvidenceStrength.ADEQUATE)

    def test_inr_no_missing_required_when_all_present(self):
        r = build_evidence_package(self._inp(
            reason_code="INR",
            has_tracking_number=True,
            has_shipment_date_proof=True,
        ))
        assert len(r.missing_required) == 0

    # ── SNAD ──────────────────────────────────────────────────────

    def test_snad_requires_product_listing(self):
        r = build_evidence_package(self._inp(reason_code="SNAD"))
        assert "has_product_listing" in r.missing_required

    def test_snad_full_evidence_can_file(self):
        r = build_evidence_package(self._inp(
            reason_code="SNAD",
            has_product_listing=True,
            has_refund_policy_at_checkout=True,
        ))
        assert r.can_file is True

    # ── Unauthorized ──────────────────────────────────────────────

    def test_unauthorized_requires_ip_log(self):
        r = build_evidence_package(self._inp(reason_code="UNAUTHORIZED"))
        assert "has_ip_log" in r.missing_required

    def test_unauthorized_with_evidence_can_file(self):
        r = build_evidence_package(self._inp(
            reason_code="UNAUTHORIZED",
            has_ip_log=True,
            has_order_history=True,
        ))
        assert r.can_file is True

    # ── Visa 10.4 ─────────────────────────────────────────────────

    def test_visa_10_4_requires_device_fingerprint(self):
        r = build_evidence_package(self._inp(
            reason_code="10.4", network="VISA",
            has_ip_log=True, has_order_history=True))
        assert "has_device_fingerprint" in r.missing_required

    def test_visa_10_4_full_evidence_can_file(self):
        r = build_evidence_package(self._inp(
            reason_code="10.4", network="VISA",
            has_ip_log=True,
            has_order_history=True,
            has_device_fingerprint=True,
        ))
        assert r.can_file is True

    # ── High-value signature gap ──────────────────────────────────

    def test_high_value_no_sig_generates_gap(self):
        r = build_evidence_package(self._inp(
            reason_code="INR", transaction_amount=800.0,
            has_tracking_number=True, has_shipment_date_proof=True,
            has_signature_confirmation=False,
        ))
        sig_gaps = [g for g in r.gaps if "signature" in g.field_name.lower()]
        assert len(sig_gaps) >= 1

    def test_low_value_no_sig_no_gap(self):
        r = build_evidence_package(self._inp(
            reason_code="INR", transaction_amount=100.0,
            has_tracking_number=True, has_shipment_date_proof=True,
        ))
        sig_gaps = [g for g in r.gaps if "signature" in g.field_name.lower()]
        assert len(sig_gaps) == 0

    # ── Win rate boost ────────────────────────────────────────────

    def test_compelling_positive_boost(self):
        r = build_evidence_package(self._inp(
            reason_code="INR",
            has_tracking_number=True,
            has_shipment_date_proof=True,
            has_delivery_confirmation=True,
            has_signature_confirmation=True,
            has_buyer_communication=True,
            communication_supports_merchant=True,
            tracking_shows_delivered=True,
        ))
        assert r.win_rate_boost > 0.0

    def test_insufficient_negative_boost(self):
        r = build_evidence_package(self._inp(reason_code="INR"))
        assert r.win_rate_boost < 0.0

    # ── Recommendations ───────────────────────────────────────────

    def test_missing_required_in_recommendations(self):
        r = build_evidence_package(self._inp(reason_code="SNAD"))
        assert len(r.recommendations) >= 1

    # ── Items list ────────────────────────────────────────────────

    def test_items_non_empty(self):
        r = build_evidence_package(self._inp(reason_code="INR"))
        assert len(r.items) >= 1

    def test_items_reflect_present_flags(self):
        r = build_evidence_package(self._inp(
            reason_code="INR",
            has_tracking_number=True,
        ))
        tracking_item = next(
            (i for i in r.items if i.item_id == "has_tracking_number"), None)
        if tracking_item:
            assert tracking_item.is_present is True


# ═══════════════════════════════════════════════════════════════════
# Seller Protection Checker Tests
# ═══════════════════════════════════════════════════════════════════

class TestSellerProtectionChecker:

    def _inp(self, dispute_type=DisputeTypeForSP.INR, amount=100.0, **kw) -> SellerProtectionInput:
        defaults = dict(
            dispute_id="D-001",
            dispute_type=dispute_type,
            transaction_amount=amount,
            paid_via_paypal_checkout=True,
            is_physical_tangible_good=True,
            item_category="general",
            shipped_to_paypal_address=True,
            tracking_uploaded_to_paypal=True,
            tracking_uploaded_before_dispute=True,
            has_delivery_confirmation=True,
            has_signature_confirmation=False,
            buyer_address_confirmed=True,
            buyer_address_mismatch=False,
            is_digital_goods=False,
            has_ce30_qualification=False,
        )
        defaults.update(kw)
        return SellerProtectionInput(**defaults)

    # ── SNAD always ineligible ────────────────────────────────────

    def test_snad_ineligible(self):
        r = check_seller_protection(self._inp(
            dispute_type=DisputeTypeForSP.SNAD))
        assert r.eligibility == SPEligibility.INELIGIBLE

    def test_snad_negative_impact(self):
        r = check_seller_protection(self._inp(
            dispute_type=DisputeTypeForSP.SNAD))
        assert r.win_rate_impact < 0.0

    def test_subscription_ineligible(self):
        r = check_seller_protection(self._inp(
            dispute_type=DisputeTypeForSP.SUBSCRIPTION))
        assert r.eligibility == SPEligibility.INELIGIBLE

    # ── INR fully eligible ────────────────────────────────────────

    def test_inr_fully_eligible(self):
        r = check_seller_protection(self._inp(
            dispute_type=DisputeTypeForSP.INR))
        assert r.eligibility in (
            SPEligibility.ELIGIBLE, SPEligibility.ELIGIBLE_WITH_GAPS)

    def test_eligible_positive_impact(self):
        r = check_seller_protection(self._inp(
            dispute_type=DisputeTypeForSP.INR))
        assert r.win_rate_impact > 0.0

    # ── Missing tracking ──────────────────────────────────────────

    def test_missing_tracking_blocks_sp(self):
        r = check_seller_protection(self._inp(
            tracking_uploaded_to_paypal=False,
            tracking_uploaded_before_dispute=False,
        ))
        assert len(r.blocking_issues) >= 1

    def test_missing_tracking_ineligible(self):
        r = check_seller_protection(self._inp(
            tracking_uploaded_to_paypal=False,
            tracking_uploaded_before_dispute=False,
        ))
        assert r.eligibility in (
            SPEligibility.INELIGIBLE, SPEligibility.PARTIAL)

    # ── Digital goods ─────────────────────────────────────────────

    def test_digital_goods_ineligible(self):
        r = check_seller_protection(self._inp(is_digital_goods=True))
        assert r.eligibility == SPEligibility.INELIGIBLE

    def test_digital_goods_blocking_issue(self):
        r = check_seller_protection(self._inp(is_digital_goods=True))
        assert any("digital" in b.lower() for b in r.blocking_issues)

    # ── Excluded categories ───────────────────────────────────────

    def test_excluded_category_ineligible(self):
        r = check_seller_protection(self._inp(item_category="vehicle"))
        assert r.eligibility == SPEligibility.INELIGIBLE

    # ── Signature requirement ─────────────────────────────────────

    def test_signature_required_at_750(self):
        r = check_seller_protection(self._inp(amount=750.0))
        assert r.signature_required is True

    def test_no_signature_required_below_750(self):
        r = check_seller_protection(self._inp(amount=100.0))
        assert r.signature_required is False

    def test_missing_signature_blocks_on_high_value(self):
        r = check_seller_protection(self._inp(
            amount=800.0,
            has_signature_confirmation=False,
        ))
        assert any("signature" in b.lower() for b in r.blocking_issues)

    def test_signature_present_high_value_eligible(self):
        r = check_seller_protection(self._inp(
            amount=800.0,
            has_signature_confirmation=True,
        ))
        assert r.eligibility in (
            SPEligibility.ELIGIBLE, SPEligibility.ELIGIBLE_WITH_GAPS)

    # ── CE3.0 ─────────────────────────────────────────────────────

    def test_ce30_boost_on_unauthorized(self):
        no_ce = check_seller_protection(self._inp(
            dispute_type=DisputeTypeForSP.UNAUTHORIZED,
            has_ce30_qualification=False,
        ))
        with_ce = check_seller_protection(self._inp(
            dispute_type=DisputeTypeForSP.UNAUTHORIZED,
            has_ce30_qualification=True,
        ))
        assert with_ce.win_rate_impact > no_ce.win_rate_impact

    def test_ce30_flag_set(self):
        r = check_seller_protection(self._inp(
            dispute_type=DisputeTypeForSP.UNAUTHORIZED,
            has_ce30_qualification=True,
        ))
        assert r.ce30_applicable is True

    def test_ce30_not_applicable_for_inr(self):
        r = check_seller_protection(self._inp(
            dispute_type=DisputeTypeForSP.INR,
            has_ce30_qualification=True,
        ))
        assert r.ce30_applicable is False

    # ── Address mismatch ──────────────────────────────────────────

    def test_address_mismatch_blocks(self):
        r = check_seller_protection(self._inp(
            shipped_to_paypal_address=False,
            buyer_address_mismatch=True,
        ))
        assert len(r.blocking_issues) >= 1

    # ── Summary ───────────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = check_seller_protection(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_requirement_checks_non_empty(self):
        r = check_seller_protection(self._inp())
        assert len(r.requirement_checks) >= 1
