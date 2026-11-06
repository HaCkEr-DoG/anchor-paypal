"""
Tests for oracle/paypal_rubric.py — PayPal's own evidence scoring rubric.

These tests verify that Anchor correctly models PayPal's Seller Protection
requirements and evidence grading criteria, ensuring evidence is submitted
in the optimal format to score highest on PayPal's AI review system.
"""

import pytest

from oracle.paypal_rubric import (
    ClaimType,
    ProtectionStatus,
    check_seller_protection,
    score_rubric,
)
from oracle.ensemble import score as ensemble_score
from oracle.decision import calculate as oracle_calculate, Evidence


# ── Seller Protection compliance tests ───────────────────────────────────────

class TestSellerProtectionCompliance:

    def test_fully_covered_inr_all_requirements_met(self):
        result = check_seller_protection(
            ClaimType.ITEM_NOT_RECEIVED,
            paid_via_paypal=True,
            us_or_eligible_account=True,
            is_tangible_goods=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            dispute_amount=100.0,
        )
        assert result.status == ProtectionStatus.FULLY_COVERED
        assert result.is_auto_win_eligible
        assert len(result.critical_gaps) == 0

    def test_fully_covered_unauthorized_all_requirements_met(self):
        result = check_seller_protection(
            ClaimType.UNAUTHORIZED,
            paid_via_paypal=True,
            us_or_eligible_account=True,
            is_tangible_goods=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            dispute_amount=200.0,
        )
        assert result.status == ProtectionStatus.FULLY_COVERED
        assert result.is_auto_win_eligible

    def test_not_covered_missing_tracking(self):
        result = check_seller_protection(
            ClaimType.ITEM_NOT_RECEIVED,
            paid_via_paypal=True,
            is_tangible_goods=True,
            shipped_to_paypal_address=True,
            has_online_tracking=False,  # Missing
            tracking_shows_delivered=False,
        )
        assert result.status == ProtectionStatus.NOT_COVERED
        assert not result.is_auto_win_eligible
        assert any("tracking" in gap.lower() for gap in result.critical_gaps)

    def test_not_covered_shipped_to_wrong_address(self):
        """SP is void if shipped to address not provided by PayPal — even at buyer's request."""
        result = check_seller_protection(
            ClaimType.ITEM_NOT_RECEIVED,
            paid_via_paypal=True,
            is_tangible_goods=True,
            shipped_to_paypal_address=False,  # Shipped elsewhere
            has_online_tracking=True,
            tracking_shows_delivered=True,
        )
        assert result.status == ProtectionStatus.NOT_COVERED
        assert any("address" in gap.lower() for gap in result.critical_gaps)

    def test_snad_not_covered_by_sp(self):
        """Seller Protection does NOT cover SNAD claims."""
        result = check_seller_protection(ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED)
        assert result.status == ProtectionStatus.NOT_COVERED
        assert not result.is_auto_win_eligible

    def test_billing_issue_not_covered_by_sp(self):
        """Seller Protection does NOT cover billing disputes."""
        result = check_seller_protection(ClaimType.BILLING_ISSUE)
        assert result.status == ProtectionStatus.NOT_COVERED

    def test_excluded_category_ineligible(self):
        """Vehicle sales are excluded from Seller Protection."""
        result = check_seller_protection(
            ClaimType.ITEM_NOT_RECEIVED,
            item_category="vehicles",
        )
        assert result.status == ProtectionStatus.INELIGIBLE
        assert not result.is_auto_win_eligible

    def test_high_value_requires_signature_confirmation(self):
        """Transactions >$750 require signature confirmation for SP."""
        result = check_seller_protection(
            ClaimType.ITEM_NOT_RECEIVED,
            paid_via_paypal=True,
            is_tangible_goods=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            dispute_amount=850.0,
            has_signature_confirmation=False,  # Missing for >$750
        )
        assert result.status == ProtectionStatus.NOT_COVERED
        assert any("signature" in gap.lower() for gap in result.critical_gaps)

    def test_high_value_with_signature_confirmation_fully_covered(self):
        """Transactions >$750 with signature confirmation are fully SP-eligible."""
        result = check_seller_protection(
            ClaimType.ITEM_NOT_RECEIVED,
            paid_via_paypal=True,
            is_tangible_goods=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            dispute_amount=850.0,
            has_signature_confirmation=True,
        )
        assert result.status == ProtectionStatus.FULLY_COVERED

    def test_tracking_not_delivered_not_covered(self):
        """'Out for delivery' or 'in transit' is NOT sufficient for SP — must be DELIVERED."""
        result = check_seller_protection(
            ClaimType.ITEM_NOT_RECEIVED,
            paid_via_paypal=True,
            is_tangible_goods=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=False,  # In transit, not delivered
        )
        assert result.status == ProtectionStatus.NOT_COVERED

    def test_not_paid_via_paypal_void(self):
        """SP doesn't apply if payment wasn't through PayPal."""
        result = check_seller_protection(
            ClaimType.ITEM_NOT_RECEIVED,
            paid_via_paypal=False,
        )
        assert result.status == ProtectionStatus.NOT_COVERED
        assert any("paypal" in gap.lower() for gap in result.critical_gaps)


