"""Wave 77 — Evidence Gap Detector tests."""
import pytest
from oracle.evidence_gap_detector import (
    GapSeverity,
    EvidenceGapInput,
    detect_evidence_gaps,
)


def _inp(
    reason_code="INR",
    submitted=None,
    deadline_hours=120.0,
    amount=299.0,
):
    return EvidenceGapInput(
        dispute_id="PP-D-GAP",
        reason_code=reason_code,
        submitted_evidence_keys=submitted or [],
        deadline_hours_remaining=deadline_hours,
        transaction_amount=amount,
    )


# ─── Coverage basics ──────────────────────────────────────────────────────────

class TestCoverage:
    def test_no_evidence_full_gap(self):
        r = detect_evidence_gaps(_inp(submitted=[]))
        assert r.total_gap_score == pytest.approx(1.0, abs=0.001)
        assert r.coverage_score == pytest.approx(0.0, abs=0.001)

    def test_full_evidence_zero_gap(self):
        full = ["tracking_number", "delivery_scan", "carrier_name",
                "ship_date_proof", "order_confirmation"]
        r = detect_evidence_gaps(_inp(submitted=full))
        assert r.total_gap_score == pytest.approx(0.0, abs=0.001)
        assert r.coverage_score == pytest.approx(1.0, abs=0.001)
        assert len(r.all_gaps) == 0

    def test_partial_evidence_partial_gap(self):
        r = detect_evidence_gaps(_inp(submitted=["tracking_number", "delivery_scan"]))
        # tracking=0.35, delivery=0.25 covered → remaining = 0.40
        assert abs(r.total_gap_score - 0.40) < 0.001

    def test_coverage_plus_gap_equals_one(self):
        r = detect_evidence_gaps(_inp(submitted=["tracking_number"]))
        assert abs(r.total_gap_score + r.coverage_score - 1.0) < 0.001


# ─── Submission readiness ─────────────────────────────────────────────────────

class TestSubmissionReadiness:
    def test_full_evidence_is_ready(self):
        full = ["tracking_number", "delivery_scan", "carrier_name",
                "ship_date_proof", "order_confirmation"]
        r = detect_evidence_gaps(_inp(submitted=full))
        assert r.is_submission_ready is True

    def test_no_evidence_not_ready(self):
        r = detect_evidence_gaps(_inp(submitted=[]))
        assert r.is_submission_ready is False

    def test_minor_gap_is_ready(self):
        # Only missing order_confirmation (0.10) → gap = 0.10 < 0.20
        most = ["tracking_number", "delivery_scan", "carrier_name", "ship_date_proof"]
        r = detect_evidence_gaps(_inp(submitted=most))
        assert r.is_submission_ready is True

    def test_major_gap_not_ready(self):
        # Only have tracking (0.35 covered) → 0.65 gap
        r = detect_evidence_gaps(_inp(submitted=["tracking_number"]))
        assert r.is_submission_ready is False


# ─── Gap severity classification ─────────────────────────────────────────────

class TestGapSeverity:
    def test_tracking_number_critical(self):
        # tracking_number weight = 0.35 → CRITICAL
        r = detect_evidence_gaps(_inp(submitted=[]))
        tracking_gap = next(g for g in r.all_gaps if g.evidence_key == "tracking_number")
        assert tracking_gap.severity == GapSeverity.CRITICAL

    def test_order_confirmation_minor(self):
        # order_confirmation weight = 0.10 → MINOR
        r = detect_evidence_gaps(_inp(submitted=[]))
        oc_gap = next(g for g in r.all_gaps if g.evidence_key == "order_confirmation")
        assert oc_gap.severity == GapSeverity.MINOR

    def test_carrier_name_major(self):
        # carrier_name weight = 0.15 → MAJOR
        r = detect_evidence_gaps(_inp(submitted=[]))
        cn_gap = next(g for g in r.all_gaps if g.evidence_key == "carrier_name")
        assert cn_gap.severity == GapSeverity.MAJOR


# ─── Gaps sorted correctly ────────────────────────────────────────────────────

class TestGapSorting:
    def test_gaps_sorted_by_weight_descending(self):
        r = detect_evidence_gaps(_inp(submitted=[]))
        weights = [g.weight for g in r.all_gaps]
        assert weights == sorted(weights, reverse=True)

    def test_critical_gaps_subset_of_all_gaps(self):
        r = detect_evidence_gaps(_inp(submitted=[]))
        for cg in r.critical_gaps:
            assert cg in r.all_gaps


# ─── Win probability cost ─────────────────────────────────────────────────────

class TestWinProbabilityCost:
    def test_cost_is_half_weight(self):
        r = detect_evidence_gaps(_inp(submitted=[]))
        for g in r.all_gaps:
            assert abs(g.win_probability_cost - g.weight * 0.50) < 0.001

    def test_no_gaps_zero_total_cost(self):
        full = ["tracking_number", "delivery_scan", "carrier_name",
                "ship_date_proof", "order_confirmation"]
        r = detect_evidence_gaps(_inp(submitted=full))
        assert sum(g.win_probability_cost for g in r.all_gaps) == 0.0


# ─── Reason code coverage ────────────────────────────────────────────────────

class TestReasonCodeCoverage:
    def test_snad_evidence_set(self):
        r = detect_evidence_gaps(_inp(reason_code="SNAD", submitted=[]))
        keys = {g.evidence_key for g in r.all_gaps}
        assert "product_photos" in keys
        assert "listing_screenshot" in keys

    def test_unauthorized_evidence_set(self):
        r = detect_evidence_gaps(_inp(reason_code="UNAUTHORIZED", submitted=[]))
        keys = {g.evidence_key for g in r.all_gaps}
        assert "avs_cvv_match" in keys

    def test_subscription_evidence_set(self):
        r = detect_evidence_gaps(_inp(reason_code="SUBSCRIPTION", submitted=[]))
        keys = {g.evidence_key for g in r.all_gaps}
        assert "signup_proof" in keys

    def test_unknown_reason_code_uses_generic(self):
        r = detect_evidence_gaps(_inp(reason_code="UNKNOWN_CODE", submitted=[]))
        assert len(r.all_gaps) > 0

    def test_case_insensitive_submitted_keys(self):
        # Submit with uppercase — should match lowercase evidence keys
        r = detect_evidence_gaps(_inp(submitted=["TRACKING_NUMBER", "DELIVERY_SCAN"]))
        keys = {g.evidence_key for g in r.all_gaps}
        assert "tracking_number" not in keys
        assert "delivery_scan" not in keys


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_dispute_id_echoed(self):
        r = detect_evidence_gaps(_inp())
        assert r.dispute_id == "PP-D-GAP"

    def test_reason_code_normalized_to_upper(self):
        r = detect_evidence_gaps(_inp(reason_code="inr"))
        assert r.reason_code == "INR"

    def test_recommendation_non_empty(self):
        r = detect_evidence_gaps(_inp(submitted=[]))
        assert len(r.close_first_recommendation) > 20

    def test_paypal_signal_contains_dispute_id(self):
        r = detect_evidence_gaps(_inp())
        assert "PP-D-GAP" in r.paypal_signal

    def test_action_descriptions_set(self):
        r = detect_evidence_gaps(_inp(submitted=[]))
        for g in r.all_gaps:
            assert len(g.action_description) > 10
