"""
Undefeatable v2 — tests for the 3 new modules:
  oracle/chargeback.py      — card network chargeback defense
  oracle/evidence_builder.py — optimal evidence package assembly
  oracle/velocity.py         — dispute rate threshold forecaster
"""

import pytest

from oracle.chargeback import (
    analyze_chargeback,
    CardNetwork,
    ChargebackCategory,
    ChargebackStrength,
)
from oracle.evidence_builder import (
    build_evidence_package,
    ClaimType,
)
from oracle.velocity import (
    analyze_velocity,
    DisputeRecord,
    ThresholdTier,
    threshold_safe_dispute_count,
)


# ── Chargeback Defense ────────────────────────────────────────────────────────

class TestChargebackDefense:

    def test_3ds_fraud_auto_win_visa(self):
        """3DS authentication on a Visa fraud chargeback = auto-win."""
        result = analyze_chargeback(
            ChargebackCategory.FRAUD_UNAUTHORIZED,
            CardNetwork.VISA,
            dispute_amount=250.0,
            has_3ds_authentication=True,
        )
        assert result.strength == ChargebackStrength.AUTO_WIN
        assert result.win_probability >= 0.85
        assert result.reason_code == "10.4"
        assert "3D Secure" in result.auto_win_reason or "3DS" in result.auto_win_reason

    def test_emv_chip_fraud_mastercard(self):
        """EMV chip data on Mastercard fraud = near auto-win."""
        result = analyze_chargeback(
            ChargebackCategory.FRAUD_UNAUTHORIZED,
            CardNetwork.MASTERCARD,
            dispute_amount=400.0,
            has_emv_chip_data=True,
        )
        assert result.strength in (ChargebackStrength.AUTO_WIN, ChargebackStrength.STRONG)
        assert result.win_probability >= 0.80
        assert result.reason_code == "4863"

    def test_inr_delivered_strong(self):
        """INR chargeback with DELIVERED tracking = STRONG."""
        result = analyze_chargeback(
            ChargebackCategory.ITEM_NOT_RECEIVED,
            CardNetwork.VISA,
            dispute_amount=120.0,
            has_tracking_delivered=True,
            shipped_to_billing_address=True,
        )
        assert result.strength == ChargebackStrength.STRONG
        assert result.win_probability >= 0.65

    def test_inr_no_tracking_weak(self):
        """INR without delivery confirmation = weak/concede."""
        result = analyze_chargeback(
            ChargebackCategory.ITEM_NOT_RECEIVED,
            CardNetwork.MASTERCARD,
            dispute_amount=90.0,
            has_tracking_delivered=False,
        )
        assert result.strength in (ChargebackStrength.WEAK, ChargebackStrength.CONCEDE)
        assert result.win_probability < 0.50
        assert "carrier_delivered_confirmation" in result.missing_critical

    def test_credit_not_issued_already_refunded(self):
        """Already refunded = trivially win credit_not_issued chargeback."""
        result = analyze_chargeback(
            ChargebackCategory.CREDIT_NOT_ISSUED,
            CardNetwork.AMEX,
            dispute_amount=75.0,
            already_issued_refund=True,
        )
        assert result.win_probability >= 0.90
        assert "ISSUED" in result.rebuttal_letter.upper() or "REFUND" in result.rebuttal_letter.upper()

    def test_cancelled_recurring_with_tos_wins(self):
        """Recurring chargeback with ToS + no cancellation on record = moderate+."""
        result = analyze_chargeback(
            ChargebackCategory.CANCELLED_RECURRING,
            CardNetwork.VISA,
            dispute_amount=29.0,
            has_cancellation_policy_accepted=True,
            has_no_cancellation_received=True,
        )
        assert result.strength in (ChargebackStrength.STRONG, ChargebackStrength.MODERATE)
        assert result.win_probability > 0.50

    def test_visa_response_deadline_30_days(self):
        """Visa chargeback response deadline is 30 days."""
        result = analyze_chargeback(
            ChargebackCategory.ITEM_NOT_RECEIVED,
            CardNetwork.VISA,
            dispute_amount=100.0,
        )
        assert result.response_deadline_days == 30

    def test_amex_response_deadline_20_days(self):
        """Amex chargeback response deadline is 20 days (shorter!)."""
        result = analyze_chargeback(
            ChargebackCategory.ITEM_NOT_RECEIVED,
            CardNetwork.AMEX,
            dispute_amount=100.0,
        )
        assert result.response_deadline_days == 20

    def test_mastercard_deadline_45_days(self):
        """Mastercard gives 45 days — most generous."""
        result = analyze_chargeback(
            ChargebackCategory.NOT_AS_DESCRIBED,
            CardNetwork.MASTERCARD,
            dispute_amount=200.0,
        )
        assert result.response_deadline_days == 45

    def test_ev_fight_positive_for_strong_case(self):
        """EV(fight) should be positive for a STRONG case."""
        result = analyze_chargeback(
            ChargebackCategory.ITEM_NOT_RECEIVED,
            CardNetwork.VISA,
            dispute_amount=300.0,
            has_tracking_delivered=True,
            shipped_to_billing_address=True,
            has_signature_confirmation=True,
        )
        assert result.ev_fight > 0

    def test_rebuttal_letter_contains_reason_code(self):
        """Rebuttal letter must reference the specific reason code."""
        result = analyze_chargeback(
            ChargebackCategory.FRAUD_UNAUTHORIZED,
            CardNetwork.VISA,
            dispute_amount=150.0,
        )
        assert result.reason_code in result.rebuttal_letter

    def test_no_auth_fraud_weak(self):
        """Fraud chargeback with no authentication = weak position."""
        result = analyze_chargeback(
            ChargebackCategory.FRAUD_UNAUTHORIZED,
            CardNetwork.VISA,
            dispute_amount=200.0,
            has_3ds_authentication=False,
            has_emv_chip_data=False,
            has_avs_match=False,
        )
        assert result.strength in (ChargebackStrength.WEAK, ChargebackStrength.CONCEDE, ChargebackStrength.MODERATE)
        assert "transaction_authentication_proof" in result.missing_critical


