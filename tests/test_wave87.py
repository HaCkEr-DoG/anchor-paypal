"""Wave 87 — Evidence Assembly Scorer tests."""
import pytest
from oracle.evidence_assembly_scorer import (
    EvidenceCategory, EvidenceItem, ScoredEvidenceItem,
    EvidenceGap, EvidenceAssemblyReport,
    score_evidence, _weights_for,
)


def _item(item_id="E-001", category=EvidenceCategory.TRACKING,
          description="tracking info", quality=1.0, file_name=None):
    return EvidenceItem(
        item_id=item_id,
        category=category,
        description=description,
        quality=quality,
        file_name=file_name,
    )


# ─── Weight lookups ───────────────────────────────────────────────────────────

class TestWeights:
    def test_inr_tracking_highest(self):
        w = _weights_for("INR")
        assert w[EvidenceCategory.TRACKING] == max(w.values())

    def test_snad_photo_highest(self):
        w = _weights_for("SNAD")
        assert w[EvidenceCategory.PHOTO] == max(w.values())

    def test_unauth_comms_highest(self):
        w = _weights_for("UNAUTH")
        assert w[EvidenceCategory.COMMS] == max(w.values())

    def test_other_all_equal(self):
        w = _weights_for("OTHER")
        vals = list(w.values())
        assert all(abs(v - vals[0]) < 1e-9 for v in vals)

    def test_unknown_claim_falls_to_other(self):
        assert _weights_for("ZZZZ") == _weights_for("OTHER")

    def test_case_insensitive(self):
        assert _weights_for("inr") == _weights_for("INR")


# ─── Coverage score ───────────────────────────────────────────────────────────

class TestCoverageScore:
    def test_no_items_zero_coverage(self):
        r = score_evidence("D-1", "INR", [])
        assert r.coverage_score == 0.0

    def test_perfect_full_set_inr(self):
        items = [
            _item("e1", EvidenceCategory.TRACKING, quality=1.0),
            _item("e2", EvidenceCategory.DELIVERY, quality=1.0),
            _item("e3", EvidenceCategory.COMMS,    quality=1.0),
            _item("e4", EvidenceCategory.PHOTO,    quality=1.0),
            _item("e5", EvidenceCategory.POLICY,   quality=1.0),
        ]
        r = score_evidence("D-1", "INR", items)
        assert r.coverage_score == 1.0

    def test_partial_coverage_between_zero_one(self):
        r = score_evidence("D-1", "INR", [_item("e1", EvidenceCategory.TRACKING)])
        assert 0.0 < r.coverage_score < 1.0

    def test_duplicate_category_keeps_best(self):
        items = [
            _item("e1", EvidenceCategory.TRACKING, quality=0.3),
            _item("e2", EvidenceCategory.TRACKING, quality=0.9),
        ]
        r = score_evidence("D-1", "INR", items)
        assert len(r.items) == 1
        assert r.items[0].quality == 0.9

    def test_low_quality_reduces_coverage(self):
        items_full = [
            _item(f"e{i}", cat, quality=1.0)
            for i, cat in enumerate(EvidenceCategory)
        ]
        items_low = [
            _item(f"e{i}", cat, quality=0.3)
            for i, cat in enumerate(EvidenceCategory)
        ]
        r_full = score_evidence("D-1", "SNAD", items_full)
        r_low  = score_evidence("D-1", "SNAD", items_low)
        assert r_full.coverage_score > r_low.coverage_score


# ─── Submission ranking ───────────────────────────────────────────────────────

class TestSubmissionRanking:
    def test_highest_contribution_ranked_first(self):
        # For SNAD: PHOTO weight=0.40 > POLICY=0.25; both quality=1.0
        items = [
            _item("photo", EvidenceCategory.PHOTO,  quality=1.0),
            _item("pol",   EvidenceCategory.POLICY, quality=1.0),
        ]
        r = score_evidence("D-1", "SNAD", items)
        assert r.items[0].category == EvidenceCategory.PHOTO

    def test_ranks_sequential(self):
        items = [_item(f"e{i}", cat) for i, cat in enumerate(EvidenceCategory)]
        r = score_evidence("D-1", "INR", items)
        assert [s.submission_rank for s in r.items] == list(range(1, len(r.items) + 1))

    def test_contribution_equals_weight_times_quality(self):
        items = [_item("e1", EvidenceCategory.TRACKING, quality=0.7)]
        r = score_evidence("D-1", "INR", items)
        s = r.items[0]
        expected = round(s.weight * 0.7, 4)
        assert abs(s.contribution - expected) < 1e-9


# ─── Gaps ─────────────────────────────────────────────────────────────────────

class TestGaps:
    def test_missing_category_is_a_gap(self):
        # Provide only TRACKING for INR — DELIVERY, COMMS, PHOTO, POLICY are missing
        r = score_evidence("D-1", "INR", [_item("e1", EvidenceCategory.TRACKING)])
        gap_cats = [g.category for g in r.gaps]
        assert EvidenceCategory.DELIVERY in gap_cats

    def test_no_gaps_when_perfect(self):
        items = [
            _item(f"e{i}", cat, quality=1.0)
            for i, cat in enumerate(EvidenceCategory)
        ]
        r = score_evidence("D-1", "INR", items)
        assert len(r.gaps) == 0

    def test_low_quality_item_creates_gap(self):
        items = [_item("e1", EvidenceCategory.PHOTO, quality=0.2)]
        r = score_evidence("D-1", "SNAD", items)
        gap_cats = [g.category for g in r.gaps]
        assert EvidenceCategory.PHOTO in gap_cats

    def test_gap_has_suggestion(self):
        r = score_evidence("D-1", "INR", [])
        assert all(len(g.suggestion) > 10 for g in r.gaps)


# ─── Sufficient to fight ──────────────────────────────────────────────────────

class TestSufficientToFight:
    def test_full_set_sufficient(self):
        items = [
            _item(f"e{i}", cat, quality=1.0)
            for i, cat in enumerate(EvidenceCategory)
        ]
        r = score_evidence("D-1", "INR", items)
        assert r.sufficient_to_fight is True

    def test_empty_not_sufficient(self):
        r = score_evidence("D-1", "INR", [])
        assert r.sufficient_to_fight is False

    def test_threshold_at_60_pct(self):
        # For INR: TRACKING(0.40) + DELIVERY(0.30) = 0.70 total → coverage 0.70 ≥ 0.60
        items = [
            _item("t", EvidenceCategory.TRACKING, quality=1.0),
            _item("d", EvidenceCategory.DELIVERY, quality=1.0),
        ]
        r = score_evidence("D-1", "INR", items)
        assert r.sufficient_to_fight is True


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_dispute_id_preserved(self):
        r = score_evidence("DISP-999", "SNAD", [])
        assert r.dispute_id == "DISP-999"

    def test_claim_type_normalised_uppercase(self):
        r = score_evidence("D-1", "snad", [])
        assert r.claim_type == "SNAD"

    def test_recommendation_non_empty(self):
        r = score_evidence("D-1", "INR", [_item()])
        assert len(r.overall_recommendation) > 10

    def test_strong_recommendation_on_high_coverage(self):
        items = [
            _item(f"e{i}", cat, quality=1.0)
            for i, cat in enumerate(EvidenceCategory)
        ]
        r = score_evidence("D-1", "INR", items)
        assert "strong" in r.overall_recommendation.lower()

    def test_insufficient_recommendation_on_low_coverage(self):
        r = score_evidence("D-1", "INR", [])
        assert "insufficient" in r.overall_recommendation.lower()
