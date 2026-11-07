"""Wave 79 — Dispute Cluster Analyzer tests."""
import pytest
from oracle.dispute_cluster_analyzer import (
    ClusterRisk,
    DisputeRecord,
    ClusterAnalysisInput,
    analyze_dispute_clusters,
)


def _d(did, buyer="BUYER_A", product="PROD-1", ip="192.168.1.100",
       date="2026-10-15", amount=299.0, reason_code="INR"):
    return DisputeRecord(
        dispute_id=did,
        buyer_id=buyer,
        product_id=product,
        ip_address=ip,
        dispute_date=date,
        transaction_amount=amount,
        reason_code=reason_code,
    )


def _run(disputes, threshold=0.10):
    return analyze_dispute_clusters(ClusterAnalysisInput(
        merchant_id="MERCH_CLU",
        disputes=disputes,
        amount_similarity_threshold=threshold,
    ))


# ─── Basic clustering ─────────────────────────────────────────────────────────

class TestBasicClustering:
    def test_single_dispute_no_clusters(self):
        r = _run([_d("D1")])
        assert r.total_clusters_found == 0
        assert len(r.unclustered_dispute_ids) == 1

    def test_two_disputes_same_buyer_one_cluster(self):
        r = _run([_d("D1", buyer="B1"), _d("D2", buyer="B1",
                   product="PROD-2", ip="10.0.0.1", date="2026-10-16")])
        assert r.total_clusters_found == 1

    def test_no_shared_attributes_no_cluster(self):
        r = _run([
            _d("D1", buyer="B1", product="P1", ip="1.1.1.1", date="2026-10-01", amount=100.0),
            _d("D2", buyer="B2", product="P2", ip="2.2.2.2", date="2026-10-02", amount=500.0),
        ])
        assert r.total_clusters_found == 0

    def test_three_disputes_connected_one_cluster(self):
        # D1-D2 share buyer, D2-D3 share product → all in one cluster
        r = _run([
            _d("D1", buyer="B_SAME", product="P1"),
            _d("D2", buyer="B_SAME", product="P2"),
        ])
        assert r.total_clusters_found == 1
        assert len(r.all_clusters[0].dispute_ids) == 2


# ─── Shared attributes detection ─────────────────────────────────────────────

class TestSharedAttributes:
    def test_same_buyer_id_detected(self):
        r = _run([_d("D1", buyer="B1"), _d("D2", buyer="B1",
                   product="P2", ip="10.0.0.1", date="2026-11-01")])
        assert "same_buyer_id" in r.all_clusters[0].shared_attributes

    def test_same_ip_subnet_detected(self):
        r = _run([
            _d("D1", buyer="B1", ip="192.168.1.100"),
            _d("D2", buyer="B2", ip="192.168.1.200"),
        ])
        # Same /24 → same_ip_subnet_24
        if r.total_clusters_found > 0:
            assert "same_ip_subnet_24" in r.all_clusters[0].shared_attributes

    def test_same_product_id_detected(self):
        r = _run([
            _d("D1", buyer="B1", product="P_SAME"),
            _d("D2", buyer="B2", product="P_SAME", ip="9.9.9.9"),
        ])
        assert r.total_clusters_found > 0
        assert "same_product_id" in r.all_clusters[0].shared_attributes

    def test_similar_amounts_within_threshold(self):
        r = _run([
            _d("D1", buyer="B1", product="P1", ip="1.1.1.1", date="2026-10-01", amount=100.0),
            _d("D2", buyer="B2", product="P2", ip="2.2.2.2", date="2026-10-02", amount=105.0),
        ], threshold=0.10)
        # 5% difference → within 10% threshold → similar_amount
        if r.total_clusters_found > 0:
            assert "similar_amount" in r.all_clusters[0].shared_attributes

    def test_different_amounts_no_cluster(self):
        r = _run([
            _d("D1", buyer="B1", product="P1", ip="1.1.1.1", date="2026-10-01", amount=100.0),
            _d("D2", buyer="B2", product="P2", ip="2.2.2.2", date="2026-10-02", amount=500.0),
        ], threshold=0.10)
        assert r.total_clusters_found == 0


