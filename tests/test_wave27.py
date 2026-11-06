"""
Tests for Wave 27: seller_protection_checker, win_probability_calibrator.
"""
import pytest
from oracle.seller_protection_checker import (
    check_seller_protection, SellerProtectionInput, SPEligibilityStatus,
    SPCoverageType, SPEligibilityGapType,
)
from oracle.win_probability_calibrator import (
    calibrate_win_probability, WinProbabilityInput,
)


# ═══════════════════════════════════════════════════════════════════
# Seller Protection Checker Tests
# ═══════════════════════════════════════════════════════════════════

class TestSellerProtectionChecker:

    def _inp(self, **kw) -> SellerProtectionInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            transaction_amount=100.0,
            item_is_tangible=True,
            item_is_digital=False,
            item_is_service=False,
            item_is_custom=False,
            item_is_ticket=False,
            has_tracking_number=True,
            tracking_carrier="UPS",
            shipped_to_paypal_address=True,
            has_signature_confirmation=False,
            transaction_marked_no_sp=False,
            is_partial_payment=False,
            buyer_account_verified_2fa=False,
            no_account_breach_detected=True,
        )
        defaults.update(kw)
        return SellerProtectionInput(**defaults)

    # ── Fully eligible ────────────────────────────────────────────

    def test_inr_with_tracking_fully_eligible(self):
        r = check_seller_protection(self._inp())
        assert r.eligibility_status == SPEligibilityStatus.FULLY_ELIGIBLE

    def test_fully_eligible_waives_fee(self):
        r = check_seller_protection(self._inp())
        assert r.chargeback_fee_waived is True
        assert r.fee_savings == pytest.approx(20.0)

    # ── SNAD always ineligible ─────────────────────────────────────

    def test_snad_ineligible(self):
        r = check_seller_protection(self._inp(claim_type="SNAD"))
        assert r.eligibility_status == SPEligibilityStatus.INELIGIBLE

    def test_snad_no_coverage(self):
        r = check_seller_protection(self._inp(claim_type="SNAD"))
        assert r.coverage_type == SPCoverageType.NO_COVERAGE

    def test_snad_no_fee_savings(self):
        r = check_seller_protection(self._inp(claim_type="SNAD"))
        assert r.fee_savings == pytest.approx(0.0)

    # ── Tracking requirement ───────────────────────────────────────

    def test_missing_tracking_inr_ineligible(self):
        r = check_seller_protection(self._inp(has_tracking_number=False))
        assert r.eligibility_status == SPEligibilityStatus.INELIGIBLE
        gap_types = [g.gap_type for g in r.gaps]
        assert SPEligibilityGapType.MISSING_TRACKING in gap_types

    # ── Signature requirement for ≥ $750 ──────────────────────────

    def test_high_value_no_signature_ineligible(self):
        r = check_seller_protection(self._inp(transaction_amount=800.0))
        assert r.eligibility_status == SPEligibilityStatus.INELIGIBLE
        gap_types = [g.gap_type for g in r.gaps]
        assert SPEligibilityGapType.MISSING_SIGNATURE in gap_types

    def test_high_value_with_signature_eligible(self):
        r = check_seller_protection(self._inp(
            transaction_amount=800.0,
            has_signature_confirmation=True,
        ))
        assert r.eligibility_status == SPEligibilityStatus.FULLY_ELIGIBLE

    def test_signature_required_flag_set_at_750(self):
        r = check_seller_protection(self._inp(transaction_amount=750.0))
        assert r.signature_required is True

    def test_signature_not_required_below_750(self):
        r = check_seller_protection(self._inp(transaction_amount=749.0))
        assert r.signature_required is False

    # ── Wrong ship address ────────────────────────────────────────

    def test_wrong_address_ineligible(self):
        r = check_seller_protection(self._inp(shipped_to_paypal_address=False))
        assert r.eligibility_status == SPEligibilityStatus.INELIGIBLE
        gap_types = [g.gap_type for g in r.gaps]
        assert SPEligibilityGapType.WRONG_SHIP_ADDRESS in gap_types

    # ── Digital goods ─────────────────────────────────────────────

    def test_digital_goods_ineligible(self):
        r = check_seller_protection(self._inp(item_is_digital=True))
        assert r.eligibility_status == SPEligibilityStatus.INELIGIBLE
        gap_types = [g.gap_type for g in r.gaps]
        assert SPEligibilityGapType.DIGITAL_GOODS in gap_types

    def test_service_ineligible(self):
        r = check_seller_protection(self._inp(item_is_service=True))
        assert r.eligibility_status == SPEligibilityStatus.INELIGIBLE

    # ── Sale-time flags ───────────────────────────────────────────

    def test_no_sp_flag_ineligible(self):
        r = check_seller_protection(self._inp(transaction_marked_no_sp=True))
        assert r.eligibility_status == SPEligibilityStatus.INELIGIBLE
        gap_types = [g.gap_type for g in r.gaps]
        assert SPEligibilityGapType.NO_SP_FLAG_AT_SALE in gap_types

    def test_partial_payment_ineligible(self):
        r = check_seller_protection(self._inp(is_partial_payment=True))
        assert r.eligibility_status == SPEligibilityStatus.INELIGIBLE

    # ── Unauthorized coverage ─────────────────────────────────────

    def test_unauthorized_gives_coverage(self):
        r = check_seller_protection(self._inp(claim_type="UNAUTHORIZED"))
        assert r.coverage_type in (
            SPCoverageType.UNAUTHORIZED_COVERAGE, SPCoverageType.ENHANCED_COVERAGE)

    def test_unauthorized_enhanced_with_2fa(self):
        r = check_seller_protection(self._inp(
            claim_type="UNAUTHORIZED",
            buyer_account_verified_2fa=True,
            no_account_breach_detected=True,
        ))
        assert r.coverage_type == SPCoverageType.ENHANCED_COVERAGE

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = check_seller_protection(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_no_blocking_gaps_on_fully_eligible(self):
        r = check_seller_protection(self._inp())
        assert r.blocking_gaps == 0

    def test_recommendation_non_empty(self):
        r = check_seller_protection(self._inp())
        assert len(r.sp_recommendation) > 10


# ═══════════════════════════════════════════════════════════════════
# Win Probability Calibrator Tests
# ═══════════════════════════════════════════════════════════════════

class TestWinProbabilityCalibrator:

    def _inp(self, **kw) -> WinProbabilityInput:
        defaults = dict(
            dispute_id="D-001",
            claim_type="INR",
            sp_eligible=False,
            sp_conditionally_eligible=False,
            ce30_eligible=False,
            evidence_score=60.0,
            evidence_category="ADEQUATE",
            fraud_risk_level="LOW_RISK",
            merchant_responded=True,
            days_until_response=1,
            days_remaining_to_respond=10,
        )
        defaults.update(kw)
        return WinProbabilityInput(**defaults)

    # ── Probability range ─────────────────────────────────────────

    def test_final_p_win_in_range(self):
        r = calibrate_win_probability(self._inp())
        assert 0.0 <= r.final_p_win <= 1.0

    def test_confidence_band_contains_final(self):
        r = calibrate_win_probability(self._inp())
        assert r.confidence_band_low <= r.final_p_win <= r.confidence_band_high

    # ── Base rates ────────────────────────────────────────────────

    def test_inr_sp_eligible_higher_than_inr_no_sp(self):
        no_sp = calibrate_win_probability(self._inp(sp_eligible=False))
        with_sp = calibrate_win_probability(self._inp(sp_eligible=True))
        assert with_sp.final_p_win > no_sp.final_p_win

    def test_duplicate_base_higher_than_snad(self):
        snad = calibrate_win_probability(self._inp(claim_type="SNAD"))
        dup  = calibrate_win_probability(self._inp(claim_type="DUPLICATE"))
        assert dup.final_p_win > snad.final_p_win

    def test_ce30_raises_unauthorized_base(self):
        no_ce = calibrate_win_probability(self._inp(claim_type="UNAUTHORIZED"))
        ce30  = calibrate_win_probability(self._inp(
            claim_type="UNAUTHORIZED", ce30_eligible=True))
        assert ce30.final_p_win > no_ce.final_p_win

    # ── Evidence adjustments ──────────────────────────────────────

    def test_strong_evidence_raises_p_win(self):
        weak   = calibrate_win_probability(self._inp(
            evidence_score=35.0, evidence_category="POOR"))
        strong = calibrate_win_probability(self._inp(
            evidence_score=85.0, evidence_category="STRONG"))
        assert strong.final_p_win > weak.final_p_win

    def test_poor_evidence_lowers_p_win(self):
        adequate = calibrate_win_probability(self._inp(evidence_category="ADEQUATE"))
        poor     = calibrate_win_probability(self._inp(
            evidence_score=30.0, evidence_category="POOR"))
        assert poor.final_p_win < adequate.final_p_win

    # ── Fraud adjustments ─────────────────────────────────────────

    def test_high_fraud_risk_raises_p_win(self):
        low  = calibrate_win_probability(self._inp(fraud_risk_level="LOW_RISK"))
        high = calibrate_win_probability(self._inp(fraud_risk_level="HIGH_RISK"))
        assert high.final_p_win > low.final_p_win

    # ── Response adjustments ──────────────────────────────────────

    def test_no_response_lowers_p_win(self):
        responded    = calibrate_win_probability(self._inp(merchant_responded=True))
        no_response  = calibrate_win_probability(self._inp(merchant_responded=False))
        assert no_response.final_p_win < responded.final_p_win

    def test_overdue_deadline_lowers_p_win(self):
        comfortable = calibrate_win_probability(self._inp(days_remaining_to_respond=14))
        overdue     = calibrate_win_probability(self._inp(days_remaining_to_respond=-1))
        assert overdue.final_p_win < comfortable.final_p_win

    # ── Custom base override ──────────────────────────────────────

    def test_custom_base_p_win_used(self):
        r = calibrate_win_probability(self._inp(custom_base_p_win=0.80))
        assert r.base_rate_key == "CUSTOM"
        assert r.base_p_win == pytest.approx(0.80)

    # ── Fight recommendation ──────────────────────────────────────

    def test_fight_recommended_when_above_threshold(self):
        r = calibrate_win_probability(
            self._inp(
                sp_eligible=True,
                evidence_category="STRONG",
                evidence_score=85.0,
                fraud_risk_level="HIGH_RISK",
            ),
            fight_threshold=0.45,
        )
        assert r.fight_recommended is True

    def test_fight_not_recommended_when_below_threshold(self):
        r = calibrate_win_probability(
            self._inp(
                claim_type="SNAD",
                evidence_score=30.0,
                evidence_category="POOR",
                merchant_responded=False,
            ),
            fight_threshold=0.45,
        )
        assert r.fight_recommended is False

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = calibrate_win_probability(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_total_adjustment_matches_factors(self):
        r = calibrate_win_probability(self._inp())
        expected = sum(f.adjustment for f in r.factors)
        assert abs(r.total_adjustment - expected) < 0.001

    def test_factors_count_expected(self):
        r = calibrate_win_probability(self._inp())
        assert len(r.factors) == 5  # evidence, fraud, SP, response, deadline
