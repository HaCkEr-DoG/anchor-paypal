"""
Tests for Wave 49: subscription_cancellation_analyzer, partial_fulfillment_evaluator.
"""
import pytest
from oracle.subscription_cancellation_analyzer import (
    analyze_subscription_cancellation, SubscriptionEvidenceInput,
    NoticeQuality, CancellationOutcome, SubscriptionDefense,
)
from oracle.partial_fulfillment_evaluator import (
    evaluate_partial_fulfillment, PartialFulfillmentInput,
    ConditionMismatch, FulfillmentVerdict,
)

BASE_TS = 1700000000
DAY = 86400


# ═══════════════════════════════════════════════════════════════════
# Subscription Cancellation Analyzer Tests
# ═══════════════════════════════════════════════════════════════════

class TestSubscriptionCancellationAnalyzer:

    def _inp(self, **kw) -> SubscriptionEvidenceInput:
        defaults = dict(
            dispute_id="D-001",
            transaction_amount=50.0,
            reason_code="SUBSCRIPTION",
        )
        defaults.update(kw)
        return SubscriptionEvidenceInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = analyze_subscription_cancellation(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_win_rate_adjustment_has_value(self):
        r = analyze_subscription_cancellation(self._inp())
        assert isinstance(r.win_rate_adjustment, float)

    def test_recommendation_non_empty(self):
        r = analyze_subscription_cancellation(self._inp())
        assert len(r.recommendation) > 0

    # ── Indefensible cases ────────────────────────────────────────

    def test_indefensible_on_cancelled_before_billing(self):
        r = analyze_subscription_cancellation(self._inp(
            cancellation_outcome=CancellationOutcome.CANCELLED_BEFORE_BILLING,
        ))
        assert r.defense_quality == SubscriptionDefense.INDEFENSIBLE
        assert r.win_rate_adjustment <= -0.20

    def test_indefensible_on_cancellation_not_processed(self):
        r = analyze_subscription_cancellation(self._inp(
            cancellation_outcome=CancellationOutcome.CANCELLATION_NOT_PROCESSED,
        ))
        assert r.defense_quality == SubscriptionDefense.INDEFENSIBLE

    def test_indefensible_recommendation_says_refund(self):
        r = analyze_subscription_cancellation(self._inp(
            cancellation_outcome=CancellationOutcome.CANCELLED_BEFORE_BILLING,
        ))
        assert "refund" in r.recommendation.lower()

    # ── Strong defense ────────────────────────────────────────────

    def test_strong_defense_with_excellent_notice_and_terms(self):
        r = analyze_subscription_cancellation(self._inp(
            notice_quality=NoticeQuality.EXCELLENT,
            notice_sent_days_before_billing=10,
            notice_delivery_confirmed=True,
            cancellation_outcome=CancellationOutcome.NO_CANCELLATION_REQUESTED,
            terms_accepted_at_signup=True,
            recurring_billing_disclosed=True,
        ))
        assert r.defense_quality == SubscriptionDefense.STRONG
        assert r.win_rate_adjustment > 0

    def test_strong_positive_win_adjustment(self):
        r = analyze_subscription_cancellation(self._inp(
            notice_quality=NoticeQuality.EXCELLENT,
            cancellation_outcome=CancellationOutcome.NO_CANCELLATION_REQUESTED,
            terms_accepted_at_signup=True,
        ))
        if r.defense_quality == SubscriptionDefense.STRONG:
            assert r.win_rate_adjustment >= 0.05

    # ── Weak defense ──────────────────────────────────────────────

    def test_weak_defense_on_no_notice(self):
        r = analyze_subscription_cancellation(self._inp(
            notice_quality=NoticeQuality.NONE,
        ))
        assert r.defense_quality in (SubscriptionDefense.WEAK, SubscriptionDefense.INDEFENSIBLE)
        assert r.win_rate_adjustment < 0

    def test_weak_defense_has_required_evidence(self):
        r = analyze_subscription_cancellation(self._inp(
            notice_quality=NoticeQuality.NONE,
        ))
        assert len(r.required_evidence) >= 1

    # ── Notice quality ────────────────────────────────────────────

    def test_excellent_notice_in_strengths(self):
        r = analyze_subscription_cancellation(self._inp(
            notice_quality=NoticeQuality.EXCELLENT,
        ))
        assert any("notice" in s.lower() or "advance" in s.lower() for s in r.strengths)

    def test_poor_notice_in_weaknesses(self):
        r = analyze_subscription_cancellation(self._inp(
            notice_quality=NoticeQuality.POOR,
        ))
        assert any("notice" in w.lower() for w in r.weaknesses)

    # ── Prior renewals ────────────────────────────────────────────

    def test_prior_renewals_appear_in_strengths(self):
        r = analyze_subscription_cancellation(self._inp(
            prior_renewals_accepted=3,
        ))
        renewal_strength = any("prior" in s.lower() or "renewal" in s.lower() for s in r.strengths)
        assert renewal_strength

    # ── Terms acceptance ──────────────────────────────────────────

    def test_terms_acceptance_boosts_strengths(self):
        without = analyze_subscription_cancellation(self._inp(terms_accepted_at_signup=False))
        with_t   = analyze_subscription_cancellation(self._inp(terms_accepted_at_signup=True))
        assert len(with_t.strengths) > len(without.strengths)

    # ── Cancel after billing ──────────────────────────────────────

    def test_cancelled_after_billing_is_legitimate(self):
        r = analyze_subscription_cancellation(self._inp(
            cancellation_outcome=CancellationOutcome.CANCELLED_AFTER_BILLING,
            billing_epoch=BASE_TS,
            cancellation_request_epoch=BASE_TS + DAY,
            notice_quality=NoticeQuality.ADEQUATE,
            terms_accepted_at_signup=True,
        ))
        assert r.defense_quality in (SubscriptionDefense.STRONG, SubscriptionDefense.ADEQUATE)


# ═══════════════════════════════════════════════════════════════════
# Partial Fulfillment Evaluator Tests
# ═══════════════════════════════════════════════════════════════════

class TestPartialFulfillmentEvaluator:

    def _inp(self, amount=100.0, **kw) -> PartialFulfillmentInput:
        defaults = dict(
            dispute_id="D-001",
            transaction_amount=amount,
            reason_code="SNAD",
        )
        defaults.update(kw)
        return PartialFulfillmentInput(**defaults)

    # ── Basic output ──────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = evaluate_partial_fulfillment(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_score_bounded_0_100(self):
        r = evaluate_partial_fulfillment(self._inp())
        assert 0 <= r.total_score <= 100

    def test_refund_amount_bounded(self):
        r = evaluate_partial_fulfillment(self._inp(amount=100.0))
        assert 0 <= r.recommended_refund_amount <= 100.0

    def test_dimensions_non_empty(self):
        r = evaluate_partial_fulfillment(self._inp())
        assert len(r.dimensions) >= 3

    # ── Safety issue ──────────────────────────────────────────────

    def test_safety_issue_full_refund(self):
        r = evaluate_partial_fulfillment(self._inp(safety_issue=True))
        assert r.fight_recommendation == "FULL_REFUND"
        assert r.recommended_refund_pct == 1.0

    # ── Quantity dimension ────────────────────────────────────────

    def test_full_delivery_qty_score_max(self):
        r = evaluate_partial_fulfillment(self._inp(ordered_quantity=3, delivered_quantity=3))
        qty_dim = next(d for d in r.dimensions if "Quantity" in d.dimension)
        assert qty_dim.score == qty_dim.max_score

    def test_partial_delivery_qty_score_prorated(self):
        r = evaluate_partial_fulfillment(self._inp(ordered_quantity=4, delivered_quantity=2))
        qty_dim = next(d for d in r.dimensions if "Quantity" in d.dimension)
        assert qty_dim.score == 15  # 30 * 2/4

    def test_zero_delivery_verdict_not_as_described(self):
        r = evaluate_partial_fulfillment(self._inp(ordered_quantity=1, delivered_quantity=0))
        assert r.verdict == FulfillmentVerdict.NOT_AS_DESCRIBED

    # ── Condition dimension ───────────────────────────────────────

    def test_no_condition_mismatch_full_score(self):
        r = evaluate_partial_fulfillment(self._inp(
            condition_mismatch=ConditionMismatch.NONE,
        ))
        cond_dim = next(d for d in r.dimensions if "Condition" in d.dimension)
        assert cond_dim.score == cond_dim.max_score

    def test_counterfeit_scores_zero(self):
        r = evaluate_partial_fulfillment(self._inp(
            condition_mismatch=ConditionMismatch.COUNTERFEIT,
        ))
        cond_dim = next(d for d in r.dimensions if "Condition" in d.dimension)
        assert cond_dim.score == 0

    def test_counterfeit_verdict_not_as_described(self):
        r = evaluate_partial_fulfillment(self._inp(
            condition_mismatch=ConditionMismatch.COUNTERFEIT,
        ))
        assert r.verdict == FulfillmentVerdict.NOT_AS_DESCRIBED

    # ── Specification dimension ───────────────────────────────────

    def test_wrong_variant_reduces_spec_score(self):
        without = evaluate_partial_fulfillment(self._inp(wrong_variant_sent=False))
        with_wv  = evaluate_partial_fulfillment(self._inp(wrong_variant_sent=True))
        without_s = next(d.score for d in without.dimensions if "Specification" in d.dimension)
        with_s    = next(d.score for d in with_wv.dimensions if "Specification" in d.dimension)
        assert with_s < without_s

    def test_missing_accessories_reduces_spec_score(self):
        without = evaluate_partial_fulfillment(self._inp(missing_accessories=False))
        with_ma  = evaluate_partial_fulfillment(self._inp(missing_accessories=True))
        without_s = next(d.score for d in without.dimensions if "Specification" in d.dimension)
        with_s    = next(d.score for d in with_ma.dimensions if "Specification" in d.dimension)
        assert with_s < without_s

    # ── Functionality ─────────────────────────────────────────────

    def test_non_functional_item_reduces_score(self):
        functional = evaluate_partial_fulfillment(self._inp(item_functional=True))
        non_func   = evaluate_partial_fulfillment(self._inp(item_functional=False))
        assert functional.total_score > non_func.total_score

    def test_partial_functionality_between_full_and_none(self):
        full_func = evaluate_partial_fulfillment(self._inp(
            item_functional=True, partial_functionality=False))
        partial   = evaluate_partial_fulfillment(self._inp(
            item_functional=False, partial_functionality=True))
        none_func = evaluate_partial_fulfillment(self._inp(
            item_functional=False, partial_functionality=False))
        f_dim = next(d.score for d in full_func.dimensions if "Functionality" in d.dimension)
        p_dim = next(d.score for d in partial.dimensions if "Functionality" in d.dimension)
        n_dim = next(d.score for d in none_func.dimensions if "Functionality" in d.dimension)
        assert f_dim >= p_dim >= n_dim

    # ── Verdict and fight recommendation ─────────────────────────

    def test_as_described_recommends_fight(self):
        r = evaluate_partial_fulfillment(self._inp(
            condition_mismatch=ConditionMismatch.NONE,
            advertised_specs_match=True,
            wrong_variant_sent=False,
            missing_accessories=False,
            item_functional=True,
        ))
        assert r.verdict == FulfillmentVerdict.AS_DESCRIBED
        assert r.fight_recommendation == "FIGHT"
        assert r.recommended_refund_pct == 0.0

    def test_not_as_described_recommends_full_refund(self):
        r = evaluate_partial_fulfillment(self._inp(
            condition_mismatch=ConditionMismatch.COUNTERFEIT,
        ))
        assert r.fight_recommendation == "FULL_REFUND"
        assert r.recommended_refund_pct == pytest.approx(1.0)

    # ── Refund amounts ────────────────────────────────────────────

    def test_refund_plus_keep_equals_amount(self):
        r = evaluate_partial_fulfillment(self._inp(amount=100.0))
        assert r.recommended_refund_amount + r.defensible_keep_amount == pytest.approx(100.0, abs=0.1)

    # ── Buyer evidence ────────────────────────────────────────────

    def test_buyer_photos_noted(self):
        r = evaluate_partial_fulfillment(self._inp(
            buyer_has_photos=True,
            condition_mismatch=ConditionMismatch.MODERATE,
        ))
        assert any("photo" in e.lower() for e in r.buyer_evidence_noted)

    def test_buyer_photos_reduce_win_adj(self):
        without = evaluate_partial_fulfillment(self._inp(
            condition_mismatch=ConditionMismatch.MODERATE, buyer_has_photos=False))
        with_ph  = evaluate_partial_fulfillment(self._inp(
            condition_mismatch=ConditionMismatch.MODERATE, buyer_has_photos=True))
        assert with_ph.win_rate_adjustment <= without.win_rate_adjustment
