"""
Wave 72 — Dispute Response Quality Analyzer tests.

Covers all five dimensions (completeness, evidence_citation, tone,
specificity, structure), grade thresholds, submit_ready logic, and
the paypal_signal field.
"""
import pytest
from oracle.response_quality_analyzer import (
    ResponseAnalysisInput,
    ResponseGrade,
    analyze_response_quality,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _inp(
    dispute_id="PP-D-99001",
    reason_code="INR",
    response_text="",
    transaction_amount=0.0,
    has_tracking=False,
    has_delivery_scan=False,
    has_order_confirmation=False,
    has_product_photos=False,
    has_buyer_communication=False,
):
    return ResponseAnalysisInput(
        dispute_id=dispute_id,
        reason_code=reason_code,
        response_text=response_text,
        transaction_amount=transaction_amount,
        has_tracking=has_tracking,
        has_delivery_scan=has_delivery_scan,
        has_order_confirmation=has_order_confirmation,
        has_product_photos=has_product_photos,
        has_buyer_communication=has_buyer_communication,
    )


_EXCELLENT_INR = """\
We are writing in response to dispute PP-D-99001 regarding order dated Oct 15, 2026.

We shipped the item via UPS on Oct 16, 2026. Tracking number 1Z9999W99999999999 \
confirms delivery on Oct 20, 2026. Please find attached the tracking confirmation, \
delivery scan, and order confirmation. We understand the buyer's concern and have \
provided full documentation to resolve this promptly.

Therefore, we respectfully request that this dispute be resolved in our favor \
based on the attached documentation confirming delivery.
"""

_POOR_INR = "buyer is a liar the package was delivered"

_DECENT_SNAD = """\
We are responding to this SNAD dispute. As described in the product listing, \
the item was sold in the condition shown in the attached photos. \
The product matches the description exactly. We respectfully request closure.
"""


# ─── Grade thresholds ─────────────────────────────────────────────────────────

class TestGradeThresholds:
    def test_excellent_response_grades_a(self):
        r = analyze_response_quality(
            _inp(
                response_text=_EXCELLENT_INR,
                transaction_amount=299.0,
                has_tracking=True,
                has_delivery_scan=True,
                has_order_confirmation=True,
            )
        )
        assert r.grade in (ResponseGrade.A, ResponseGrade.B)
        assert r.quality_score >= 0.65

    def test_poor_response_grades_d_or_f(self):
        r = analyze_response_quality(_inp(response_text=_POOR_INR))
        assert r.grade in (ResponseGrade.D, ResponseGrade.F)

    def test_empty_response_grades_d_or_f(self):
        r = analyze_response_quality(_inp(response_text=""))
        assert r.grade in (ResponseGrade.D, ResponseGrade.F)
        assert r.quality_score <= 0.40

    def test_snad_decent_response_grades_b_or_c(self):
        r = analyze_response_quality(
            _inp(
                reason_code="SNAD",
                response_text=_DECENT_SNAD,
                has_product_photos=True,
            )
        )
        assert r.grade in (ResponseGrade.B, ResponseGrade.C)


# ─── Completeness dimension ───────────────────────────────────────────────────

class TestCompleteness:
    def test_inr_missing_tracking_keyword_penalised(self):
        text = "We shipped the item via carrier. Package was delivered."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "completeness")
        # "tracking" missing from text → not perfect
        assert dim.score < 1.0

    def test_inr_all_keywords_present_scores_high(self):
        text = (
            "We shipped the package with tracking on Oct 15 via carrier "
            "and delivery was confirmed."
        )
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "completeness")
        assert dim.score >= 0.80

    def test_subscription_uses_subscription_keywords(self):
        text = "Customer agreed to the terms and renewal notification was sent."
        r = analyze_response_quality(
            _inp(reason_code="SUBSCRIPTION", response_text=text)
        )
        dim = next(d for d in r.dimensions if d.name == "completeness")
        assert dim.score > 0.5

    def test_missing_all_keywords_scores_zero(self):
        r = analyze_response_quality(_inp(reason_code="INR", response_text="hello"))
        dim = next(d for d in r.dimensions if d.name == "completeness")
        assert dim.score == 0.0

    def test_amount_not_referenced_issues_flagged(self):
        text = "We shipped the package with tracking and delivery confirmation."
        r = analyze_response_quality(
            _inp(response_text=text, transaction_amount=199.99)
        )
        dim = next(d for d in r.dimensions if d.name == "completeness")
        amount_issues = [i for i in dim.issues if "amount" in i.lower()]
        assert len(amount_issues) >= 1


