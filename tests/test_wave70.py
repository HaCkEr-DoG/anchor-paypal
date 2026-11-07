"""
Wave 70 — Evidence Strength Scorer
Tests: 26 total
"""

import pytest
from oracle.evidence_strength_scorer import (
    score_evidence,
    EvidencePackage,
    StrengthTier,
    EvidenceItemStatus,
)


def _full_inr(**kwargs):
    defaults = dict(
        dispute_id="D001",
        reason_code="INR",
        transaction_amount=200.0,
        has_tracking_number=True,
        has_delivery_scan=True,
        has_order_confirmation=True,
        has_buyer_communication=True,
        has_carrier_confirmation_letter=True,
        has_signature_proof=False,
    )
    defaults.update(kwargs)
    return EvidencePackage(**defaults)


def _bare_inr(**kwargs):
    defaults = dict(
        dispute_id="D002",
        reason_code="INR",
        transaction_amount=200.0,
    )
    defaults.update(kwargs)
    return EvidencePackage(**defaults)


def _full_unauth(**kwargs):
    defaults = dict(
        dispute_id="D003",
        reason_code="UNAUTHORIZED",
        transaction_amount=300.0,
        has_address_match_proof=True,
        has_delivery_scan=True,
        has_fraud_prevention_evidence=True,
        has_ip_address_match=True,
        has_tracking_number=True,
    )
    defaults.update(kwargs)
    return EvidencePackage(**defaults)


class TestEvidenceStrengthScorer:

    # ─── Basic structure ──────────────────────────────────────────────────────

    def test_returns_report(self):
        r = score_evidence(_full_inr())
        assert r is not None

    def test_score_between_0_and_1(self):
        r = score_evidence(_full_inr())
        assert 0.0 <= r.overall_score <= 1.0

    def test_items_list_not_empty(self):
        r = score_evidence(_full_inr())
        assert len(r.items) > 0

    # ─── INR — strong evidence ────────────────────────────────────────────────

    def test_inr_full_evidence_strong(self):
        r = score_evidence(_full_inr())
        assert r.strength_tier == StrengthTier.STRONG

    def test_inr_full_submit_ready(self):
        r = score_evidence(_full_inr())
        assert r.submit_ready is True

    def test_inr_full_win_prob_high(self):
        r = score_evidence(_full_inr())
        assert r.estimated_win_probability >= 0.70

    def test_inr_full_no_missing_critical(self):
        r = score_evidence(_full_inr())
        assert len(r.missing_critical) == 0

    # ─── INR — insufficient evidence ─────────────────────────────────────────

    def test_inr_bare_insufficient(self):
        r = score_evidence(_bare_inr())
        assert r.strength_tier in (StrengthTier.INSUFFICIENT, StrengthTier.WEAK)

    def test_inr_bare_not_submit_ready(self):
        r = score_evidence(_bare_inr())
        assert r.submit_ready is False

    def test_inr_bare_has_missing_critical(self):
        r = score_evidence(_bare_inr())
        assert len(r.missing_critical) >= 1

    def test_inr_bare_win_prob_low(self):
        r = score_evidence(_bare_inr())
        assert r.estimated_win_probability < 0.50

    # ─── INR — high value signature ───────────────────────────────────────────

    def test_inr_750_plus_signature_critical(self):
        pkg = _full_inr(transaction_amount=800.0, has_signature_proof=False)
        r = score_evidence(pkg)
        # Signature is critical for $750+, so it should appear in missing_critical
        assert any("signature" in m.lower() or "750" in m for m in r.missing_critical)

    def test_inr_750_plus_with_signature_strong(self):
        pkg = _full_inr(transaction_amount=800.0, has_signature_proof=True)
        r = score_evidence(pkg)
        assert r.strength_tier == StrengthTier.STRONG

    # ─── SNAD scoring ─────────────────────────────────────────────────────────

    def test_snad_full_evidence_strong(self):
        pkg = EvidencePackage(
            dispute_id="D004",
            reason_code="SNAD",
            transaction_amount=150.0,
            has_product_photos=True,
            has_item_description_match=True,
            has_order_confirmation=True,
            has_buyer_communication=True,
        )
        r = score_evidence(pkg)
        assert r.strength_tier in (StrengthTier.STRONG, StrengthTier.ADEQUATE)

    def test_snad_no_photos_weak(self):
        pkg = EvidencePackage(
            dispute_id="D005",
            reason_code="SNAD",
            transaction_amount=150.0,
            has_order_confirmation=True,
        )
        r = score_evidence(pkg)
        assert r.strength_tier in (StrengthTier.WEAK, StrengthTier.INSUFFICIENT)

    # ─── UNAUTHORIZED scoring ─────────────────────────────────────────────────

    def test_unauthorized_full_strong(self):
        r = score_evidence(_full_unauth())
        assert r.strength_tier == StrengthTier.STRONG

    def test_unauthorized_missing_address_match_weak(self):
        r = score_evidence(_full_unauth(has_address_match_proof=False))
        assert r.strength_tier in (StrengthTier.ADEQUATE, StrengthTier.WEAK)

    # ─── SUBSCRIPTION scoring ─────────────────────────────────────────────────

    def test_subscription_full_evidence_adequate(self):
        pkg = EvidencePackage(
            dispute_id="D006",
            reason_code="SUBSCRIPTION",
            transaction_amount=50.0,
            has_billing_notification=True,
            has_terms_acceptance=True,
            has_prior_renewal_history=True,
        )
        r = score_evidence(pkg)
        assert r.strength_tier in (StrengthTier.STRONG, StrengthTier.ADEQUATE)

    def test_subscription_no_terms_has_missing_critical(self):
        pkg = EvidencePackage(
            dispute_id="D007",
            reason_code="SUBSCRIPTION",
            transaction_amount=50.0,
            has_billing_notification=True,
        )
        r = score_evidence(pkg)
        assert len(r.missing_critical) >= 1

    # ─── Score ordering ───────────────────────────────────────────────────────

    def test_full_evidence_beats_bare_evidence(self):
        full = score_evidence(_full_inr())
        bare = score_evidence(_bare_inr())
        assert full.overall_score > bare.overall_score

    # ─── PayPal signal ────────────────────────────────────────────────────────

    def test_paypal_signal_populated(self):
        r = score_evidence(_full_inr())
        assert len(r.paypal_signal) > 20

    def test_insufficient_signal_mentions_dispute_id(self):
        r = score_evidence(_bare_inr())
        assert "D002" in r.paypal_signal

    # ─── Recommendation ───────────────────────────────────────────────────────

    def test_recommendation_populated(self):
        r = score_evidence(_full_inr())
        assert len(r.recommendation) > 20

    def test_insufficient_recommendation_says_do_not_submit(self):
        r = score_evidence(_bare_inr())
        assert "SUBMIT" in r.recommendation.upper() or "gather" in r.recommendation.lower()
