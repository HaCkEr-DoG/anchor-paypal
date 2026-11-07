"""Wave 83 — Merchant Response Quality Scorer tests."""
import pytest
from oracle.merchant_response_quality import (
    ResponseGrade,
    ResponseQualityInput,
    score_response,
)


def _inp(
    dispute_id="PP-D-RESP",
    reason="INR",
    has_tracking=True,
    has_delivery=True,
    has_comms=True,
    has_policy=True,
    prod_completeness=0.80,
    has_photo=True,
    word_count=250,
    days_deadline=10,
):
    return ResponseQualityInput(
        dispute_id=dispute_id,
        dispute_reason=reason,
        has_tracking_number=has_tracking,
        has_delivery_confirmation=has_delivery,
        has_buyer_communications=has_comms,
        has_policy_citation=has_policy,
        product_description_completeness=prod_completeness,
        has_photo_or_video_evidence=has_photo,
        response_word_count=word_count,
        days_until_deadline=days_deadline,
    )


# ─── Dimension scores ─────────────────────────────────────────────────────────

class TestDimensionScores:
    def test_full_tracking_scores_1(self):
        r = score_response(_inp(has_tracking=True, has_delivery=True))
        assert r.tracking_score == 1.0

    def test_tracking_only_scores_half(self):
        r = score_response(_inp(has_tracking=True, has_delivery=False))
        assert r.tracking_score == 0.5

    def test_no_tracking_scores_zero(self):
        r = score_response(_inp(has_tracking=False, has_delivery=False))
        assert r.tracking_score == 0.0

    def test_communications_binary(self):
        r_yes = score_response(_inp(has_comms=True))
        r_no  = score_response(_inp(has_comms=False))
        assert r_yes.communication_score == 1.0
        assert r_no.communication_score == 0.0

    def test_policy_binary(self):
        r_yes = score_response(_inp(has_policy=True))
        r_no  = score_response(_inp(has_policy=False))
        assert r_yes.policy_score == 1.0
        assert r_no.policy_score == 0.0

    def test_photo_binary(self):
        r_yes = score_response(_inp(has_photo=True))
        r_no  = score_response(_inp(has_photo=False))
        assert r_yes.photo_score == 1.0
        assert r_no.photo_score == 0.0

    def test_product_completeness_clamped(self):
        r = score_response(_inp(prod_completeness=1.5))
        assert r.product_score <= 1.0
        r2 = score_response(_inp(prod_completeness=-0.5))
        assert r2.product_score >= 0.0


# ─── Overall score and grade ──────────────────────────────────────────────────

class TestOverallGrade:
    def test_perfect_response_strong(self):
        r = score_response(_inp(word_count=350))
        assert r.overall_score >= 0.75
        assert r.grade == ResponseGrade.STRONG

    def test_all_missing_weak(self):
        r = score_response(_inp(
            has_tracking=False, has_delivery=False, has_comms=False,
            has_policy=False, prod_completeness=0.0, has_photo=False,
            word_count=50,
        ))
        assert r.grade == ResponseGrade.WEAK

    def test_partial_evidence_adequate(self):
        r = score_response(_inp(
            has_tracking=True, has_delivery=False, has_comms=True,
            has_policy=False, prod_completeness=0.60, has_photo=False,
            word_count=200,
        ))
        assert r.grade in (ResponseGrade.ADEQUATE, ResponseGrade.WEAK)

    def test_overall_score_range(self):
        r = score_response(_inp())
        assert 0.0 <= r.overall_score <= 1.0


# ─── Win probability ──────────────────────────────────────────────────────────

class TestWinProbability:
    def test_strong_inr_high_win_prob(self):
        r = score_response(_inp(reason="INR", word_count=350))
        assert r.estimated_win_probability > 0.70

    def test_unauth_always_low(self):
        r = score_response(_inp(reason="UNAUTH", word_count=350))
        assert r.estimated_win_probability < 0.50

    def test_weak_response_lowers_win_prob(self):
        strong = score_response(_inp())
        weak   = score_response(_inp(
            has_tracking=False, has_delivery=False, has_comms=False,
            has_policy=False, prod_completeness=0.0, has_photo=False,
            word_count=50,
        ))
        assert strong.estimated_win_probability > weak.estimated_win_probability

    def test_win_prob_capped_at_95(self):
        r = score_response(_inp(reason="INR", word_count=500, prod_completeness=1.0))
        assert r.estimated_win_probability <= 0.95


# ─── Missing items and recommendations ───────────────────────────────────────

class TestMissingAndRecs:
    def test_no_missing_when_complete(self):
        r = score_response(_inp(prod_completeness=0.80, word_count=350))
        assert len(r.missing_items) == 0

    def test_missing_tracking_in_list(self):
        r = score_response(_inp(has_tracking=False))
        assert any("Tracking" in m for m in r.missing_items)

    def test_missing_comms_in_list(self):
        r = score_response(_inp(has_comms=False))
        assert any("communication" in m.lower() for m in r.missing_items)

    def test_urgent_prefix_when_deadline_close(self):
        r = score_response(_inp(
            has_tracking=False, has_delivery=False, has_comms=False,
            has_policy=False, prod_completeness=0.0, has_photo=False,
            days_deadline=1, word_count=50,
        ))
        assert any("URGENT" in rec for rec in r.recommendations)

    def test_no_urgent_when_plenty_of_time(self):
        r = score_response(_inp(days_deadline=30))
        assert not any("URGENT" in rec for rec in r.recommendations)


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_dispute_id_echoed(self):
        r = score_response(_inp())
        assert r.dispute_id == "PP-D-RESP"

    def test_paypal_signal_contains_dispute_id(self):
        r = score_response(_inp())
        assert "PP-D-RESP" in r.paypal_signal

    def test_paypal_signal_contains_grade(self):
        r = score_response(_inp())
        assert r.grade.value in r.paypal_signal

    def test_all_score_fields_floats(self):
        r = score_response(_inp())
        for attr in ("tracking_score", "communication_score", "policy_score",
                     "product_score", "photo_score", "overall_score"):
            assert isinstance(getattr(r, attr), float)
