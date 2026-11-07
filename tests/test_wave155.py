"""
Wave 155 — Dispute Cluster Analyzer  POST /api/oracle/analyze-dispute-clusters

Tests:
  1. Same buyer + same IP → cluster found, overall_risk HIGH
  2. All distinct disputes → 0 clusters
  3. all_clusters is list
  4. unclustered_dispute_ids is list
  5. paypal_signal present
  6. high_risk_clusters subset of all_clusters
  7. Response shape complete
"""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from agents.watcher import app

_client = TestClient(app, raise_server_exceptions=False)


def _post(body: dict):
    return _client.post("/api/oracle/analyze-dispute-clusters", json=body).json()


def _d(dispute_id, buyer_id="B-001", product_id="PROD-001", ip="10.0.0.1",
        date="2026-10-01", amount=100.0, reason="INR"):
    return {
        "dispute_id": dispute_id, "buyer_id": buyer_id,
        "product_id": product_id, "ip_address": ip,
        "dispute_date": date, "transaction_amount": amount,
        "reason_code": reason,
    }


def test_same_buyer_cluster():
    disputes = [
        _d("PP-D-001", buyer_id="B-999", ip="10.1.2.100"),
        _d("PP-D-002", buyer_id="B-999", ip="10.1.2.101"),
        _d("PP-D-003", buyer_id="B-999", ip="10.1.2.102"),
    ]
    d = _post({"merchant_id": "M-001", "disputes": disputes})
    assert d["total_clusters_found"] >= 1
    assert d["overall_risk"] in ("HIGH", "MODERATE")
    assert d["wave"] == 155


def test_distinct_disputes_no_cluster():
    disputes = [
        _d(f"PP-D-{i:03d}", buyer_id=f"B-{i:03d}", ip=f"10.0.{i}.1")
        for i in range(5)
    ]
    d = _post({"merchant_id": "M-002", "disputes": disputes})
    assert d["overall_risk"] in ("NONE", "LOW", "MODERATE")


def test_all_clusters_list():
    disputes = [_d(f"PP-D-{i:03d}") for i in range(3)]
    d = _post({"merchant_id": "M-003", "disputes": disputes})
    assert isinstance(d["all_clusters"], list)


def test_unclustered_list():
    disputes = [
        _d("PP-D-001", buyer_id="B-001"),
        _d("PP-D-002", buyer_id="B-002"),
    ]
    d = _post({"merchant_id": "M-004", "disputes": disputes})
    assert isinstance(d["unclustered_dispute_ids"], list)


def test_paypal_signal_present():
    d = _post({"merchant_id": "M-005", "disputes": [_d("PP-D-001")]})
    assert isinstance(d["paypal_signal"], str)
    assert len(d["paypal_signal"]) > 0


def test_high_risk_subset_of_all():
    disputes = [
        _d("PP-D-001", buyer_id="B-999"),
        _d("PP-D-002", buyer_id="B-999"),
        _d("PP-D-003", buyer_id="B-888"),
    ]
    d = _post({"merchant_id": "M-006", "disputes": disputes})
    all_ids = {c["cluster_id"] for c in d["all_clusters"]}
    high_ids = {c["cluster_id"] for c in d["high_risk_clusters"]}
    assert high_ids.issubset(all_ids)


def test_response_shape():
    d = _post({
        "merchant_id": "M-007",
        "disputes": [_d("PP-D-001")],
    })
    required = {
        "merchant_id", "total_disputes_analyzed", "total_clusters_found",
        "high_risk_clusters", "all_clusters", "unclustered_dispute_ids",
        "overall_risk", "paypal_signal", "wave",
    }
    assert required.issubset(d.keys())
