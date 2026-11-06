"""
Tests for Wave 54: response_quality_scorer, dispute_category_classifier.
"""
import pytest
from oracle.response_quality_scorer import (
    score_response_quality, ResponseDraftInput,
    ResponseQualityTier,
)
from oracle.dispute_category_classifier import (
    classify_dispute, DisputeClassifierInput,
    DisputeCategory, FirstAction, SellerProtectionResult,
)


# ═══════════════════════════════════════════════════════════════════
# Response Quality Scorer Tests
# ═══════════════════════════════════════════════════════════════════

class TestResponseQualityScorer:

    def _inp(self, reason="INR", network="VISA", **kw) -> ResponseDraftInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code=reason,
            network=network,
        )
        defaults.update(kw)
        return ResponseDraftInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = score_response_quality(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_five_dimensions(self):
        r = score_response_quality(self._inp())
        assert len(r.dimensions) == 5

    def test_score_bounded(self):
        r = score_response_quality(self._inp())
        assert 0 <= r.total_score <= 100

    def test_dim_scores_sum_to_total(self):
        r = score_response_quality(self._inp())
        assert sum(d.score for d in r.dimensions) == r.total_score

    # ── Quality tier thresholds ───────────────────────────────────

    def test_excellent_tier_at_high_score(self):
        r = score_response_quality(self._inp(
            evidence_items_attached=5, required_evidence_count=3,
            has_tracking_evidence=True, has_auth_evidence=True,
            has_policy_evidence=True, has_communication_log=True,
            dates_consistent=True, amounts_consistent=True,
            tracking_numbers_valid=True, buyer_identity_confirmed=True,
            reason_code_addressed=True, has_counter_narrative=True,
            cites_network_rules=True, has_supporting_precedent=True,
            has_cover_letter=True, has_evidence_index=True,
            meets_page_limit=True, follows_network_template=True,
            tone_professional=True, no_threats_or_demands=True,
            concise_under_1000_words=True,
        ))
        assert r.quality_tier in (ResponseQualityTier.EXCELLENT, ResponseQualityTier.GOOD)

    def test_inadequate_tier_on_minimal_response(self):
        r = score_response_quality(self._inp(
            evidence_items_attached=0, required_evidence_count=5,
            reason_code_addressed=False,
            tone_professional=False,
        ))
        assert r.quality_tier in (ResponseQualityTier.INADEQUATE, ResponseQualityTier.WEAK)

    # ── Approval gate ─────────────────────────────────────────────

    def test_approved_when_good_no_blockers(self):
        r = score_response_quality(self._inp(
            evidence_items_attached=4, required_evidence_count=3,
            has_tracking_evidence=True, has_policy_evidence=True,
            reason_code_addressed=True, has_counter_narrative=True,
            has_cover_letter=True, has_evidence_index=True,
            meets_page_limit=True,
        ))
        assert r.approved_to_submit is True

    def test_not_approved_when_blocking_issue(self):
        r = score_response_quality(self._inp(
            reason_code="UNAUTHORIZED",
            has_auth_evidence=False,  # → blocking issue
            reason_code_addressed=True,
        ))
        # Blocking issue (no auth evidence for unauthorized) prevents approval
        assert len(r.blocking_issues) >= 1

    def test_not_approved_with_date_inconsistency(self):
        r = score_response_quality(self._inp(dates_consistent=False))
        assert len(r.blocking_issues) >= 1
        assert r.approved_to_submit is False

    # ── Evidence dimension ────────────────────────────────────────

    def test_full_evidence_scores_higher(self):
        low = score_response_quality(self._inp(evidence_items_attached=0, required_evidence_count=4))
        high = score_response_quality(self._inp(
            evidence_items_attached=4, required_evidence_count=4,
            has_tracking_evidence=True, has_policy_evidence=True,
        ))
        ev_low  = next(d for d in low.dimensions if "evidence" in d.dimension.lower())
        ev_high = next(d for d in high.dimensions if "evidence" in d.dimension.lower())
        assert ev_high.score > ev_low.score

    def test_tracking_evidence_adds_to_strengths(self):
        r = score_response_quality(self._inp(has_tracking_evidence=True))
        assert any("racking" in s for s in r.strengths)

    # ── Argumentation dimension ───────────────────────────────────

    def test_reason_code_not_addressed_is_blocking(self):
        r = score_response_quality(self._inp(reason_code_addressed=False))
        assert any("reason code" in b.lower() for b in r.blocking_issues)

    def test_cites_network_rules_adds_strength(self):
        r = score_response_quality(self._inp(cites_network_rules=True))
        assert any("ule" in s for s in r.strengths)

    # ── Format dimension ─────────────────────────────────────────

    def test_cover_letter_adds_to_format(self):
        with_cl = score_response_quality(self._inp(has_cover_letter=True, has_evidence_index=True, meets_page_limit=True))
        without = score_response_quality(self._inp(has_cover_letter=False))
        fmt_with = next(d for d in with_cl.dimensions if "format" in d.dimension.lower())
        fmt_w    = next(d for d in without.dimensions  if "format" in d.dimension.lower())
        assert fmt_with.score > fmt_w.score

    # ── Tone dimension ────────────────────────────────────────────

    def test_adversarial_tone_is_blocking(self):
        r = score_response_quality(self._inp(no_threats_or_demands=False))
        assert any("threat" in b.lower() or "demand" in b.lower() for b in r.blocking_issues)

    def test_unprofessional_tone_reduces_score(self):
        professional   = score_response_quality(self._inp(tone_professional=True))
        unprofessional = score_response_quality(self._inp(tone_professional=False))
        assert professional.total_score > unprofessional.total_score

    # ── Improvements list ─────────────────────────────────────────

    def test_improvements_non_empty_on_weak_response(self):
        r = score_response_quality(self._inp())
        assert len(r.improvements) >= 1

    def test_no_cover_letter_generates_improvement(self):
        r = score_response_quality(self._inp(has_cover_letter=False))
        assert any("cover" in imp.lower() for imp in r.improvements)


# ═══════════════════════════════════════════════════════════════════
# Dispute Category Classifier Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeCategoryClassifier:

    def _inp(self, reason="INR", network="VISA", amount=150.0, **kw) -> DisputeClassifierInput:
        defaults = dict(
            dispute_id="D-001",
            reason_code=reason,
            network=network,
            transaction_amount=amount,
        )
        defaults.update(kw)
        return DisputeClassifierInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = classify_dispute(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_category_set(self):
        r = classify_dispute(self._inp())
        assert isinstance(r.primary_category, DisputeCategory)

    def test_confidence_bounded(self):
        r = classify_dispute(self._inp())
        assert 0.0 <= r.confidence <= 1.0

    def test_first_action_set(self):
        r = classify_dispute(self._inp())
        assert isinstance(r.first_action, FirstAction)

    # ── Low-value auto-accept ─────────────────────────────────────

    def test_auto_accept_on_tiny_amount(self):
        r = classify_dispute(self._inp(amount=10.0, win_probability_estimate=0.20))
        assert r.primary_category == DisputeCategory.AUTO_ACCEPT
        assert r.first_action == FirstAction.ACCEPT_IMMEDIATELY

    def test_not_auto_accept_on_vip_buyer(self):
        # VIP override: never auto-accept
        r = classify_dispute(self._inp(amount=10.0, ltv_tier="VIP", win_probability_estimate=0.20))
        assert r.primary_category != DisputeCategory.AUTO_ACCEPT

    # ── Seller Protection ─────────────────────────────────────────

    def test_sp_eligible_for_inr_with_tracking(self):
        r = classify_dispute(self._inp(
            reason="INR",
            has_tracking_with_delivery=True,
            has_seller_protection=True,
        ))
        assert r.seller_protection == SellerProtectionResult.ELIGIBLE

    def test_sp_ineligible_for_snad(self):
        r = classify_dispute(self._inp(reason="SNAD"))
        assert r.seller_protection == SellerProtectionResult.INELIGIBLE

    def test_sp_partial_missing_signature_on_high_value(self):
        r = classify_dispute(self._inp(
            reason="INR",
            amount=1000.0,
            has_tracking_with_delivery=True,
            has_signature_confirmation=False,
            has_seller_protection=True,
        ))
        assert r.seller_protection == SellerProtectionResult.PARTIAL
        assert "signature" in r.sp_missing_requirement.lower()

    def test_sp_category_on_eligible(self):
        r = classify_dispute(self._inp(
            reason="INR",
            amount=200.0,
            has_tracking_with_delivery=True,
            has_seller_protection=True,
        ))
        assert r.primary_category == DisputeCategory.SP_ELIGIBLE
        assert r.first_action == FirstAction.FILE_SP_CLAIM

    # ── CE3.0 ─────────────────────────────────────────────────────

    def test_ce3_eligible_visa_unauthorized_prior_txns(self):
        r = classify_dispute(self._inp(
            reason="UNAUTHORIZED",
            network="VISA",
            is_prior_ce3_transaction=True,
        ))
        assert r.ce3_eligible is True
        assert r.ce3_boost == pytest.approx(0.18)

    def test_ce3_ineligible_on_non_visa(self):
        r = classify_dispute(self._inp(
            reason="UNAUTHORIZED",
            network="MC",
            is_prior_ce3_transaction=True,
        ))
        assert r.ce3_eligible is False

    def test_ce3_ineligible_on_inr(self):
        r = classify_dispute(self._inp(
            reason="INR",
            network="VISA",
            is_prior_ce3_transaction=True,
        ))
        assert r.ce3_eligible is False

    def test_ce3_category_set_when_eligible(self):
        r = classify_dispute(self._inp(
            reason="UNAUTHORIZED",
            network="VISA",
            is_prior_ce3_transaction=True,
            has_seller_protection=False,
        ))
        assert r.primary_category == DisputeCategory.CE3_CANDIDATE
        assert r.first_action == FirstAction.SUBMIT_CE3_PACKAGE

    # ── INR with tracking ─────────────────────────────────────────

    def test_inr_track_on_delivery_evidence_no_sp(self):
        r = classify_dispute(self._inp(
            reason="INR",
            amount=200.0,
            has_tracking_with_delivery=True,
            has_seller_protection=False,
        ))
        assert r.primary_category in (DisputeCategory.INR_TRACK, DisputeCategory.SP_ELIGIBLE, DisputeCategory.STRONG_FIGHT)

    # ── SNAD accept ───────────────────────────────────────────────

    def test_snad_accept_on_low_evidence(self):
        r = classify_dispute(self._inp(
            reason="SNAD",
            evidence_completeness_score=20,
        ))
        assert r.primary_category in (DisputeCategory.SNAD_ACCEPT, DisputeCategory.AUTO_ACCEPT)

    # ── Strong fight ──────────────────────────────────────────────

    def test_strong_fight_on_high_win_prob_and_evidence(self):
        r = classify_dispute(self._inp(
            reason="INR",
            win_probability_estimate=0.80,
            evidence_completeness_score=85,
            has_seller_protection=False,
        ))
        assert r.primary_category in (DisputeCategory.STRONG_FIGHT, DisputeCategory.INR_TRACK)

    # ── Buyer risk influence ──────────────────────────────────────

    def test_fraudster_buyer_boosts_fight_category(self):
        r = classify_dispute(self._inp(
            reason="UNAUTHORIZED",
            buyer_risk_class="FRAUDSTER",
            buyer_dispute_rate=0.30,
            has_seller_protection=False,
        ))
        assert r.primary_category not in (DisputeCategory.AUTO_ACCEPT, DisputeCategory.SNAD_ACCEPT)

    # ── Deadline urgency ──────────────────────────────────────────

    def test_deadline_flag_at_1_day_left(self):
        r = classify_dispute(self._inp(
            response_window_days=20,
            days_since_dispute_opened=19,
        ))
        assert any("URGENT" in f for f in r.flags)

    # ── Escalation ────────────────────────────────────────────────

    def test_escalation_on_very_high_value(self):
        r = classify_dispute(self._inp(
            amount=5000.0,
            has_seller_protection=False,
            win_probability_estimate=0.40,
        ))
        assert r.primary_category == DisputeCategory.ESCALATION

    # ── Signals list ─────────────────────────────────────────────

    def test_contributing_signals_non_empty_on_actionable_case(self):
        r = classify_dispute(self._inp(
            reason="INR",
            has_tracking_with_delivery=True,
            has_seller_protection=True,
        ))
        assert len(r.contributing_signals) >= 1