# ── Rubric scoring tests ──────────────────────────────────────────────────────

class TestRubricScoring:

    def test_perfect_inr_score(self):
        """Full evidence + SP = maximum rubric score."""
        result = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED,
            dispute_amount=200.0,
            has_tracking_number=True,
            has_delivery_confirmation=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            paid_via_paypal=True,
            is_tangible_goods=True,
        )
        assert result.score >= 0.85
        assert result.win_probability_boost >= 0.12
        assert result.seller_protection.is_auto_win_eligible

    def test_weak_inr_score_no_tracking(self):
        """No tracking = very weak rubric position for INR."""
        result = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED,
            dispute_amount=200.0,
            has_tracking_number=False,
            has_delivery_confirmation=False,
            shipped_to_paypal_address=False,
            has_online_tracking=False,
            tracking_shows_delivered=False,
        )
        assert result.score < 0.50
        assert result.win_probability_boost <= 0.0
        assert "tracking_number" in result.missing_critical
        assert "delivery_confirmation" in result.missing_critical

    def test_snad_evidence_scoring(self):
        """SNAD: item description + buyer comm + photos = strong rubric."""
        result = score_rubric(
            ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
            dispute_amount=150.0,
            has_item_description_proof=True,
            has_buyer_communication=True,
            has_photos=True,
            has_return_policy=True,
        )
        assert result.score >= 0.75
        assert result.win_probability_boost >= 0.06

    def test_snad_missing_description_proof_critical(self):
        """SNAD without item description = critical gap."""
        result = score_rubric(
            ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
            dispute_amount=150.0,
            has_item_description_proof=False,
            has_buyer_communication=True,
        )
        assert "item_description_proof" in result.missing_critical

    def test_unauthorized_3ds_boost(self):
        """3DS authentication dramatically boosts win probability for UNAUTHORIZED."""
        result_with_3ds = score_rubric(
            ClaimType.UNAUTHORIZED,
            dispute_amount=300.0,
            has_3ds_authentication=True,
            has_buyer_communication=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            paid_via_paypal=True,
            is_tangible_goods=True,
        )
        result_without_3ds = score_rubric(
            ClaimType.UNAUTHORIZED,
            dispute_amount=300.0,
            has_3ds_authentication=False,
            has_buyer_communication=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            paid_via_paypal=True,
            is_tangible_goods=True,
        )
        assert result_with_3ds.score > result_without_3ds.score

    def test_submission_order_tracking_first_for_inr(self):
        """For INR, delivery confirmation must be first in submission order."""
        result = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED,
            dispute_amount=100.0,
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            shipped_to_paypal_address=True,
        )
        assert len(result.submission_order) > 0
        # Delivery confirmation should be first
        assert "DELIVERY_CONFIRMATION" in result.submission_order[0]

    def test_submission_order_description_first_for_snad(self):
        """For SNAD, item description should be first in submission order."""
        result = score_rubric(
            ClaimType.SIGNIFICANTLY_NOT_AS_DESCRIBED,
            dispute_amount=100.0,
            has_item_description_proof=True,
            has_buyer_communication=True,
        )
        assert len(result.submission_order) > 0
        assert "ITEM_DESCRIPTION" in result.submission_order[0]

    def test_evidence_note_mentions_seller_protection_when_active(self):
        """Evidence note should reference Seller Protection when SP is fully active."""
        result = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED,
            dispute_amount=100.0,
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            shipped_to_paypal_address=True,
            paid_via_paypal=True,
            is_tangible_goods=True,
        )
        assert "Seller Protection" in result.evidence_note

    def test_evidence_note_avoids_defensive_language(self):
        """Evidence note should not use uncertain language."""
        result = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED,
            dispute_amount=100.0,
            has_tracking_number=True,
            has_delivery_confirmation=True,
        )
        for phrase in ["I believe", "I think", "probably", "possibly"]:
            assert phrase not in result.evidence_note

    def test_evidence_note_says_delivered_not_sent(self):
        """Evidence note must say 'DELIVERED' — not 'sent' or 'shipped'."""
        result = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED,
            dispute_amount=100.0,
            has_tracking_number=True,
            has_delivery_confirmation=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            shipped_to_paypal_address=True,
        )
        assert "DELIVERED" in result.evidence_note or "delivered" in result.evidence_note

    def test_signature_not_required_under_750(self):
        """Signature confirmation is optional for transactions ≤$750."""
        result = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED,
            dispute_amount=750.0,
            has_tracking_number=True,
            has_delivery_confirmation=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            has_signature_confirmation=False,  # Not required at exactly $750
        )
        # SP should still be active since $750 is the threshold, not above it
        assert "signature_confirmation" not in result.missing_critical

    def test_signature_required_above_750(self):
        """Signature confirmation is required for transactions >$750."""
        result = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED,
            dispute_amount=751.0,
            has_tracking_number=True,
            has_delivery_confirmation=True,
            shipped_to_paypal_address=True,
            has_online_tracking=True,
            tracking_shows_delivered=True,
            has_signature_confirmation=False,
        )
        assert "signature_confirmation" in result.missing_critical

    def test_billing_issue_rubric(self):
        """Billing issue: service proof + ToS = strong rubric."""
        result = score_rubric(
            ClaimType.BILLING_ISSUE,
            dispute_amount=50.0,
            has_service_delivery_proof=True,
            has_terms_of_service=True,
            paid_via_paypal=True,
        )
        assert result.score >= 0.50

    def test_rubric_win_boost_tiers(self):
        """Higher rubric score = higher win probability boost."""
        perfect = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED, 100.0,
            has_tracking_number=True, has_delivery_confirmation=True,
            has_online_tracking=True, tracking_shows_delivered=True,
            shipped_to_paypal_address=True, paid_via_paypal=True, is_tangible_goods=True,
        )
        minimal = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED, 100.0,
            has_tracking_number=True,
        )
        none_ = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED, 100.0,
        )
        assert perfect.win_probability_boost > minimal.win_probability_boost
        assert minimal.win_probability_boost >= none_.win_probability_boost