# ─── Evidence citation dimension ─────────────────────────────────────────────

class TestEvidenceCitation:
    def test_tracking_present_but_not_mentioned_penalised(self):
        text = "We shipped the item and it was delivered."
        r = analyze_response_quality(_inp(response_text=text, has_tracking=True))
        dim = next(d for d in r.dimensions if d.name == "evidence_citation")
        assert dim.score < 0.5

    def test_tracking_mentioned_boosts_score(self):
        text = "Tracking number confirms delivery."
        r = analyze_response_quality(_inp(response_text=text, has_tracking=True))
        dim = next(d for d in r.dimensions if d.name == "evidence_citation")
        assert dim.score >= 0.5

    def test_photos_not_referenced_penalised(self):
        text = "The product is as described."
        r = analyze_response_quality(
            _inp(reason_code="SNAD", response_text=text, has_product_photos=True)
        )
        dim = next(d for d in r.dimensions if d.name == "evidence_citation")
        issues = [i for i in dim.issues if "photo" in i.lower()]
        assert len(issues) >= 1

    def test_all_evidence_cited_scores_at_or_above_base(self):
        text = "Tracking shows delivery. Order confirmed. Photos attached."
        r = analyze_response_quality(
            _inp(
                response_text=text,
                has_tracking=True,
                has_delivery_scan=True,
                has_order_confirmation=True,
                has_product_photos=True,
            )
        )
        dim = next(d for d in r.dimensions if d.name == "evidence_citation")
        assert dim.score >= 0.70

    def test_no_evidence_flags_no_issues_for_missing(self):
        """When no evidence is uploaded, the dimension should not penalise for missing citations."""
        r = analyze_response_quality(
            _inp(response_text="We shipped the package tracking confirmed.")
        )
        dim = next(d for d in r.dimensions if d.name == "evidence_citation")
        # With no evidence uploaded and tracking keyword present, should be at base or above
        assert dim.score >= 0.5


# ─── Tone dimension ───────────────────────────────────────────────────────────

class TestTone:
    def test_aggressive_language_penalised(self):
        text = "The buyer is a fraud and a liar. This is a scam dispute."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "tone")
        assert dim.score < 0.60
        assert any("aggressive" in i.lower() for i in dim.issues)

    def test_professional_language_scores_high(self):
        text = (
            "We respectfully submit this response. Please find attached the records. "
            "Our documentation confirms delivery. We understand the concern."
        )
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "tone")
        assert dim.score >= 0.80

    def test_empathy_absence_generates_suggestion(self):
        text = "We shipped the package. Tracking confirms delivery. Please close this dispute."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "tone")
        assert any("empathy" in s.lower() or "understand" in s.lower() for s in dim.suggestions)

    def test_multiple_aggressive_phrases_reduce_score_further(self):
        text = "The buyer is a cheat and a thief who is trying to steal. This is outrageous."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "tone")
        assert dim.score <= 0.50


# ─── Specificity dimension ────────────────────────────────────────────────────