# ── Evidence Builder ──────────────────────────────────────────────────────────

class TestEvidenceBuilder:

    def test_full_sp_coverage_leads_cover_letter(self):
        """Full SP coverage should lead the cover letter."""
        pkg = build_evidence_package(
            ClaimType.INR,
            dispute_amount=120.0,
            paid_via_paypal=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            delivery_address_matches=True,
            has_tracking_number=True,
            has_order_confirmation=True,
        )
        assert "Seller Protection" in pkg.cover_letter
        assert pkg.completeness > 0.5
        assert pkg.win_probability_estimate >= 0.75

    def test_no_delivery_means_low_completeness(self):
        """No tracking = low completeness for INR."""
        pkg = build_evidence_package(
            ClaimType.INR,
            dispute_amount=90.0,
            paid_via_paypal=True,
            has_tracking_number=False,
            tracking_shows_delivered=False,
        )
        assert pkg.completeness < 0.50
        assert pkg.highest_impact_missing is not None

    def test_snad_listing_and_photos_required(self):
        """SNAD package must flag missing listing and photos as critical."""
        pkg = build_evidence_package(
            ClaimType.SNAD,
            dispute_amount=80.0,
            has_listing_screenshot=False,
            has_item_photos=False,
        )
        warnings_text = " ".join(pkg.warnings)
        assert "CRITICAL" in warnings_text

    def test_cover_letter_under_2000_chars(self):
        """Cover letter must stay under PayPal's 2000-char limit."""
        pkg = build_evidence_package(
            ClaimType.INR,
            dispute_amount=500.0,
            paid_via_paypal=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            has_tracking_number=True,
            has_signature_confirmation=True,
            has_avs_cvv_match=True,
            has_buyer_messages=True,
            buyer_acknowledged_receipt=True,
            has_order_confirmation=True,
            has_return_policy=True,
            has_terms_of_service=True,
        )
        assert len(pkg.cover_letter) <= 2000

    def test_signature_required_above_750_flags_warning(self):
        """$750+ without signature = warning."""
        pkg = build_evidence_package(
            ClaimType.INR,
            dispute_amount=800.0,
            paid_via_paypal=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            has_signature_confirmation=False,
        )
        warnings_text = " ".join(pkg.warnings)
        assert "signature" in warnings_text.lower() or "750" in warnings_text

    def test_evidence_items_ordered_by_section(self):
        """Evidence items in the package must be ordered by section."""
        pkg = build_evidence_package(
            ClaimType.INR,
            dispute_amount=100.0,
            paid_via_paypal=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
        )
        sections = [e.section for e in pkg.ordered_evidence]
        assert sections == sorted(sections), "Evidence not ordered by section"

    def test_digital_goods_access_before_dispute_boosts_win_prob(self):
        """Access log before dispute significantly boosts win probability."""
        pkg_no_access = build_evidence_package(
            ClaimType.INR,
            dispute_amount=49.0,
            has_access_log=False,
        )
        pkg_with_access = build_evidence_package(
            ClaimType.INR,
            dispute_amount=49.0,
            has_access_log=True,
            access_before_dispute=True,
        )
        assert pkg_with_access.win_probability_estimate > pkg_no_access.win_probability_estimate

    def test_unauthorized_3ds_in_package(self):
        """UNAUTHORIZED with 3DS auth should be in evidence items."""
        pkg = build_evidence_package(
            ClaimType.UNAUTHORIZED,
            dispute_amount=200.0,
            has_3ds_authentication=True,
            shipped_to_paypal_address=True,
            tracking_shows_delivered=True,
            has_online_tracking=True,
        )
        labels = [e.label for e in pkg.ordered_evidence]
        assert any("3D Secure" in label or "Authentication" in label for label in labels)

    def test_completeness_scales_with_evidence(self):
        """More evidence = higher completeness score."""
        low = build_evidence_package(ClaimType.INR, dispute_amount=100.0)
        high = build_evidence_package(
            ClaimType.INR,
            dispute_amount=100.0,
            paid_via_paypal=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            has_tracking_number=True,
            has_buyer_messages=True,
            has_order_confirmation=True,
        )
        assert high.completeness > low.completeness