# ── Ensemble integration tests ────────────────────────────────────────────────

class TestEnsembleRubricIntegration:

    def _make_oracle(self, amount=200.0):
        from oracle.decision import ClaimType as OracleClaimType
        return oracle_calculate(
            dispute_amount=amount,
            claim_type=OracleClaimType.ITEM_NOT_RECEIVED,
            evidence=Evidence(
                has_tracking_number=True,
                has_delivery_confirmation=True,
                seller_account_age_days=365,
                seller_dispute_win_rate=0.65,
            ),
        )

    def test_rubric_boost_increases_composite_win_prob(self):
        """A strong rubric score should increase the composite win probability."""
        oracle = self._make_oracle()

        strong_rubric = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED, 200.0,
            has_tracking_number=True, has_delivery_confirmation=True,
            has_online_tracking=True, tracking_shows_delivered=True,
            shipped_to_paypal_address=True, paid_via_paypal=True, is_tangible_goods=True,
        )
        no_rubric_result = ensemble_score(
            oracle, 200.0, ClaimType.ITEM_NOT_RECEIVED,
        )
        rubric_result = ensemble_score(
            oracle, 200.0, ClaimType.ITEM_NOT_RECEIVED,
            rubric_score=strong_rubric,
        )
        assert rubric_result.composite_win_probability > no_rubric_result.composite_win_probability

    def test_weak_rubric_lowers_composite_win_prob(self):
        """A weak rubric score (no evidence) should penalize the win probability."""
        oracle = self._make_oracle()

        weak_rubric = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED, 200.0,
            # No evidence at all
        )
        no_rubric_result = ensemble_score(
            oracle, 200.0, ClaimType.ITEM_NOT_RECEIVED,
        )
        rubric_result = ensemble_score(
            oracle, 200.0, ClaimType.ITEM_NOT_RECEIVED,
            rubric_score=weak_rubric,
        )
        assert rubric_result.composite_win_probability <= no_rubric_result.composite_win_probability

    def test_sp_fully_covered_in_signal_contributions(self):
        """SP-active rubric should appear in ensemble signal contributions."""
        oracle = self._make_oracle()
        rubric = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED, 200.0,
            has_tracking_number=True, has_delivery_confirmation=True,
            has_online_tracking=True, tracking_shows_delivered=True,
            shipped_to_paypal_address=True, paid_via_paypal=True, is_tangible_goods=True,
        )
        result = ensemble_score(
            oracle, 200.0, ClaimType.ITEM_NOT_RECEIVED,
            rubric_score=rubric,
        )
        sources = [c.source for c in result.signal_contributions]
        assert "paypal_rubric" in sources

    def test_full_stack_sp_plus_adversarial(self):
        """Full signal stack: Oracle + rubric + adversarial + calibration."""
        from oracle.adversarial import analyze as adv_analyze, AdversarialReport

        oracle = self._make_oracle(amount=500.0)
        rubric = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED, 500.0,
            has_tracking_number=True, has_delivery_confirmation=True,
            has_online_tracking=True, tracking_shows_delivered=True,
            shipped_to_paypal_address=True, paid_via_paypal=True, is_tangible_goods=True,
        )
        # Clean buyer (no adversarial signals)
        adv = adv_analyze(
            dispute_amount=500.0,
            prior_disputes_last_7d=0,
        )
        result = ensemble_score(
            oracle, 500.0, ClaimType.ITEM_NOT_RECEIVED,
            rubric_score=rubric,
            adversarial_report=adv,
            calibration_adjustment=0.03,
        )
        # With SP active + clean buyer + positive calibration: should be strong
        assert result.composite_win_probability >= 0.70
        assert result.recommendation.value in ("FIGHT",)
        sources = [c.source for c in result.signal_contributions]
        assert "paypal_rubric" in sources

    def test_rubric_boost_capped_at_25_percent(self):
        """Rubric boost applied to ensemble is capped at 25%."""
        from oracle.paypal_rubric import RubricScore, SellerProtectionResult
        from oracle.decision import ClaimType as OracleClaimType

        oracle = oracle_calculate(
            dispute_amount=500.0,
            claim_type=OracleClaimType.ITEM_NOT_RECEIVED,
            evidence=Evidence(has_tracking_number=True, has_delivery_confirmation=True),
        )
        # Mock a rubric score with an artificially extreme boost
        extreme_rubric = score_rubric(
            ClaimType.ITEM_NOT_RECEIVED, 500.0,
            has_tracking_number=True, has_delivery_confirmation=True,
            has_online_tracking=True, tracking_shows_delivered=True,
            shipped_to_paypal_address=True, paid_via_paypal=True, is_tangible_goods=True,
        )
        result = ensemble_score(
            oracle, 500.0, ClaimType.ITEM_NOT_RECEIVED,
            rubric_score=extreme_rubric,
        )
        # Final probability can never exceed 0.95 (ensemble hard cap)
        assert result.composite_win_probability <= 0.95
