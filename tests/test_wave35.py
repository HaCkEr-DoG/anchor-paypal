"""
Tests for Wave 35: dispute_clustering_engine, appeal_window_tracker.
"""
import pytest
from oracle.dispute_clustering_engine import (
    cluster_disputes, ClusteringInput, DisputeForClustering,
    EvidenceReadiness, AmountBand,
)
from oracle.appeal_window_tracker import (
    track_appeal_window, AppealWindowInput,
    AppealOutcome, AppealRecommendation,
)


# ═══════════════════════════════════════════════════════════════════
# Dispute Clustering Engine Tests
# ═══════════════════════════════════════════════════════════════════

class TestDisputeClusteringEngine:

    def _d(self, dispute_id="D-001", claim_type="INR", amount=50.0,
           network="PAYPAL", priority_band="P2",
           evidence_readiness=EvidenceReadiness.PARTIAL,
           days_until_deadline=7, **kw) -> DisputeForClustering:
        return DisputeForClustering(
            dispute_id=dispute_id,
            claim_type=claim_type,
            amount=amount,
            network=network,
            priority_band=priority_band,
            evidence_readiness=evidence_readiness,
            days_until_deadline=days_until_deadline,
            **kw,
        )

    def _inp(self, disputes=None, **kw) -> ClusteringInput:
        if disputes is None:
            disputes = [self._d()]
        return ClusteringInput(merchant_id="M-001", disputes=disputes, **kw)

    # ── Basic functionality ───────────────────────────────────────

    def test_empty_batch_ok(self):
        r = cluster_disputes(self._inp(disputes=[]))
        assert r.total_clusters == 0
        assert r.total_disputes == 0

    def test_single_dispute_one_cluster(self):
        r = cluster_disputes(self._inp())
        assert r.total_clusters == 1
        assert r.clusters[0].dispute_count == 1

    def test_identical_disputes_same_cluster(self):
        disputes = [
            self._d("D-1"),
            self._d("D-2"),
            self._d("D-3"),
        ]
        r = cluster_disputes(self._inp(disputes=disputes))
        assert r.total_clusters == 1
        assert r.clusters[0].dispute_count == 3

    def test_different_claim_types_different_clusters(self):
        disputes = [
            self._d("D-1", claim_type="INR"),
            self._d("D-2", claim_type="UNAUTHORIZED"),
        ]
        r = cluster_disputes(self._inp(disputes=disputes))
        assert r.total_clusters == 2

    def test_different_networks_different_clusters(self):
        disputes = [
            self._d("D-1", network="PAYPAL"),
            self._d("D-2", network="VISA"),
        ]
        r = cluster_disputes(self._inp(disputes=disputes))
        assert r.total_clusters == 2

    def test_different_evidence_readiness_different_clusters(self):
        disputes = [
            self._d("D-1", evidence_readiness=EvidenceReadiness.COMPLETE),
            self._d("D-2", evidence_readiness=EvidenceReadiness.NONE),
        ]
        r = cluster_disputes(self._inp(disputes=disputes))
        assert r.total_clusters == 2

    # ── Amount bands ──────────────────────────────────────────────

    def test_micro_band_under_25(self):
        r = cluster_disputes(self._inp(disputes=[self._d(amount=10.0)]))
        assert r.clusters[0].key.amount_band == AmountBand.MICRO

    def test_large_band_over_500(self):
        r = cluster_disputes(self._inp(disputes=[self._d(amount=750.0)]))
        assert r.clusters[0].key.amount_band == AmountBand.LARGE

    def test_small_band_25_to_99(self):
        r = cluster_disputes(self._inp(disputes=[self._d(amount=50.0)]))
        assert r.clusters[0].key.amount_band == AmountBand.SMALL

    # ── Cluster content ───────────────────────────────────────────

    def test_cluster_contains_dispute_ids(self):
        r = cluster_disputes(self._inp(disputes=[self._d("D-XYZ")]))
        assert "D-XYZ" in r.clusters[0].dispute_ids

    def test_cluster_total_amount_sums_correctly(self):
        disputes = [self._d("D-1", amount=50.0), self._d("D-2", amount=50.0)]
        r = cluster_disputes(self._inp(disputes=disputes))
        assert r.clusters[0].total_amount == pytest.approx(100.0)

    def test_template_assigned(self):
        r = cluster_disputes(self._inp())
        assert len(r.clusters[0].template_name) > 0

    def test_shared_actions_non_empty(self):
        r = cluster_disputes(self._inp())
        assert len(r.clusters[0].shared_actions) >= 1

    # ── Time savings ──────────────────────────────────────────────

    def test_clustered_time_less_than_manual(self):
        disputes = [self._d(f"D-{i}") for i in range(6)]
        r = cluster_disputes(self._inp(disputes=disputes))
        assert r.estimated_clustered_minutes < r.estimated_manual_minutes

    def test_time_savings_positive(self):
        disputes = [self._d(f"D-{i}") for i in range(4)]
        r = cluster_disputes(self._inp(disputes=disputes))
        assert r.time_savings_minutes > 0

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_merchant_id(self):
        r = cluster_disputes(self._inp())
        assert "M-001" in r.summary

    def test_total_amount_sums_all(self):
        disputes = [self._d("D-1", amount=25.0), self._d("D-2", amount=75.0, network="VISA")]
        r = cluster_disputes(self._inp(disputes=disputes))
        assert r.total_amount == pytest.approx(100.0)