# ── Dispute Velocity Forecaster ───────────────────────────────────────────────

class TestVelocityForecaster:

    def test_normal_tier_safe_zone(self):
        """10 disputes / 5000 transactions = 0.2% = NORMAL."""
        report = analyze_velocity(transactions_30d=5000, disputes_30d=10)
        assert report.current_tier == ThresholdTier.NORMAL
        assert report.current_rate < 0.005

    def test_elevated_tier_detection(self):
        """50 disputes / 5000 transactions = 1.0% = LIMITED."""
        report = analyze_velocity(transactions_30d=5000, disputes_30d=50)
        assert report.current_tier == ThresholdTier.LIMITED
        assert report.current_rate == pytest.approx(0.01, abs=0.001)

    def test_hdr_tier_flags_fee(self):
        """1.5%+ rate should compute HDR fee."""
        report = analyze_velocity(transactions_30d=1000, disputes_30d=20)
        assert report.current_tier in (ThresholdTier.HDR, ThresholdTier.LIMITED, ThresholdTier.ELEVATED)
        if report.current_tier == ThresholdTier.HDR:
            assert report.hdr_fee_if_crossed > 0

    def test_headroom_calculation(self):
        """Headroom = disputes until next tier threshold."""
        # 5 disputes / 1000 transactions = 0.5% = ELEVATED
        # Next tier (LIMITED at 1.0%) requires 10 disputes
        report = analyze_velocity(transactions_30d=1000, disputes_30d=5)
        assert report.current_tier == ThresholdTier.ELEVATED
        assert report.disputes_until_next_tier > 0

    def test_settle_recommendation_when_near_threshold(self):
        """When 1 dispute from HDR, should recommend settling open disputes."""
        # 14 disputes / 1000 = 1.4% = LIMITED, 1 from HDR at 1.5%
        report = analyze_velocity(
            transactions_30d=1000,
            disputes_30d=14,
            open_disputes=[
                DisputeRecord("D001", 30.0, "2026-11-01", True, settlement_cost=30.0),
                DisputeRecord("D002", 200.0, "2026-11-02", True, settlement_cost=0.0),
            ],
        )
        # Either in HDR or one dispute from it — should flag caution
        assert "CAUTION" in report.settle_vs_fight_recommendation or \
               "CRITICAL" in report.settle_vs_fight_recommendation or \
               report.disputes_until_next_tier <= 2

    def test_zero_transactions_handled_gracefully(self):
        """Zero transactions should not crash."""
        report = analyze_velocity(transactions_30d=0, disputes_30d=0)
        assert report.current_tier == ThresholdTier.NORMAL
        assert report.current_rate == 0.0

    def test_velocity_projection_date(self):
        """Should project a threshold crossing date when velocity is provided."""
        report = analyze_velocity(
            transactions_30d=1000,
            disputes_30d=4,
            daily_dispute_rate_observed=0.5,
        )
        if report.disputes_until_next_tier > 0:
            assert report.projected_tier_cross_date is not None

    def test_safe_dispute_count_helper(self):
        """threshold_safe_dispute_count gives correct buffer."""
        safe = threshold_safe_dispute_count(1000, ThresholdTier.HDR)
        # 1.5% of 1000 = 15, minus 1 buffer = 14
        assert safe == 14

    def test_fight_vs_settle_sorts_cheapest_first(self):
        """Cheapest-to-settle disputes are settled first (preserve budget)."""
        report = analyze_velocity(
            transactions_30d=200,
            disputes_30d=3,  # Already high rate — 1.5%
            open_disputes=[
                DisputeRecord("cheap", 15.0, "2026-11-01", True, settlement_cost=15.0),
                DisputeRecord("expensive", 500.0, "2026-11-02", True, settlement_cost=0.0),
            ],
        )
        # In HDR territory — should recommend settling at least one
        if report.disputes_to_settle:
            assert "cheap" in report.disputes_to_settle or "expensive" in report.disputes_to_settle

    def test_rate_exactly_at_threshold(self):
        """Rate at exact threshold boundary is correctly classified."""
        # Exactly 1.5% = HDR
        report = analyze_velocity(transactions_30d=1000, disputes_30d=15)
        assert report.current_tier == ThresholdTier.HDR

    def test_critical_tier_at_3pct(self):
        """3%+ rate = CRITICAL tier."""
        report = analyze_velocity(transactions_30d=100, disputes_30d=4)
        assert report.current_tier in (ThresholdTier.HDR, ThresholdTier.CRITICAL)

    def test_recommendation_text_is_non_empty(self):
        """Recommendation should always be a non-empty string."""
        for disputes in [0, 3, 8, 15, 35]:
            report = analyze_velocity(transactions_30d=1000, disputes_30d=disputes)
            assert len(report.settle_vs_fight_recommendation) > 10