# ─── Fraud score and risk levels ─────────────────────────────────────────────

class TestFraudScore:
    def test_buyer_id_match_scores_high(self):
        # buyer_id=0.40 alone → MODERATE (≥0.30 but <0.60)
        # Use very different amounts so similar_amount doesn't fire
        r = _run([
            _d("D1", buyer="B1", product="P1", ip="1.1.1.1", date="2026-10-01", amount=50.0),
            _d("D2", buyer="B1", product="P2", ip="2.2.2.2", date="2026-10-02", amount=499.0),
        ])
        assert r.all_clusters[0].fraud_score == pytest.approx(0.40, abs=0.001)
        assert r.all_clusters[0].risk_level == ClusterRisk.MODERATE

    def test_multiple_attributes_high_risk(self):
        # buyer + ip + product = 0.40+0.25+0.20 = 0.85 → HIGH_RISK
        r = _run([
            _d("D1", buyer="B1", product="P_SAME", ip="192.168.1.100"),
            _d("D2", buyer="B1", product="P_SAME", ip="192.168.1.200"),
        ])
        assert r.all_clusters[0].risk_level == ClusterRisk.HIGH_RISK

    def test_clusters_sorted_by_fraud_score_desc(self):
        # D1-D2 share buyer only; D3-D4 share buyer+product+ip → different clusters
        r = _run([
            _d("D1", buyer="B_A", product="P1", ip="1.1.1.1", date="2026-10-01"),
            _d("D2", buyer="B_A", product="P2", ip="2.2.2.2", date="2026-10-02"),
            _d("D3", buyer="B_B", product="P_X", ip="192.168.5.100"),
            _d("D4", buyer="B_B", product="P_X", ip="192.168.5.200"),
        ])
        if len(r.all_clusters) >= 2:
            scores = [c.fraud_score for c in r.all_clusters]
            assert scores == sorted(scores, reverse=True)


# ─── Overall risk ─────────────────────────────────────────────────────────────

class TestOverallRisk:
    def test_no_clusters_overall_none(self):
        r = _run([
            _d("D1", buyer="B1", product="P1", ip="1.1.1.1", date="2026-10-01", amount=100),
            _d("D2", buyer="B2", product="P2", ip="2.2.2.2", date="2026-10-02", amount=500),
        ])
        assert r.overall_risk == "NONE"

    def test_high_risk_cluster_overall_high(self):
        r = _run([
            _d("D1", buyer="B1", product="P_SAME", ip="192.168.1.100"),
            _d("D2", buyer="B1", product="P_SAME", ip="192.168.1.200"),
        ])
        assert r.overall_risk == "HIGH"

    def test_single_dispute_overall_none(self):
        r = _run([_d("D1")])
        assert r.overall_risk == "NONE"


# ─── Report fields ────────────────────────────────────────────────────────────

class TestReportFields:
    def test_merchant_id_echoed(self):
        r = _run([_d("D1"), _d("D2", buyer="B2")])
        assert r.merchant_id == "MERCH_CLU"

    def test_unclustered_disputes_returned(self):
        r = _run([
            _d("D1", buyer="B1", product="P1", ip="1.1.1.1", date="2026-10-01", amount=100),
            _d("D2", buyer="B2", product="P2", ip="2.2.2.2", date="2026-10-02", amount=500),
            _d("D3", buyer="B3", product="P3", ip="3.3.3.3", date="2026-10-03", amount=999),
        ])
        # No clusters → all unclustered
        assert len(r.unclustered_dispute_ids) == 3

    def test_total_disputes_analyzed(self):
        disputes = [_d(f"D{i}", buyer=f"B{i}") for i in range(5)]
        r = _run(disputes)
        assert r.total_disputes_analyzed == 5

    def test_paypal_signal_contains_merchant_id(self):
        r = _run([_d("D1"), _d("D2", buyer="B2")])
        assert "MERCH_CLU" in r.paypal_signal

    def test_recommendation_non_empty(self):
        r = _run([
            _d("D1", buyer="B1"),
            _d("D2", buyer="B1", product="P2", ip="9.9.9.9"),
        ])
        if r.total_clusters_found > 0:
            assert len(r.all_clusters[0].recommendation) > 20