# ═══════════════════════════════════════════════════════════════════
# Appeal Window Tracker Tests
# ═══════════════════════════════════════════════════════════════════

class TestAppealWindowTracker:

    def _inp(self, **kw) -> AppealWindowInput:
        defaults = dict(
            dispute_id="D-001",
            network="PAYPAL",
            disputed_amount=100.0,
            ruling_outcome=AppealOutcome.LOST_ORIGINAL,
            days_since_ruling=3,
            appeal_cost=25.0,
            chargeback_fee=20.0,
        )
        defaults.update(kw)
        return AppealWindowInput(**defaults)

    # ── Window calculations ───────────────────────────────────────

    def test_paypal_window_10_days(self):
        r = track_appeal_window(self._inp(network="PAYPAL", days_since_ruling=0))
        assert r.appeal_window_days == 10

    def test_visa_window_45_days(self):
        r = track_appeal_window(self._inp(network="VISA", days_since_ruling=0))
        assert r.appeal_window_days == 45

    def test_days_remaining_correct(self):
        r = track_appeal_window(self._inp(network="PAYPAL", days_since_ruling=3))
        assert r.days_remaining == 7

    def test_zero_days_remaining_when_expired(self):
        r = track_appeal_window(self._inp(network="PAYPAL", days_since_ruling=20))
        assert r.days_remaining == 0

    def test_window_expired_flag(self):
        r = track_appeal_window(self._inp(network="PAYPAL", days_since_ruling=20))
        assert r.window_expired is True

    def test_window_not_expired_within_window(self):
        r = track_appeal_window(self._inp(network="PAYPAL", days_since_ruling=5))
        assert r.window_expired is False

    # ── Recommendation ────────────────────────────────────────────

    def test_do_not_appeal_when_won(self):
        r = track_appeal_window(self._inp(
            ruling_outcome=AppealOutcome.WON_ORIGINAL))
        assert r.recommendation == AppealRecommendation.DO_NOT_APPEAL

    def test_window_expired_when_too_late(self):
        r = track_appeal_window(self._inp(
            network="PAYPAL", days_since_ruling=15))
        assert r.recommendation == AppealRecommendation.WINDOW_EXPIRED

    def test_appeal_now_with_new_evidence_and_positive_ev(self):
        r = track_appeal_window(self._inp(
            network="VISA",           # 45-day window
            days_since_ruling=5,
            disputed_amount=200.0,
            has_new_evidence=True,
            appeal_cost=25.0,
        ))
        assert r.recommendation in (
            AppealRecommendation.APPEAL_NOW,
            AppealRecommendation.APPEAL_OPTIONAL,
        )

    def test_do_not_appeal_low_ev(self):
        r = track_appeal_window(self._inp(
            disputed_amount=10.0,    # Very small amount
            appeal_cost=50.0,        # Expensive appeal
        ))
        assert r.recommendation in (
            AppealRecommendation.DO_NOT_APPEAL,
            AppealRecommendation.APPEAL_OPTIONAL,
        )

    # ── EV calculations ───────────────────────────────────────────

    def test_new_evidence_raises_win_probability(self):
        no_ev = track_appeal_window(self._inp())
        with_ev = track_appeal_window(self._inp(has_new_evidence=True))
        assert with_ev.appeal_win_probability > no_ev.appeal_win_probability

    def test_ev_positive_on_large_amount(self):
        r = track_appeal_window(self._inp(
            disputed_amount=500.0, appeal_cost=25.0,
            has_new_evidence=True))
        assert r.ev_of_appealing > 0
        assert r.is_ev_positive is True

    def test_win_probability_in_range(self):
        r = track_appeal_window(self._inp())
        assert 0.0 <= r.appeal_win_probability <= 1.0

    # ── Deadline urgency ──────────────────────────────────────────

    def test_critical_urgency_1_day_remaining(self):
        r = track_appeal_window(self._inp(
            network="PAYPAL", days_since_ruling=9))  # 1 day left
        assert r.deadline_urgency == "CRITICAL"

    def test_low_urgency_many_days_remaining(self):
        r = track_appeal_window(self._inp(
            network="VISA", days_since_ruling=5))  # 40 days left
        assert r.deadline_urgency == "LOW"

    # ── Report fields ─────────────────────────────────────────────

    def test_summary_contains_dispute_id(self):
        r = track_appeal_window(self._inp(dispute_id="D-XYZ"))
        assert "D-XYZ" in r.summary

    def test_steps_non_empty_when_appealing(self):
        r = track_appeal_window(self._inp(
            network="VISA",
            days_since_ruling=5,
            disputed_amount=200.0,
            has_new_evidence=True,
            appeal_cost=25.0,
        ))
        if r.recommendation in (AppealRecommendation.APPEAL_NOW, AppealRecommendation.APPEAL_OPTIONAL):
            assert len(r.required_steps) >= 1