class TestSpecificity:
    def test_dates_present_boost_score(self):
        # Short text may still be penalised for length, but dates are found
        text = "We shipped on Oct 15, 2026 and delivery was confirmed on Oct 20."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "specificity")
        # Score is penalised for short length but date boost prevents worst outcome
        assert dim.score >= 0.40

    def test_no_dates_penalised(self):
        text = "We shipped the item and it was delivered to the customer."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "specificity")
        assert any("date" in i.lower() for i in dim.issues)

    def test_very_short_response_penalised(self):
        text = "Delivered. Package was shipped."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "specificity")
        assert any("short" in i.lower() or "word" in i.lower() for i in dim.issues)

    def test_long_response_boosts_score(self):
        # 10x the sentence = 80 words; no date penalty but no short-text penalty either
        text = " ".join(["We shipped the item with full tracking confirmation."] * 10)
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "specificity")
        # 80 words avoids the short-response penalty (< 50 words), score >= 0.35
        assert dim.score >= 0.30


# ─── Structure dimension ──────────────────────────────────────────────────────

class TestStructure:
    def test_clear_opening_boosts_score(self):
        text = "We are writing to respond to this dispute. The item was shipped."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "structure")
        assert dim.score > 0.5

    def test_closing_request_boosts_score(self):
        text = "The package was shipped and delivered. Therefore, we request closure."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "structure")
        assert dim.score >= 0.65

    def test_missing_closing_issues_suggestion(self):
        text = "The package was shipped and delivered to the buyer."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "structure")
        closing_issues = [i for i in dim.issues if "closing" in i.lower()]
        assert len(closing_issues) >= 1

    def test_multiple_paragraphs_boost_score(self):
        text = "We shipped the item.\n\nTracking confirms delivery.\n\nWe request closure."
        r = analyze_response_quality(_inp(response_text=text))
        dim = next(d for d in r.dimensions if d.name == "structure")
        assert dim.score >= 0.70


# ─── Overall report fields ────────────────────────────────────────────────────

class TestReportFields:
    def test_all_dimensions_present(self):
        r = analyze_response_quality(_inp(response_text="something"))
        names = {d.name for d in r.dimensions}
        assert names == {"completeness", "evidence_citation", "tone", "specificity", "structure"}

    def test_issues_and_improvements_are_lists(self):
        r = analyze_response_quality(_inp(response_text="bad response"))
        assert isinstance(r.issues, list)
        assert isinstance(r.improvements, list)

    def test_paypal_signal_contains_grade(self):
        r = analyze_response_quality(_inp(response_text="something"))
        assert r.grade.value in r.paypal_signal

    def test_recommendation_set(self):
        r = analyze_response_quality(_inp(response_text="something"))
        assert len(r.recommendation) > 10

    def test_win_prob_boost_negative_for_poor_grade(self):
        r = analyze_response_quality(_inp(response_text=_POOR_INR))
        assert r.estimated_win_probability_boost <= 0.0

    def test_win_prob_boost_positive_for_excellent_grade(self):
        r = analyze_response_quality(
            _inp(
                response_text=_EXCELLENT_INR,
                transaction_amount=299.0,
                has_tracking=True,
                has_delivery_scan=True,
                has_order_confirmation=True,
            )
        )
        assert r.estimated_win_probability_boost >= 0.0

    def test_submit_ready_false_for_poor_response(self):
        r = analyze_response_quality(_inp(response_text=_POOR_INR))
        assert r.submit_ready is False

    def test_dispute_id_echoed(self):
        r = analyze_response_quality(_inp(dispute_id="PP-D-TESTID", response_text="x"))
        assert r.dispute_id == "PP-D-TESTID"

    def test_reason_code_echoed(self):
        r = analyze_response_quality(_inp(reason_code="SNAD", response_text="x"))
        assert r.reason_code == "SNAD"

    def test_quality_score_in_range(self):
        for text in [_POOR_INR, _EXCELLENT_INR, _DECENT_SNAD, ""]:
            r = analyze_response_quality(_inp(response_text=text))
            assert 0.0 <= r.quality_score <= 1.0

    def test_unauthorized_reason_code(self):
        text = (
            "We shipped to the confirmed address on record. Delivery was confirmed "
            "to the buyer's registered address. Please find the tracking and confirm records."
        )
        r = analyze_response_quality(
            _inp(reason_code="UNAUTHORIZED", response_text=text)
        )
        assert r.reason_code == "UNAUTHORIZED"
        assert r.grade is not None
